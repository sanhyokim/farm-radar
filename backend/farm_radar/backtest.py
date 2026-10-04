"""N5b さかのぼりの計算（docs/n5-plan-2026-10-03.md の 1章 ②〜⑥。2026-10-03 オーナー承認 1A〜5A）。

今の版（18000）のデータの写し（data/import/old-18000.sqlite3。読むだけ）にある15分ごとのプールの値段で、
アプリの見込み（その時のスコアの値動き σ から出した数字）と、同じ幅を実際に動かしたときの数字を比べる。

- ② 幅の中にいた割合・置き直しの回数: 見込み（σ²/r²、1 − 回数 × 待ち時間 ÷ 1日）と、値段の記録で
  幅 ±r を置き、外に出て待ち時間がたったら今の値段を真ん中に置き直したときの回数・幅の中の時間
- ③ 値動きの損: 見込み（σ²/(4r)。建玉のお金に対する割合）と、置いたときに保険（token0 の量だけ売り）を
  掛けたとして、v3 の式で中身を動かした損（置き直すまでの分。日ごとに区切る）。
  外れ方を「σ の読み違い」と「式のずれ」に分けるため、その日の実際の値動き（15分ごとの変化から）で式を当てた値も出す
- ④ 費用: 置き直し1回の費用（両替する額 ×（手数料 + 両替のずれ）+ ガス代2回）。ずれは、その時のプールの流動性で見積もる
- ⑤ 保険: 見込みの資金調達料（スコアの入力）と、Lighter の資金調達率の記録（新しい版の feeds。1時間ごと）。
  預け金: 記録の中で、売っていたコインがいちばん上がった幅と、耐える上げ幅（hedge_withstand_rise_pct）
- ⑥ 早く出る決まり: 段階1（プールのお金が1時間で −30%）・段階2（報酬のコインが24時間で −15%・投げ売り 1時間 −15% /
  24時間 −30%）を記録に当てはめ、出た時刻と、そのあと24時間でどうなったか（避けた下げ・逃した戻り）。線を変えたときの回数も並べる

読むだけ。お金を動かすコードはない。数字は「作業場所の試験用の値」か「あなたのパソコンで確かめた値」かを、呼ぶ側が分けて書く。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, contract_address, load_venue
from .scoring import model as m
from .scoring.run import own_series
from .tokens import load_tokens
from .views import next_epoch_flip

VERSION = 9                     # 計算を変えたら上げる（とっておいた結果を使わない）。このファイルの中身の印もキーに入れる（_source_mark）
STEP_MAX_S = 45 * 60            # 15分ごとの記録で、これより間があいたら、その間の時間は数えない（欠損）
DAY_MIN_COVERAGE = 0.9          # 1日のうち、これ以上の時間の記録がある日だけ比べる
PASS_REL = 0.30                 # 合格の目安: 差が見込みの30%以内（評価のときと同じ）
PASS_ABS_CAPITAL = 0.001        # または、資金の0.1%以内（1日あたり）
# 資金調達率だけは、資金の0.1%（$1/日）の余裕だと保険の額（数百ドル）の資金調達料がいつも収まってしまう
# （2026-10-04 オーナーの質問3）。割合（30%）と、ごく小さい率のための余裕（1日 0.002%。年 0.73%）で見る
FUNDING_ABS_DAY = 0.00002       # 1日 0.002%（割合。年 0.73%）
FUNDS_THRESHOLDS = (20, 30, 40, 50)          # 段階1 の線を変えたときの回数（%。今は config の値）
REWARD_THRESHOLDS = (-10, -15, -20, -25)     # 段階2 の報酬のコインの線（%）
EVENT_GAP_S = 6 * 3600          # 同じプール・同じコインの合図は、6時間あいたら別の出来事として数える
SMALL_POOL_USD = 50_000.0       # 段階1 の中身を分ける: これより小さいプール（2026-10-03 オーナーの例）
RECOVER_S = 6 * 3600            # 段階1 の中身を分ける: この時間のうちに、減る前の90%まで戻ったら「すぐ戻った」
RECOVER_SHARE = 0.9
BIG_DROP_PCT = -20.0            # 段階1 の中身を分ける: そのあと24時間で、プールのコインがこれ以上下がったら「大きな値下がり」
KINDS = ("stock", "stable", "bonus", "coin")   # 株 / ステーブルどうし / ボーナスのコイン / ふつうのコイン
# 値段が動いていないプール: 15分ごとの記録の、となりどうしの値段が同じ割合がこれ以上（取引がほとんどない。仮）。
# 見込み（外の値動き）と比べると、実際の損が 0 に見えて「ずれ」が大きく出るので、比べるのから外して別に並べる
# （2026-10-04 オーナーの質問3。WETH/WOOD）
STILL_SHARE = 0.95
FLIP_S = 3600                   # 置き直してからこの時間のうちに前の幅に戻ったら「行ったり来たり」（N5 最終判断の準備の 5）


# --- v3 の式（token1 建て。幅 [pa, pb]、流動性 L） --------------------------------------------------------

def lp_value(p: float, pa: float, pb: float, liq: float) -> float:
    """幅に置いた中身の値打ち（token1 建て）。"""
    if p <= pa:
        return liq * (1 / math.sqrt(pa) - 1 / math.sqrt(pb)) * p
    if p >= pb:
        return liq * (math.sqrt(pb) - math.sqrt(pa))
    return liq * (2 * math.sqrt(p) - math.sqrt(pa) - p / math.sqrt(pb))


def lp_amount0(p: float, pa: float, pb: float, liq: float) -> float:
    """中身の token0 の量（保険で売る量）。"""
    if p >= pb:
        return 0.0
    lo = max(p, pa)
    return liq * (1 / math.sqrt(lo) - 1 / math.sqrt(pb))


@dataclass
class Segment:
    """1回置いた建玉（値打み 1 に合わせた量。保険は置いたときの token0 の量を売る）。"""
    p0: float
    pa: float
    pb: float
    liq: float
    x0: float

    @classmethod
    def at(cls, p: float, r: float) -> Segment:
        pa, pb = p * (1 - r), p * (1 + r)
        liq = 1 / lp_value(p, pa, pb, 1.0)
        return cls(p, pa, pb, liq, lp_amount0(p, pa, pb, liq))

    def pnl(self, p: float) -> float:
        """保険込みの損益（置いたときの値打ちに対する割合。値動きの向きは保険で消え、残りが値動きの損）。"""
        return lp_value(p, self.pa, self.pb, self.liq) - 1 - self.x0 * (p - self.p0)

    def inside(self, p: float) -> bool:
        return self.pa <= p <= self.pb


def _day(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")


@dataclass
class DayReplay:
    seconds: float = 0.0
    in_range_seconds: float = 0.0
    rebalances: int = 0
    gamma: float = 0.0              # 保険込みの値動きの損（建玉のお金に対する割合。プラス = 損）
    rebalance_cost: float = 0.0     # 置き直しの費用（建玉のお金に対する割合）
    var: float = 0.0                # その日の実際の値動き（15分ごとの対数の変化の2乗の合計）
    rebalance_slips: list[float] = field(default_factory=list)
    flips: int = 0                  # 置き直したあと FLIP_S のうちに、値段が前の幅に戻った回数（行ったり来たり）


def replay(points: list[tuple[int, float, float | None]], r: float, wait_s: float,
           cost_of: Any = None, buffer: float = 0.0) -> dict[str, DayReplay]:
    """値段の並び (UNIX秒, 値段, 今の値段のところの流動性) で、幅 ±r を置き直しながら動かす。日ごと（UTC）の結果。

    cost_of(値段, 流動性) は置き直し1回の (費用の割合, 両替のずれ) を返す（None なら0）。
    buffer は境目の余裕（幅の大きさに対する割合。N5 最終判断の準備の 5）: 境目からさらに幅 × buffer 外に出たまま
    wait_s たったら置き直す。0 なら前と同じ（境目を出たら数え始める）。幅の中にいた時間は、余裕を入れない本当の幅で数える。"""
    out: dict[str, DayReplay] = defaultdict(DayReplay)
    if len(points) < 2:
        return {}
    seg = Segment.at(points[0][1], r)
    realized = 0.0
    prev_total = 0.0
    out_since: int | None = None
    old: Segment | None = None          # 置き直す前の幅（行ったり来たりを数える）
    old_t = 0
    for i in range(1, len(points)):
        t_prev, p_prev, _ = points[i - 1]
        t, p, liq = points[i]
        day = out[_day(t_prev)]
        dt = t - t_prev
        if 0 < dt <= STEP_MAX_S:
            day.seconds += dt
            if seg.inside(p_prev):
                day.in_range_seconds += dt
            if p_prev > 0 and p > 0:
                day.var += math.log(p / p_prev) ** 2
        total = realized + seg.pnl(p)
        out[_day(t)].gamma -= total - prev_total
        prev_total = total
        if old is not None:
            if t - old_t > FLIP_S:
                old = None
            elif old.inside(p):             # 置き直さなくても、まもなく前の幅に戻っていた
                out[_day(t)].flips += 1
                old = None
        edge = buffer * (seg.pb - seg.pa)
        if seg.pa - edge <= p <= seg.pb + edge:
            out_since = None
            continue
        if out_since is None:
            out_since = t
        if t - out_since >= wait_s:
            realized = total
            old, old_t = seg, t
            seg = Segment.at(p, r)
            prev_total = realized              # 置き直した直後の損益は realized と同じ（pnl = 0）
            out_since = None
            d = out[_day(t)]
            d.rebalances += 1
            if cost_of is not None:
                c, slip = cost_of(p, liq)
                d.rebalance_cost += c
                if slip is not None:
                    d.rebalance_slips.append(slip)
    return dict(out)


# --- 今の版の写しを読む ------------------------------------------------------------------------------

def _ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _pools(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT p.*, (SELECT COUNT(*) FROM pool_snapshots s WHERE s.pool_id=p.id) n
                           FROM pools p ORDER BY p.id""").fetchall()


def _points(conn: sqlite3.Connection, pool_id: str) -> list[tuple[int, float, float | None, sqlite3.Row]]:
    out = []
    for row in conn.execute("SELECT * FROM pool_snapshots WHERE pool_id=? AND price > 0 ORDER BY ts", (pool_id,)):
        t = _ts(row["block_time"] or row["ts"])
        if t is not None:
            out.append((t, float(row["price"]), float(row["liquidity_total"]) if row["liquidity_total"] else None, row))
    return out


def fee_pct(fee_tier: Any) -> float | None:
    """プールの手数料の段（100 万分の1 の単位。100 = 0.01%、500 = 0.05%）を % で。"""
    try:
        return int(fee_tier) / 10000
    except (TypeError, ValueError):
        return None


# Uniswap v3 の値段の端（TickMath の MIN_SQRT_RATIO・MAX_SQRT_RATIO）。流動性が 0 のプールで売り買いされると、
# 値段がここまで動いたまま残る（2026-10-04 チェーンの記録: WETH/ARROW・SPCX/USDG は tick 887271・流動性 0）
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


def pool_fee(conn: sqlite3.Connection | None, pool: Any) -> dict[str, Any]:
    """プールの手数料の段（%）。up. の工場は作成の記録に手数料を入れないので、今の版の pools.fee_tier は空。
    今の版が15分ごとにチェーンから読んだ fee()（pool_snapshots.fee）を使う（最後の値と、記録の中の最小・最大。
    up. は手数料が動くプールがある）。それもなければ pools.fee_tier。どちらもなければ None（推測しない）。"""
    out: dict[str, Any] = {"fee_pct": None, "fee_min_pct": None, "fee_max_pct": None, "fee_source": None,
                           "tick_spacing": _get(pool, "tick_spacing")}
    if conn is not None:
        try:
            r = conn.execute("SELECT MIN(fee), MAX(fee), (SELECT fee FROM pool_snapshots WHERE pool_id=? AND fee IS NOT NULL "
                             "ORDER BY ts DESC LIMIT 1) FROM pool_snapshots WHERE pool_id=? AND fee IS NOT NULL",
                             (pool["id"], pool["id"])).fetchone()
        except sqlite3.OperationalError:              # 古い写しで fee の列がない
            r = None
        if r and r[2] is not None:
            return {**out, "fee_pct": fee_pct(r[2]), "fee_min_pct": fee_pct(r[0]), "fee_max_pct": fee_pct(r[1]),
                    "fee_source": "snapshots"}
    f = fee_pct(_get(pool, "fee_tier"))
    return {**out, "fee_pct": f, "fee_min_pct": f, "fee_max_pct": f, "fee_source": "pools" if f is not None else None}


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def price_edge(pts: list[tuple[int, float, float | None, Any]]) -> dict[str, Any]:
    """最後の記録で、プールが空（流動性 0）か、値段が v3 の端にあるか（とても大きい・小さい値段の見分け）。"""
    if not pts:
        return {"empty": None, "at_edge": None}
    row = pts[-1][3]
    liq = pts[-1][2]
    try:
        sq = int(row["sqrt_price_x96"]) if row["sqrt_price_x96"] is not None else None
    except (TypeError, ValueError, KeyError, IndexError):
        sq = None
    edge = None if sq is None else ("max" if sq >= MAX_SQRT_RATIO - 1 else "min" if sq <= MIN_SQRT_RATIO + 1 else "")
    return {"empty": (liq or 0) == 0, "at_edge": edge}


def same_pair(still: list[dict[str, Any]], pools: list[sqlite3.Row],
              conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """値段が動いていないプールごとに、写しの中の同じ組（コインの組が同じ）のほかのプールを付ける。
    同じ名前のプールが会場に複数あるので、どれが動いていないのかを見分ける（2026-10-04 オーナーの質問1）。"""
    still_ids = {x["pool_id"] for x in still}
    out = []
    for x in still:
        me = next((p for p in pools if p["id"] == x["pool_id"]), None)
        others = []
        if me is not None:
            pair = {str(me["token0"]).lower(), str(me["token1"]).lower()}
            for p in pools:
                if p["id"] != me["id"] and {str(p["token0"]).lower(), str(p["token1"]).lower()} == pair:
                    others.append({"pool_id": p["id"], **pool_fee(conn, p), "points": p["n"],
                                   "still": p["id"] in still_ids})
        out.append({**x, "same_pair": others})
    return out


def still_share(points: list[tuple[int, float, Any, Any]] | list[tuple[int, float]]) -> float:
    """となりどうしの記録で、値段が変わっていない割合（0〜1）。"""
    pairs = list(zip(points, points[1:]))
    if not pairs:
        return 1.0
    return sum(1 for a, b in pairs if abs(b[1] - a[1]) <= 1e-12 * max(abs(a[1]), 1e-300)) / len(pairs)


def _scores(conn: sqlite3.Connection, pool_id: str) -> list[tuple[int, sqlite3.Row]]:
    out = []
    for row in conn.execute("SELECT * FROM scores WHERE pool_id=? ORDER BY ts", (pool_id,)):
        t = _ts(row["ts"])
        if t is not None:
            out.append((t, row))
    return out


def _score_at(scores: list[tuple[int, sqlite3.Row]], t: int, max_age_s: int = 3 * 3600) -> sqlite3.Row | None:
    """t の時点で出ていた、いちばん新しいスコア（その時のアプリの見込み）。"""
    best = None
    for st, row in scores:
        if st > t:
            break
        best = (st, row)
    if best is None or t - best[0] > max_age_s:
        return None
    return best[1]


def _details(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return json.loads(row["details_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _within(actual: float, pred: float, abs_ok: float) -> bool:
    return abs(actual - pred) <= max(PASS_REL * abs(pred), abs_ok)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2


def pool_kind(pool: sqlite3.Row, stables: frozenset[str], stocks: frozenset[str], rewards: frozenset[str]) -> str:
    """プールの種類（2026-10-03 オーナー「株・ステーブル・ふつうのコイン・ボーナスのコインごとに」）。
    株のトークンを含む → stock、ボーナスのコイン（会場の報酬のコイン）を含む → bonus、両方ステーブル → stable、ほか → coin。"""
    toks = {pool["token0"].lower(), pool["token1"].lower()}
    if toks & stocks or pool["is_stock_pair"]:
        return "stock"
    if toks & rewards:
        return "bonus"
    if toks <= stables:
        return "stable"
    return "coin"


# --- ②③④ 幅に置いた建玉 ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestSettings:
    amount_usd: float = 1000.0
    lp_share: float = 0.78              # 分け方が記録にないとき（$1,000 で 保険あり の分け方の目安）
    wait_minutes: float = 15.0
    swap_ratio: float = 0.5
    ranges_pct: tuple[float, ...] = (0.5, 1, 2, 3, 5, 10, 15)
    withstand_rise_pct: float = 50.0
    stay_days: float = 14.0
    funds_drop_pct: float = 30.0
    reward_drop_pct: float = -15.0
    dump_1h_pct: float = -15.0
    dump_24h_pct: float = -30.0
    target_apr_pct: float = 30.0        # 段階2「狙い以下3回」と段階4（N5 最終判断の準備）。設定の初めの値（画面で変えた値ではない）
    below_target_times: int = 3

    @classmethod
    def from_config(cls, config: Config) -> BacktestSettings:
        sc, rk, op = config.scoring, config.risk, config.opportunities
        return cls(amount_usd=float(sc.total_capital_usd), wait_minutes=float(sc.rebalance_wait_minutes),
                   swap_ratio=float(sc.swap_ratio), ranges_pct=tuple(sc.ranges_pct),
                   withstand_rise_pct=float(op.hedge_withstand_rise_pct), stay_days=float(op.stay_days),
                   funds_drop_pct=float(rk.emergency_pool_funds_drop_1h_pct),
                   reward_drop_pct=float(rk.exit_reward_token_24h_pct),
                   dump_1h_pct=float(rk.exit_dump_1h_pct), dump_24h_pct=float(rk.exit_dump_24h_pct),
                   target_apr_pct=float(op.target_apr_pct), below_target_times=int(config.guard.below_target_times))


def _cost_fn(det: dict[str, Any], pool: sqlite3.Row, s: BacktestSettings, c_lp: float):
    """置き直し1回の費用（建玉のお金に対する割合）と、両替のずれ。スコアの入力の値段・手数料・ガス代を使う。"""
    inp = det.get("inputs") or {}
    usd = inp.get("usd") or {}
    u0, u1 = usd.get(pool["token0_symbol"]), usd.get(pool["token1_symbol"])
    fee = float(inp.get("fee") or 0.0)
    gas = float(inp.get("gas_usd_per_tx") or 0.0)
    d0, d1 = pool["token0_decimals"], pool["token1_decimals"]

    def cost(price: float, liq: float | None) -> tuple[float, float | None]:
        slip = None
        if liq and u0 and u1 and d0 is not None and d1 is not None:
            t0, t1 = m.TokenSide(usd=float(u0), decimals=int(d0), sigma_usd=0), m.TokenSide(usd=float(u1), decimals=int(d1), sigma_usd=0)
            slip = m.swap_price_impact(c_lp * s.swap_ratio, int(liq), price, t0, t1)
        return s.swap_ratio * (fee + (slip or 0.0)) + 2 * gas / c_lp, slip

    pred_slip = inp.get("slippage")
    pred_cost = s.swap_ratio * (fee + float(pred_slip or 0.0)) + 2 * gas / c_lp
    return cost, pred_cost


def pool_days(conn: sqlite3.Connection, pool: sqlite3.Row, s: BacktestSettings) -> list[dict[str, Any]]:
    """1つのプールの、日ごと・幅ごとの見込みと実際（②③④）。"""
    pts = _points(conn, pool["id"])
    scores = _scores(conn, pool["id"])
    if len(pts) < 96 or not scores:
        return []
    series = [(t, p, liq) for t, p, liq, _ in pts]
    first_day = _day(pts[0][0])
    out = []
    wait_s = s.wait_minutes * 60
    for rp in s.ranges_pct:
        r = rp / 100
        # 置き直しの費用は、最初の日のスコアの入力で計算する（日ごとに入力を変えると、置いた建玉の続きが切れるため）
        det0 = _details(_score_at(scores, pts[0][0], 10 ** 9) or scores[0][1])
        c_lp = s.amount_usd * float((det0.get("split") or {}).get("lp") or s.lp_share)
        cost_fn, _ = _cost_fn(det0, pool, s, c_lp)
        days = replay(series, r, wait_s, cost_fn)
        for day, d in sorted(days.items()):
            if day == first_day or d.seconds < 86400 * DAY_MIN_COVERAGE:
                continue                    # 置き始めた日と、記録が足りない日は比べない
            start = int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())
            sc = _score_at(scores, start)
            if sc is None or not sc["sigma_pair"]:
                continue
            det = _details(sc)
            sigma = float(sc["sigma_pair"])
            _, pred_cost = _cost_fn(det, pool, s, c_lp)
            sigma_real = math.sqrt(d.var * 86400 / d.seconds)
            n_pred = m.rebalances_per_day(sigma, r)
            out.append({
                "day": day, "r_pct": rp, "best_r_pct": sc["best_r"], "sigma_pred": sigma, "sigma_real": sigma_real,
                "rebalances_pred": n_pred, "rebalances_real": d.rebalances * 86400 / d.seconds,
                "in_range_pred": m.in_range_ratio(sigma, r, s.wait_minutes), "in_range_real": d.in_range_seconds / d.seconds,
                "gamma_pred": sigma ** 2 / (4 * r), "gamma_formula_real_sigma": sigma_real ** 2 / (4 * r),
                "gamma_real": d.gamma * 86400 / d.seconds,
                "cost_pred": n_pred * pred_cost, "cost_real": d.rebalance_cost * 86400 / d.seconds,
                "cost_per_rebalance_pred": pred_cost,
                "slip_pred": (det.get("inputs") or {}).get("slippage"), "slip_real": _mean(d.rebalance_slips),
                "c_lp": c_lp, "coverage": d.seconds / 86400,
            })
    return out


def _summarize_ranges(rows: list[dict[str, Any]], s: BacktestSettings) -> list[dict[str, Any]]:
    """幅ごとに、②③④ が合っていた日の割合と、見込み・実際の平均。"""
    out = []
    by: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for x in rows:
        by[x["r_pct"]].append(x)
    for rp in sorted(by):
        xs = by[rp]
        cap = PASS_ABS_CAPITAL * s.amount_usd
        ok_n = [_within(x["rebalances_real"], x["rebalances_pred"], 0.5) for x in xs]
        ok_irr = [_within(x["in_range_real"], x["in_range_pred"], 0.05) for x in xs]
        ok_g = [_within(x["gamma_real"] * x["c_lp"], x["gamma_pred"] * x["c_lp"], cap) for x in xs]
        ok_gf = [_within(x["gamma_real"] * x["c_lp"], x["gamma_formula_real_sigma"] * x["c_lp"], cap) for x in xs]
        ok_c = [_within(x["cost_real"] * x["c_lp"], x["cost_pred"] * x["c_lp"], cap) for x in xs]

        def share(oks: list[bool]) -> float:
            return sum(oks) / len(oks) if oks else 0.0

        def avg(k: str) -> float | None:
            return _mean([x[k] for x in xs if x[k] is not None])

        def med(k: str) -> float | None:
            return _median([x[k] for x in xs if x[k] is not None])
        out.append({
            "r_pct": rp, "days": len(xs),
            "rebalances": {"pred": avg("rebalances_pred"), "real": avg("rebalances_real"), "ok_share": share(ok_n),
                           "pred_median": med("rebalances_pred"), "real_median": med("rebalances_real")},
            "in_range": {"pred": avg("in_range_pred"), "real": avg("in_range_real"), "ok_share": share(ok_irr)},
            "gamma": {"pred": avg("gamma_pred"), "real": avg("gamma_real"), "formula_real_sigma": avg("gamma_formula_real_sigma"),
                      "ok_share": share(ok_g), "ok_share_real_sigma": share(ok_gf),
                      "pred_median": med("gamma_pred"), "real_median": med("gamma_real"),
                      "formula_real_sigma_median": med("gamma_formula_real_sigma")},
            "cost": {"pred": avg("cost_pred"), "real": avg("cost_real"), "ok_share": share(ok_c),
                     "slip_pred": avg("slip_pred"), "slip_real": avg("slip_real"),
                     "pred_median": med("cost_pred"), "real_median": med("cost_real")},
            "sigma": {"pred": avg("sigma_pred"), "real": avg("sigma_real"),
                      "pred_median": med("sigma_pred"), "real_median": med("sigma_real")},
        })
    return out


# --- 比べる相手（N5c。docs/n5-plan-2026-10-03.md 2-4。SPEC 13.2「何もしない場合、レンディングに置いた場合と比べる」） ---

def lending_daily(fconn: sqlite3.Connection | None) -> dict[str, float]:
    """比べる相手の貸し出しの、日ごとの年あたりの利回り（%。DefiLlama の毎日の記録。feeds/trial.LENDING_BASELINE）。"""
    from .feeds.trial import LENDING_BASELINE
    if fconn is None:
        return {}
    try:
        return {str(d): float(a) for d, a in fconn.execute(
            "SELECT day, apy FROM llama_yield_history WHERE pool=? AND apy IS NOT NULL", (LENDING_BASELINE["pool"],))}
    except sqlite3.OperationalError:
        return {}


def _full_income(x: dict[str, Any] | None) -> float | None:
    """スコアの幅ごとの収入（幅の中にいる割合の見込みをかけた値）を、ずっと幅の中にいたときの1日の額に戻す。"""
    if not x or x.get("income") is None or not x.get("in_range_ratio"):
        return None
    return float(x["income"]) / float(x["in_range_ratio"])


def baseline_days(conn: sqlite3.Connection, pool: sqlite3.Row, s: BacktestSettings,
                  lend: dict[str, float]) -> list[dict[str, Any]]:
    """1つのプールの日ごとに、今のやり方と比べる相手の1日の損益（ドル。建玉のお金 c_lp あたり）。

    - 今のやり方: その日のスコアのいちばん良い幅で、外に出たら置き直す（②〜④ と同じ動かし方）。
    - 広い幅で置きっぱなし: 設定のいちばん広い幅で、記録の最初に1回置いて、置き直さない。
    - 貸し出し: 同じ額を LENDING_BASELINE に置く。何もしない: 0。
    収入（手数料とボーナス）は今の版のスコアの見込みに、実際に幅の中にいた時間の割合をかけたもの（実際の収入の記録はないため）。
    値動きの損と置き直しの費用は実際の値段の並びで計算した値。保険の資金調達料はどちらにも入れない（⑤ で別に比べる）。"""
    pts = _points(conn, pool["id"])
    scores = _scores(conn, pool["id"])
    if len(pts) < 96 or not scores:
        return []
    series = [(t, p, liq) for t, p, liq, _ in pts]
    first_day = _day(pts[0][0])
    det0 = _details(_score_at(scores, pts[0][0], 10 ** 9) or scores[0][1])
    c_lp = s.amount_usd * float((det0.get("split") or {}).get("lp") or s.lp_share)
    cost_fn, _ = _cost_fn(det0, pool, s, c_lp)
    wide = max(s.ranges_pct)
    alone = replay(series, wide / 100, math.inf)
    moved: dict[float, dict[str, DayReplay]] = {}
    out = []
    for day, d in sorted(alone.items()):
        if day == first_day or d.seconds < 86400 * DAY_MIN_COVERAGE:
            continue
        sc = _score_at(scores, int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()))
        if sc is None or sc["best_r"] is None:
            continue
        ranges = {round(float(x["r_pct"]), 6): x for x in _details(sc).get("ranges") or [] if x.get("r_pct") is not None}
        best = round(float(sc["best_r"]), 6)
        inc_b, inc_w = _full_income(ranges.get(best)), _full_income(ranges.get(round(wide, 6)))
        if inc_b is None or inc_w is None:
            continue
        if best not in moved:
            moved[best] = replay(series, best / 100, s.wait_minutes * 60, cost_fn)
        db = moved[best].get(day)
        if db is None or db.seconds < 86400 * DAY_MIN_COVERAGE:
            continue
        k_b, k_w = 86400 / db.seconds, 86400 / d.seconds
        now_usd = inc_b * db.in_range_seconds / db.seconds - (db.gamma + db.rebalance_cost) * k_b * c_lp
        wide_usd = inc_w * d.in_range_seconds / d.seconds - d.gamma * k_w * c_lp
        apy = lend.get(day)
        out.append({"day": day, "c_lp": c_lp, "r_pct": best, "wide_r_pct": wide,
                    "now": now_usd, "now_income": inc_b * db.in_range_seconds / db.seconds,
                    "wide": wide_usd, "wide_in_range": d.in_range_seconds / d.seconds,
                    "lend": c_lp * apy / 100 / 365 if apy is not None else None, "nothing": 0.0})
    return out


def _summarize_baselines(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """年あたりの割合（建玉のお金あたり、%）の平均と、今のやり方が勝った日の割合。
    年あたりは、記録のある短い期間の1日あたりの平均を 365 倍した参考値（1年間の見込みではない）。"""
    def year_pct(k: str) -> float | None:
        m = _mean([x[k] / x["c_lp"] for x in rows if x.get(k) is not None and x["c_lp"]])
        return m * 365 * 100 if m is not None else None

    with_lend = [x for x in rows if x["lend"] is not None]
    # days・lend_days は「プールと日の組」の数（暦の日数ではない）。暦の日数は calendar_days・lend_calendar_days
    # （2026-10-04 オーナー: 「貸し出し 3.9%（231日）」が 231日分の記録に見える）
    return {"days": len(rows), "calendar_days": len({x["day"] for x in rows}),
            "lend_calendar_days": len({x["day"] for x in with_lend}),
            "first_day": min((x["day"] for x in rows), default=None), "last_day": max((x["day"] for x in rows), default=None),
            "now_year_pct": year_pct("now"), "now_income_year_pct": year_pct("now_income"),
            "wide_year_pct": year_pct("wide"), "lend_year_pct": year_pct("lend"), "nothing_year_pct": 0.0 if rows else None,
            "beat_wide_share": sum(1 for x in rows if x["now"] > x["wide"]) / len(rows) if rows else None,
            "beat_lend_share": sum(1 for x in with_lend if x["now"] > x["lend"]) / len(with_lend) if with_lend else None,
            "beat_nothing_share": sum(1 for x in rows if x["now"] > 0) / len(rows) if rows else None,
            "lend_days": len(with_lend)}


def _lending_info() -> dict[str, str]:
    from .feeds.trial import LENDING_BASELINE
    return {k: LENDING_BASELINE[k] for k in ("name", "source", "checked")}


# --- ⑤ 保険 ----------------------------------------------------------------------------------------------

def funding_actual(fconn: sqlite3.Connection | None, market_id: int, start: int, end: int) -> float | None:
    """Lighter の記録から、売りを持ったときの1日の支払い（元本に対する割合。プラス = 払う）。
    rate は「1時間あたりの%」、direction は払う側（external/lighter.py。2026-09-29 照合済み）。"""
    if fconn is None:
        return None
    try:
        rows = fconn.execute("SELECT rate, direction FROM lighter_funding_history WHERE market_id=? AND ts>=? AND ts<?",
                             (market_id, start, end)).fetchall()
    except sqlite3.OperationalError:
        return None
    vals = [(-1 if d == "long" else 1) * float(rt) / 100 for rt, d in rows if rt is not None]
    return sum(vals) / len(vals) * 24 if vals else None


def hedge_days(conn: sqlite3.Connection, fconn: sqlite3.Connection | None, pool: sqlite3.Row,
               s: BacktestSettings) -> list[dict[str, Any]]:
    scores = _scores(conn, pool["id"])
    if not scores:
        return []
    days = sorted({_day(t) for t, _ in scores})
    out = []
    for day in days[1:]:
        start = int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())
        det = _details(_score_at(scores, start))
        c_lp = s.amount_usd * float((det.get("split") or {}).get("lp") or s.lp_share)
        for sym, h in ((det.get("inputs") or {}).get("hedge") or {}).items():
            if not isinstance(h, dict) or h.get("hedge_id") != "lighter" or h.get("market_id") is None:
                continue
            real = funding_actual(fconn, int(h["market_id"]), start, start + 86400)
            if real is None or h.get("funding_daily") is None:
                continue
            out.append({"day": day, "symbol": sym, "perp": h.get("symbol"), "market_id": h["market_id"],
                        "pred": float(h["funding_daily"]), "real": real, "notional": c_lp * 0.5})
    return out


def _summarize_funding(rows: list[dict[str, Any]], s: BacktestSettings) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in rows:
        by[x["perp"] or x["symbol"]].append(x)
    out = []
    for perp, xs in sorted(by.items()):
        seen = {(x["day"]): x for x in xs}          # 同じ日に同じ銘柄を使うプールが複数あっても1日1回
        xs = list(seen.values())
        cap = PASS_ABS_CAPITAL * s.amount_usd
        loose = [_within(x["real"] * x["notional"], x["pred"] * x["notional"], cap) for x in xs]
        ok = [_within(x["real"], x["pred"], FUNDING_ABS_DAY) for x in xs]
        out.append({"perp": perp, "days": len(xs), "pred": _mean([x["pred"] for x in xs]),
                    "real": _mean([x["real"] for x in xs]), "ok_share": sum(ok) / len(ok) if ok else 0.0,
                    "ok_share_loose": sum(loose) / len(loose) if loose else 0.0})
    return out


def max_rise(points: list[tuple[int, float]], window_s: float) -> dict[str, Any] | None:
    """記録の中で、window_s 以内にいちばん上がった幅（売りの保険の預け金が足りるかの目安）と、15分でいちばん大きな飛び。"""
    if len(points) < 2:
        return None
    best = (0.0, None, None)
    lo_i = 0
    # 窓の中の最安値からの上げ（単純に O(n·k)。記録は数千点なので十分速い）
    for j in range(1, len(points)):
        while points[j][0] - points[lo_i][0] > window_s:
            lo_i += 1
        low_t, low_p = min(points[lo_i:j], key=lambda x: x[1])
        if low_p > 0:
            rise = points[j][1] / low_p - 1
            if rise > best[0]:
                best = (rise, low_t, points[j][0])
    jump = max(((b[1] / a[1] - 1, b[0]) for a, b in zip(points, points[1:])
                if a[1] > 0 and 0 < b[0] - a[0] <= STEP_MAX_S), default=(0.0, None))
    return {"rise_pct": best[0] * 100, "from": best[1], "to": best[2], "jump_up_pct": jump[0] * 100, "jump_at": jump[1],
            "span_days": (points[-1][0] - points[0][0]) / 86400}


def max_rise_hl(candles: list[tuple[int, float, float, float]], window_s: float) -> dict[str, Any] | None:
    """1時間の足（時刻, 高値, 安値, 終値）で、window_s 以内に安値からいちばん上がった幅と、1時間でいちばん大きな上げ
    （前の足の終値 → 高値。市場が開くときの飛びを含む）。"""
    if len(candles) < 2:
        return None
    best = (0.0, None, None)
    lows: deque[int] = deque()          # 窓の中の安値の候補（安い順に並ぶ添字。足が多いので O(n) で数える）
    for j in range(1, len(candles)):
        i = j - 1
        while lows and candles[lows[-1]][2] >= candles[i][2]:
            lows.pop()
        lows.append(i)
        while lows and candles[j][0] - candles[lows[0]][0] > window_s:
            lows.popleft()
        if not lows:
            continue
        low_t, _, low_p, _ = candles[lows[0]]
        if low_p > 0:
            rise = candles[j][1] / low_p - 1
            if rise > best[0]:
                best = (rise, low_t, candles[j][0])
    jump = max(((b[1] / a[3] - 1, b[0]) for a, b in zip(candles, candles[1:])
                if a[3] > 0 and 0 < b[0] - a[0] <= 2 * 3600), default=(0.0, None))
    return {"rise_pct": best[0] * 100, "from": best[1], "to": best[2], "jump_up_pct": jump[0] * 100, "jump_at": jump[1],
            "span_days": (candles[-1][0] - candles[0][0]) / 86400}


def hedge_markets(tok_book: Any, fund_rows: list[dict[str, Any]]) -> dict[int, str]:
    """預け金を見る Lighter の市場: 保険に使う市場すべて（venues/tokens-robinhood.yaml の perps.map）と、今の版の写しで
    資金調達料を比べた市場。前は後ろだけで、写しの中で保険に使われた16市場しか出なかった（2026-10-04 オーナーの質問4）。"""
    out = {int(x["market_id"]): x["perp"] or x["symbol"] for x in fund_rows}
    for refs in (tok_book.perp_alts or {}).values():
        for r in refs:
            if r.venue == "lighter":
                out.setdefault(int(r.market_id), r.symbol)
    return out


def margins_long(fconn: sqlite3.Connection | None, markets: dict[int, str], s: "BacktestSettings") -> list[dict[str, Any]]:
    """保険に使う Lighter の市場ごとに、値段の過去（最大90日。lighter_price_history）で預け金の目安を見る
    （2026-10-04 オーナーの質問4。今の版の写しは7日ほどしかないため）。"""
    if fconn is None:
        return []
    out = []
    for mid, sym in sorted(markets.items(), key=lambda x: x[1] or ""):
        try:
            rows = fconn.execute("SELECT ts, high, low, close FROM lighter_price_history WHERE market_id=? "
                                 "AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL ORDER BY ts",
                                 (mid,)).fetchall()
        except sqlite3.OperationalError:
            return []
        mr = max_rise_hl([(int(t), float(h), float(lo), float(c)) for t, h, lo, c in rows], s.stay_days * 86400)
        if mr:
            out.append({"market_id": mid, "symbol": sym, **mr, "withstand_pct": s.withstand_rise_pct,
                        "enough": mr["rise_pct"] < s.withstand_rise_pct})
    return out


# --- ⑥ 早く出る決まり -------------------------------------------------------------------------------------

def _at_or_before(points: list[tuple[int, float]], t: int, max_gap: int = 20 * 60) -> float | None:
    lo, hi = 0, len(points) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None or t - points[best][0] > max_gap:
        return None
    return points[best][1]


def changes(points: list[tuple[int, float]], hours: float) -> list[tuple[int, float, float]]:
    """各時点の (時刻, 値段, hours 時間前と比べた変化)。比べる記録がない時点は入れない（views.series_change と同じ比べ方）。"""
    out = []
    span = int(hours * 3600)
    j = 0
    for t, p in points:
        while j + 1 < len(points) and points[j + 1][0] <= t - span:
            j += 1
        if points[j][0] <= t - span and points[j][1] > 0:
            out.append((t, p, p / points[j][1] - 1))
    return out


def events(chs: list[tuple[int, float, float]], hit, after_points: list[tuple[int, float]]) -> list[dict[str, Any]]:
    """合図が出た時点（同じ出来事は EVENT_GAP_S あいたら別）と、そのあと6時間・24時間の値段の動き。"""
    out = []
    last = None
    for t, p, ch in chs:
        if not hit(ch):
            continue
        if last is not None and t - last < EVENT_GAP_S:
            last = t
            continue
        last = t
        later = [(tt, pp) for tt, pp in after_points if t < tt <= t + 86400]
        p24 = _at_or_before(after_points, t + 86400, 3600) if later and later[-1][0] >= t + 86400 - 3600 else None
        p6 = _at_or_before(after_points, t + 6 * 3600, 3600) if later and later[-1][0] >= t + 6 * 3600 - 3600 else None
        out.append({"at": t, "price": p, "change_pct": ch * 100,
                     "after_6h_pct": (p6 / p - 1) * 100 if p6 and p > 0 else None,
                     "min_24h_pct": (min(pp for _, pp in later) / p - 1) * 100 if later and p > 0 else None,
                     "after_24h_pct": (p24 / p - 1) * 100 if p24 and p > 0 else None})
    return out


def pool_funds(pts: list[tuple[int, float, float | None, sqlite3.Row]], usd: dict[str, list[tuple[int, float]]],
               pool: sqlite3.Row) -> list[tuple[int, float, float]]:
    """段階1 の比べ方（risk_job と同じ）: 今と1時間前のコインの量を、どちらも今の値段で数えたプールのお金。(時刻, 今, 1時間前)。"""
    t0, t1 = pool["token0"].lower(), pool["token1"].lower()
    d0, d1 = pool["token0_decimals"], pool["token1_decimals"]
    if d0 is None or d1 is None:
        return []
    bal = [(t, int(row["balance0_raw"]) / 10 ** d0, int(row["balance1_raw"]) / 10 ** d1)
           for t, _, _, row in pts if row["balance0_raw"] is not None and row["balance1_raw"] is not None]
    out = []
    j = 0
    for t, b0, b1 in bal:
        while j + 1 < len(bal) and bal[j + 1][0] <= t - 3600:
            j += 1
        if not (bal[j][0] <= t - 3600 and t - 3600 - bal[j][0] <= 20 * 60):
            continue
        u0, u1 = _at_or_before(usd.get(t0, []), t), _at_or_before(usd.get(t1, []), t)
        if u0 is None or u1 is None:
            continue
        out.append((t, b0 * u0 + b1 * u1, bal[j][1] * u0 + bal[j][2] * u1))
    return out


def stage1_detail(t: int, funds: list[tuple[int, float, float]], coins: list[list[tuple[int, float]]]) -> dict[str, Any]:
    """段階1 の合図の中身（2026-10-03 オーナーの質問2）: プールの大きさ、すぐ戻ったか、そのあと大きく下がったか。"""
    before = next((ago for tt, _, ago in funds if tt == t), None)
    later = [(tt, now) for tt, now, _ in funds if t < tt <= t + RECOVER_S]
    recovered = bool(before and any(now >= RECOVER_SHARE * before for _, now in later))
    # 24時間後のプールのお金（合図の時点と比べる。N5 最終判断の準備の 2「24時間でさらに悪化した割合」）
    at_now = next((now for tt, now, _ in funds if tt == t), None)
    f24 = _at_or_before([(tt, now) for tt, now, _ in funds], t + 86400, 3600)
    drops = []
    for pts in coins:
        p0 = _at_or_before(pts, t)
        nxt = [pp for tt, pp in pts if t < tt <= t + 86400]
        if p0 and nxt:
            drops.append((min(nxt) / p0 - 1) * 100)
    worst = min(drops) if drops else None
    return {"funds_before_usd": before, "small": bool(before is not None and before < SMALL_POOL_USD),
            "recovered_6h": recovered, "coin_min_24h_pct": worst,
            "funds_after_24h_pct": (f24 / at_now - 1) * 100 if f24 and at_now else None,
            "big_drop": worst is not None and worst <= BIG_DROP_PCT}


def stage1_breakdown(evs: list[dict[str, Any]]) -> dict[str, int]:
    """段階1 の合図を分ける（重なりあり）。「どれでもない」は、大きいプールで、すぐ戻らず、大きな値下がりもなかったもの。"""
    return {"total": len(evs), "small": sum(1 for e in evs if e["small"]),
            "recovered_6h": sum(1 for e in evs if e["recovered_6h"]),
            "big_drop": sum(1 for e in evs if e["big_drop"]),
            "none": sum(1 for e in evs if not (e["small"] or e["recovered_6h"] or e["big_drop"]))}


def miss(x: dict[str, Any]) -> dict[str, Any]:
    """1つのプールの、1日の損（値動きの損 + 置き直しの費用）の見込みと実際（ドル）と、その差。"""
    pred = (x["gamma_pred"] + (x.get("cost_pred") or 0.0)) * (x.get("c_lp") or 0.0)
    real = (x["gamma_real"] + (x.get("cost_real") or 0.0)) * (x.get("c_lp") or 0.0)
    return {**x, "loss_pred_usd_day": pred, "loss_real_usd_day": real, "gap_usd_day": abs(pred - real)}


# --- N5 最終判断の準備（2026-10-04 18:20 JST オーナーの指示書。仮の数字の見直し案の材料。決めるのはオーナー） ---------
# ここは材料を数えるだけ。線や式は変えない。どれが「判断できる」「記録待ち」「材料不足」かは final_prep が決める。

GRID_BUFFERS = (0.0, 0.05, 0.10, 0.15)      # 5: 境目の余裕（幅の大きさに対する割合）
GRID_WAITS_MIN = (15.0, 30.0, 60.0)          # 5: 外に出てから直すまでの待ち時間（分）
MOVE_MULTIPLES = (1.0, 1.5, 2.0, 3.0)        # 4: 移る費用の何倍の得なら移るか（今は2倍）


def _round(xs: list[float], nd: int = 4) -> list[float]:
    return [round(x, nd) for x in xs]


def loss_series(base_rows: list[dict[str, Any]], amount_usd: float) -> dict[str, Any]:
    """1: 損の線の材料。今のやり方（その日のいちばん良い幅で置き直す）でプールを1つ持ったときの、総資産あたりの損益（%）。
    1日 = プールと日の組ごと、7日 = 記録が7日続いた窓ごと、始めてから = プールごとの、記録の最初からのいちばん悪いところ。
    保険の資金調達料とボーナスのコインの値下がりは入らない（比べる相手と同じ作り）。"""
    by_pool: dict[str, dict[str, float]] = defaultdict(dict)
    for x in base_rows:
        by_pool[x["pool_id"]][x["day"]] = x["now"]
    day, week, start, spans = [], [], [], []
    for days in by_pool.values():
        ds = sorted(days)
        vals = [days[d] / amount_usd * 100 for d in ds]
        day += vals
        dates = [datetime.fromisoformat(d + "T00:00:00+00:00") for d in ds]
        for i in range(len(ds) - 6):
            if (dates[i + 6] - dates[i]).days == 6:
                week.append(sum(vals[i:i + 7]))
        cum, worst = 0.0, 0.0
        for v in vals:
            cum += v
            worst = min(worst, cum)
        start.append(worst)
        spans.append(len(ds))
    return {"day": _round(day), "week": _round(week), "since_start": _round(start), "pools": len(by_pool),
            "since_start_days_median": _median([float(x) for x in spans]),
            "calendar_days": len({x["day"] for x in base_rows})}


def stage1_table(stage1: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """2: 段階1 の線（−20/−30/−40/−50%）ごとに、出た回数と、そのあとどうなったか。全部と、大きいプールだけ。"""
    def part(evs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(evs)
        w = [e for e in evs if e.get("funds_after_24h_pct") is not None]
        return {"count": n,
                "recovered_6h_share": sum(1 for e in evs if e["recovered_6h"]) / n if n else None,
                "worse_24h_share": sum(1 for e in w if e["funds_after_24h_pct"] < 0) / len(w) if w else None,
                "worse_24h_known": len(w),
                "big_drop_share": sum(1 for e in evs if e["big_drop"]) / n if n else None,
                # 空振りらしい: 6時間のうちに戻り、そのあとコインも大きく下がらなかった
                "false_alarm_share": sum(1 for e in evs if e["recovered_6h"] and not e["big_drop"]) / n if n else None}
    return [{"threshold_pct": th, "all": part(evs), "big": part([e for e in evs if not e["small"]])}
            for th, evs in sorted(stage1.items())]


def below_target_events(scores: list[tuple[int, sqlite3.Row]], target_apr_pct: float, times: int) -> list[dict[str, Any]]:
    """3: 狙い利回り以下が times 回続いた合図（risk_job.count_below_target と同じ比べ方。年 = 1日の純利回り × 365）。
    前に狙い以上だったプールで、下がったときだけ数える（入っている建玉にだけ働く決まりのため）。
    そのあと6時間・24時間のうちに、また狙い以上に戻ったか。"""
    seq = [(t, float(r["net_daily_pct"])) for t, r in scores if r["net_daily_pct"] is not None]
    out = []
    above, n = False, 0
    for i, (t, net) in enumerate(seq):
        if net * 365 >= target_apr_pct:
            above, n = True, 0
            continue
        n += 1
        if n == times and above:
            later = [(tt, v) for tt, v in seq[i + 1:] if tt <= t + 86400]
            known_24h = bool(later) and later[-1][0] >= t + 86400 - 3600
            out.append({"at": t, "apr_pct": net * 365,
                        "back_6h": any(v * 365 >= target_apr_pct for tt, v in later if tt <= t + 6 * 3600),
                        "back_24h": any(v * 365 >= target_apr_pct for _, v in later) if known_24h else None})
            above = False
    return out


def _next_day_pnl(evs: list[dict[str, Any]], base_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """3: 狙い以下3回で出たとして、そのプールの次の日（UTC）の「今のやり方」の実際の損益（ドル）。
    マイナス = 出て避けた損、プラス = 出て逃した分。"""
    now = {(x["pool_id"], x["day"]): x["now"] for x in base_rows}
    vals = []
    for e in evs:
        nxt = datetime.fromtimestamp(e["at"], UTC).date().toordinal() + 1
        v = now.get((e["pool_id"], datetime.fromordinal(nxt).strftime("%Y-%m-%d")))
        if v is not None:
            vals.append(v)
    neg, pos = [-v for v in vals if v < 0], [v for v in vals if v >= 0]
    return {"known": len(vals), "avoided_n": len(neg), "missed_n": len(pos),
            "avoided_mean_usd": _mean(neg), "missed_mean_usd": _mean(pos)}


def _side_cost(sc: sqlite3.Row, s: BacktestSettings) -> float:
    """移るときの片側（閉じる・始める）の費用（ドル）: 両替する額 ×（手数料 + ずれ）+ ガス代2回（risk_job.better_place と同じ）。"""
    det = _details(sc)
    inp = det.get("inputs") or {}
    lp = float((det.get("split") or {}).get("lp") or s.lp_share)
    return s.amount_usd * lp * s.swap_ratio * (float(inp.get("fee") or 0) + float(inp.get("slippage") or 0)) \
        + 2 * float(inp.get("gas_usd_per_tx") or 0)


def move_sim(info: dict[str, dict[str, dict[str, Any]]], now_usd: dict[tuple[str, str], float],
             flip_days: dict[str, dict[str, float]], s: BacktestSettings, mult: float | None) -> dict[str, Any]:
    """4: 1日ごとに、今のプールより（利回りの差 × 次の切り替えまでの日数）が移る費用の mult 倍より大きいプールがあれば移る。
    mult が None なら移らない。損益はそのプールのその日の「今のやり方」の実際（比べる相手と同じ）。最初の1つは同じ。"""
    days = sorted({d for x in info.values() for d in x})
    held: str | None = None
    pnl, cost, moves, missing, n = 0.0, 0.0, 0, 0, 0
    for day in days:
        cands = {pid: x[day] for pid, x in info.items() if day in x and (pid, day) in now_usd}
        if not cands:
            continue
        good = {pid: c for pid, c in cands.items() if c["net"] * 365 >= s.target_apr_pct}
        if held is None:
            held = max(good or cands, key=lambda k: (good or cands)[k]["net"])
        elif mult is not None and held in cands:
            cur = cands[held]
            alts = sorted(((pid, c) for pid, c in good.items() if pid != held), key=lambda x: -x[1]["net"])
            if alts:
                pid, alt = alts[0]
                gain = (alt["net"] - cur["net"]) / 100 * s.amount_usd * flip_days.get(pid, {}).get(day, 0.0)
                move_cost = cur["side_cost"] + alt["side_cost"]
                if gain > 0 and gain > mult * move_cost:
                    held = pid
                    moves += 1
                    cost += move_cost
        v = now_usd.get((held, day))
        if v is None:
            missing += 1
            continue
        pnl += v
        n += 1
    return {"multiple": mult, "moves": moves, "move_cost_usd": cost, "pnl_before_cost_usd": pnl,
            "pnl_usd": pnl - cost, "days": n, "missing_days": missing}


def grid_days(conn: sqlite3.Connection, pool: sqlite3.Row, s: BacktestSettings) -> list[dict[str, Any]]:
    """5: 境目の余裕 × 待ち時間を変えて、その日のいちばん良い幅で動かした日ごとの結果（今のやり方と同じ作り）。"""
    pts = _points(conn, pool["id"])
    scores = _scores(conn, pool["id"])
    if len(pts) < 96 or not scores:
        return []
    series = [(t, p, liq) for t, p, liq, _ in pts]
    first_day = _day(pts[0][0])
    det0 = _details(_score_at(scores, pts[0][0], 10 ** 9) or scores[0][1])
    c_lp = s.amount_usd * float((det0.get("split") or {}).get("lp") or s.lp_share)
    cost_fn, _ = _cost_fn(det0, pool, s, c_lp)
    runs: dict[tuple[float, float, float], dict[str, DayReplay]] = {}
    out = []
    for day in sorted({_day(t) for t, _, _ in series}):
        if day == first_day:
            continue
        sc = _score_at(scores, int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()))
        if sc is None or sc["best_r"] is None:
            continue
        ranges = {round(float(x["r_pct"]), 6): x for x in _details(sc).get("ranges") or [] if x.get("r_pct") is not None}
        best = round(float(sc["best_r"]), 6)
        inc = _full_income(ranges.get(best))
        if inc is None:
            continue
        for b in GRID_BUFFERS:
            for w in GRID_WAITS_MIN:
                key = (best, b, w)
                if key not in runs:
                    runs[key] = replay(series, best / 100, w * 60, cost_fn, b)
                d = runs[key].get(day)
                if d is None or d.seconds < 86400 * DAY_MIN_COVERAGE:
                    continue
                k = 86400 / d.seconds
                share = d.in_range_seconds / d.seconds
                out.append({"day": day, "buffer": b, "wait_min": w, "rebalances": d.rebalances * k, "flips": d.flips * k,
                            "cost_usd": d.rebalance_cost * k * c_lp, "out_share": 1 - share,
                            "net_usd": inc * share - (d.gamma + d.rebalance_cost) * k * c_lp})
    return out


def grid_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    for x in rows:
        by[(x["buffer"], x["wait_min"])].append(x)
    out = []
    for (b, w), xs in sorted(by.items()):
        out.append({"buffer": b, "wait_min": w, "pool_days": len(xs),
                    "rebalances_day": _mean([x["rebalances"] for x in xs]),
                    "flips_day": _mean([x["flips"] for x in xs]),
                    "flip_share": (sum(x["flips"] for x in xs) / sum(x["rebalances"] for x in xs))
                    if sum(x["rebalances"] for x in xs) else None,
                    "cost_usd_day": _mean([x["cost_usd"] for x in xs]),
                    "out_share": _mean([x["out_share"] for x in xs]),
                    "net_usd_day": _mean([x["net_usd"] for x in xs]),
                    "net_usd_total": sum(x["net_usd"] for x in xs),
                    "calendar_days": len({x["day"] for x in xs})})
    return out


def gas_series(conn: sqlite3.Connection) -> dict[str, Any]:
    """10: 今の版が15分ごとに読んだ Robinhood Chain のガス代（1回の取引。チェーンの gas price × 決めた量 × ETH の値段）。"""
    seen: dict[str, float] = {}
    for ts, dj in conn.execute("SELECT ts, details_json FROM scores WHERE details_json IS NOT NULL ORDER BY ts"):
        if ts in seen:
            continue
        try:
            g = ((json.loads(dj) or {}).get("inputs") or {}).get("gas_usd_per_tx")
        except ValueError:
            continue
        if g is not None:
            seen[ts] = float(g)
    vals = list(seen.values())
    return {"chain": "robinhood", "points": len(vals), "first": min(seen, default=None), "last": max(seen, default=None),
            "median": _median(vals), "p90": _quantile(vals, 0.9), "p99": _quantile(vals, 0.99),
            "max": max(vals) if vals else None, "distinct": len(set(vals))}


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = q * (len(ys) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)


# --- まとめ ----------------------------------------------------------------------------------------------

def run(conn: sqlite3.Connection, config: Config, fconn: sqlite3.Connection | None = None,
        s: BacktestSettings | None = None) -> dict[str, Any]:
    """今の版の写し（conn。読むだけ）で、②〜⑥ を計算する。"""
    s = s or BacktestSettings.from_config(config)
    pools = [p for p in _pools(conn) if p["n"] >= 96]
    venues = sorted({p["venue_id"] for p in pools})
    tok_book = load_tokens(root=config.root)
    reward_tokens: dict[str, str] = {}             # 会場 → 報酬のコイン（確認済みのアドレスだけ）
    for v in venues:
        try:
            rt = (contract_address(load_venue(v, config.root), "reward_token") or "").lower()
        except Exception:
            rt = ""
        if rt:
            reward_tokens[v] = rt
    rewards = frozenset(reward_tokens.values())
    stocks = frozenset(tok_book.stock_tokens)
    rows: list[dict[str, Any]] = []
    fund_rows: list[dict[str, Any]] = []
    per_pool: list[dict[str, Any]] = []
    kinds: dict[str, str] = {}
    still: list[dict[str, Any]] = []
    base_rows: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    move_info: dict[str, dict[str, dict[str, Any]]] = {}
    flip_days: dict[str, dict[str, float]] = {}
    epochs: dict[str, tuple[int, int]] = {}
    for v in venues:
        try:
            ep = (load_venue(v, config.root).get("mechanics") or {}).get("epoch") or {}
            epochs[v] = (int(ep.get("length_seconds") or 0), int(ep.get("offset_seconds") or 0))
        except Exception:
            epochs[v] = (0, 0)
    lend = lending_daily(fconn)
    for p in pools:
        kinds[p["id"]] = kind = pool_kind(p, tok_book.stablecoins, stocks, rewards)
        pts_p = _points(conn, p["id"])
        share = still_share(pts_p)
        if share >= STILL_SHARE:
            # 値段が動いていない（取引がない）プールは、②③④ の比べるのから外す。保険の資金調達料は市場のものなので数える。
            # 同じ名前のプールが会場に複数あるので、プールの住所・手数料の段・最初と最後の値段・違う値段の数も出す
            # （2026-10-04 オーナーの質問1: WETH/USDG が入ったのはなぜか）
            still.append({"pool_id": p["id"], "pair": f"{p['token0_symbol']}/{p['token1_symbol']}", "kind": kind,
                          "points": p["n"], "still_share": share, "fee_tier": p["fee_tier"],
                          **pool_fee(conn, p), **price_edge(pts_p), "gauge": p["gauge_address"],
                          "first_price": pts_p[0][1] if pts_p else None, "last_price": pts_p[-1][1] if pts_p else None,
                          "distinct_prices": len({x[1] for x in pts_p})})
            fund_rows += hedge_days(conn, fconn, p, s)
            continue
        ds = pool_days(conn, p, s)
        rows += [{**x, "pool_id": p["id"], "stock": kind == "stock", "kind": kind} for x in ds]
        bd = baseline_days(conn, p, s, lend)
        base_rows += [{**x, "pool_id": p["id"], "kind": kind} for x in bd]
        grid_rows += grid_days(conn, p, s)
        sc_p = _scores(conn, p["id"])
        below += [{**e, "pool_id": p["id"], "pair": f"{p['token0_symbol']}/{p['token1_symbol']}"}
                  for e in below_target_events(sc_p, s.target_apr_pct, s.below_target_times)]
        length, offset = epochs.get(p["venue_id"], (0, 0))
        for x in bd:
            start = datetime.fromisoformat(x["day"] + "T00:00:00+00:00")
            sc = _score_at(sc_p, int(start.timestamp()))
            if sc is None or sc["net_daily_pct"] is None or (sc["signal"] or "") == "red":
                continue
            move_info.setdefault(p["id"], {})[x["day"]] = {"net": float(sc["net_daily_pct"]), "side_cost": _side_cost(sc, s)}
            if length:
                flip_days.setdefault(p["id"], {})[x["day"]] = (next_epoch_flip(start, length, offset) - start).total_seconds() / 86400
        fund_rows += hedge_days(conn, fconn, p, s)
        best = [x for x in ds if x["best_r_pct"] is not None and abs(x["r_pct"] - x["best_r_pct"]) < 1e-9]
        if best:
            per_pool.append({"pool_id": p["id"], "pair": f"{p['token0_symbol']}/{p['token1_symbol']}",
                             "stock": kind == "stock", "kind": kind, "days": len(best), "r_pct": best[0]["r_pct"],
                             "sigma_pred": _mean([x["sigma_pred"] for x in best]),
                             "sigma_real": _mean([x["sigma_real"] for x in best]),
                             "gamma_pred": _mean([x["gamma_pred"] for x in best]),
                             "gamma_real": _mean([x["gamma_real"] for x in best]),
                             "rebalances_pred": _mean([x["rebalances_pred"] for x in best]),
                             "rebalances_real": _mean([x["rebalances_real"] for x in best]),
                             "cost_pred": _mean([x["cost_pred"] for x in best]),
                             "cost_real": _mean([x["cost_real"] for x in best]),
                             "c_lp": _mean([x["c_lp"] for x in best])})

    # ⑤ 預け金・⑥ 早く出る決まり（会場ごとの値段の並び）
    since = datetime(2000, 1, 1, tzinfo=UTC)
    usd: dict[str, list[tuple[int, float]]] = {}
    for v in venues:
        series, _ = own_series(conn, v, tok_book.stablecoins, since)
        for tok, pts in series.items():
            usd.setdefault(tok, pts)
    symbols = {}
    for p in pools:
        symbols[p["token0"].lower()] = p["token0_symbol"]
        symbols[p["token1"].lower()] = p["token1_symbol"]
    hedged = sorted({sym for x in fund_rows for sym in [x["symbol"]]})
    margins = []
    for tok, sym in sorted(symbols.items(), key=lambda x: x[1] or ""):
        if sym not in hedged or tok not in usd:
            continue
        mr = max_rise(usd[tok], s.stay_days * 86400)
        if mr:
            margins.append({"symbol": sym, **mr, "withstand_pct": s.withstand_rise_pct,
                            "enough": mr["rise_pct"] < s.withstand_rise_pct})

    stage1 = {th: [] for th in FUNDS_THRESHOLDS}
    for p in pools:
        pts = _points(conn, p["id"])
        f = pool_funds(pts, usd, p)
        chs = [(t, now, now / ago - 1) for t, now, ago in f if ago > 0]
        prices = [(t, pr) for t, pr, _, _ in pts]
        coins = [usd.get(tk, []) for tk in (p["token0"].lower(), p["token1"].lower()) if tk not in tok_book.stablecoins]
        for th in FUNDS_THRESHOLDS:
            for e in events(chs, lambda ch, th=th: ch * 100 <= -th, [(t, now) for t, now, _ in f]):
                e["pool_id"] = p["id"]
                e["pair"] = f"{p['token0_symbol']}/{p['token1_symbol']}"
                e["kind"] = kinds.get(p["id"])
                e["price_after_24h_pct"] = None
                pr0 = _at_or_before(prices, e["at"])
                pr24 = _at_or_before(prices, e["at"] + 86400, 3600)
                if pr0 and pr24:
                    e["price_after_24h_pct"] = (pr24 / pr0 - 1) * 100
                stage1[th].append({**e, **stage1_detail(e["at"], f, coins)})

    reward = []
    for v in venues:
        rt = reward_tokens.get(v, "")
        if not rt or rt not in usd:
            continue
        chs = changes(usd[rt], 24)
        by_th = {th: events(chs, lambda ch, th=th: ch * 100 <= th, usd[rt]) for th in REWARD_THRESHOLDS}
        reward.append({"venue": v, "symbol": symbols.get(rt, rt[:8]), "points": len(usd[rt]),
                       "events": {str(th): e for th, e in by_th.items()}})

    dumps = []
    for tok, sym in sorted(symbols.items(), key=lambda x: x[1] or ""):
        if tok in tok_book.stablecoins or tok not in usd:
            continue
        e1 = events(changes(usd[tok], 1), lambda ch: ch * 100 <= s.dump_1h_pct, usd[tok])
        e24 = events(changes(usd[tok], 24), lambda ch: ch * 100 <= s.dump_24h_pct, usd[tok])
        if e1 or e24:
            dumps.append({"symbol": sym, "token": tok, "events_1h": e1, "events_24h": e24})

    return {
        "version": VERSION,
        "settings": s.__dict__,
        "pools": len(pools), "pool_days": len({(x["pool_id"], x["day"]) for x in rows}),
        "days": sorted({x["day"] for x in rows}),
        "ranges": {"all": _summarize_ranges(rows, s),
                   "stock": _summarize_ranges([x for x in rows if x["stock"]], s),
                   "crypto": _summarize_ranges([x for x in rows if not x["stock"]], s),
                   **{f"kind_{k}": _summarize_ranges([x for x in rows if x["kind"] == k], s) for k in KINDS}},
        "kinds": {k: sum(1 for v in kinds.values() if v == k) for k in KINDS},
        "per_pool_best": per_pool,
        "still_pools": same_pair(still, pools, conn),
        "still_share_line": STILL_SHARE,
        # 見込みと実際のずれが大きいプール（1日の損 = 値動きの損 + 置き直しの費用 の、見込みと実際の差のドル。大きい順に10）。
        # 前は割合（見込み ÷ 実際）の順で、値動きの小さいプールが上に来ていた（2026-10-04 オーナーの質問5）
        "misses": sorted([miss(x) for x in per_pool if x["gamma_pred"] is not None and x["gamma_real"] is not None],
                         key=lambda x: -x["gap_usd_day"])[:10],
        "spy": [x for x in per_pool if "SPY" in x["pair"].upper()],
        "funding": _summarize_funding(fund_rows, s),
        "baselines": {"lending": _lending_info(), "wide_r_pct": max(s.ranges_pct),
                      "all": _summarize_baselines(base_rows),
                      **{f"kind_{k}": _summarize_baselines([x for x in base_rows if x["kind"] == k]) for k in KINDS}},
        "margins": margins,
        "margins_long": margins_long(fconn, hedge_markets(tok_book, fund_rows), s),
        "stage1": {"threshold_pct": s.funds_drop_pct,
                   "counts": {str(th): len(e) for th, e in stage1.items()},
                   "counts_big_pools": {str(th): sum(1 for x in e if not x["small"]) for th, e in stage1.items()},
                   "breakdown": stage1_breakdown(stage1.get(int(s.funds_drop_pct), [])),
                   "breakdown_big_pools": stage1_breakdown([x for x in stage1.get(int(s.funds_drop_pct), [])
                                                            if not x["small"]]),
                   "big_pool_events": [x for x in stage1.get(int(s.funds_drop_pct), []) if not x["small"]][:20],
                   "events": stage1.get(int(s.funds_drop_pct), [])[:50]},
        "stage2_reward": {"threshold_pct": s.reward_drop_pct, "tokens": reward},
        "stage2_dump": {"threshold_1h_pct": s.dump_1h_pct, "threshold_24h_pct": s.dump_24h_pct, "tokens": dumps},
        # N5 最終判断の準備（材料。決めない）
        "prep": {
            "loss": loss_series(base_rows, s.amount_usd),
            "stage1": stage1_table(stage1),
            "below_target": {"target_apr_pct": s.target_apr_pct, "times": s.below_target_times, "events": below,
                             "next_day": _next_day_pnl(below, base_rows)},
            "moves": {"multiples": list(MOVE_MULTIPLES),
                      "runs": [move_sim(move_info, {(x["pool_id"], x["day"]): x["now"] for x in base_rows}, flip_days, s, k)
                               for k in (None, *MOVE_MULTIPLES)],
                      "pools": len(move_info), "with_flip": len(flip_days)},
            "grid": {"buffers": list(GRID_BUFFERS), "waits_min": list(GRID_WAITS_MIN), "flip_s": FLIP_S,
                     "rows": grid_summary(grid_rows)},
            "gas": gas_series(conn),
        },
    }


# --- 写しの結果をとっておく（計算は数秒〜数十秒。写しが変わるか、計算を変えたら作り直す） ---------------------

def _funding_rows(feeds_db: Path | None) -> int | None:
    """Lighter の記録の数（資金調達率と値段）と、比べる相手の貸し出しの記録の数。1日1回増える。増えたら作り直す。"""
    if feeds_db is None or not feeds_db.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{feeds_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            n = c.execute("SELECT COUNT(*) FROM lighter_funding_history").fetchone()[0]
            from .feeds.trial import LENDING_BASELINE
            for sql, args in (("SELECT COUNT(*) FROM lighter_price_history", ()),
                              ("SELECT COUNT(*) FROM llama_yield_history WHERE pool=?", (LENDING_BASELINE["pool"],))):
                try:
                    n += c.execute(sql, args).fetchone()[0]
                except sqlite3.OperationalError:
                    pass
            return n
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _source_mark() -> str:
    """このファイルの中身の印。行を足したのに VERSION を上げ忘れても、とっておいた古い結果を使わないため
    （2026-10-04: still_pools の欄を足したのに古い結果が出て、パソコンの表示が空欄になった）。"""
    import hashlib
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def cached(data_dir: Path, config: Config, feeds_db: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    from . import trial_records
    copy = data_dir / trial_records.OLD_COPY
    if not copy.exists():
        return {"present": False, "text": "今の版のデータの写しがないので、さかのぼりの計算はまだできません。"}
    manifest_path = data_dir / trial_records.OLD_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    key = {"version": VERSION, "source": _source_mark(), "copied_at": manifest.get("copied_at"), "bytes": copy.stat().st_size,
           "funding_rows": _funding_rows(feeds_db)}
    out_path = data_dir / "import" / "backtest.json"
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            if old.get("key") == key:
                return old
        except ValueError:
            pass
    conn = trial_records.open_old_copy(data_dir)
    assert conn is not None
    fconn = None
    if feeds_db is not None and feeds_db.exists():
        fconn = sqlite3.connect(f"file:{feeds_db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        res = run(conn, config, fconn)
    finally:
        conn.close()
        if fconn is not None:
            fconn.close()
    out = {"present": True, "key": key, "computed_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
           "result": res}
    try:
        out_path.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass
    return out


__all__ = ["BacktestSettings", "Segment", "replay", "run", "cached", "changes", "events", "max_rise",
           "funding_actual", "lp_value"]
