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

VERSION = 4                     # 計算を変えたら上げる（とっておいた結果を使わない）
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


def replay(points: list[tuple[int, float, float | None]], r: float, wait_s: float,
           cost_of: Any = None) -> dict[str, DayReplay]:
    """値段の並び (UNIX秒, 値段, 今の値段のところの流動性) で、幅 ±r を置き直しながら動かす。日ごと（UTC）の結果。

    cost_of(値段, 流動性) は置き直し1回の (費用の割合, 両替のずれ) を返す（None なら0）。"""
    out: dict[str, DayReplay] = defaultdict(DayReplay)
    if len(points) < 2:
        return {}
    seg = Segment.at(points[0][1], r)
    realized = 0.0
    prev_total = 0.0
    out_since: int | None = None
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
        if seg.inside(p):
            out_since = None
            continue
        if out_since is None:
            out_since = t
        if t - out_since >= wait_s:
            realized = total
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

    @classmethod
    def from_config(cls, config: Config) -> BacktestSettings:
        sc, rk, op = config.scoring, config.risk, config.opportunities
        return cls(amount_usd=float(sc.total_capital_usd), wait_minutes=float(sc.rebalance_wait_minutes),
                   swap_ratio=float(sc.swap_ratio), ranges_pct=tuple(sc.ranges_pct),
                   withstand_rise_pct=float(op.hedge_withstand_rise_pct), stay_days=float(op.stay_days),
                   funds_drop_pct=float(rk.emergency_pool_funds_drop_1h_pct),
                   reward_drop_pct=float(rk.exit_reward_token_24h_pct),
                   dump_1h_pct=float(rk.exit_dump_1h_pct), dump_24h_pct=float(rk.exit_dump_24h_pct))


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
    """合図が出た時点（同じ出来事は EVENT_GAP_S あいたら別）と、そのあと24時間の値段の動き。"""
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
        out.append({"at": t, "price": p, "change_pct": ch * 100,
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
    drops = []
    for pts in coins:
        p0 = _at_or_before(pts, t)
        nxt = [pp for tt, pp in pts if t < tt <= t + 86400]
        if p0 and nxt:
            drops.append((min(nxt) / p0 - 1) * 100)
    worst = min(drops) if drops else None
    return {"funds_before_usd": before, "small": bool(before is not None and before < SMALL_POOL_USD),
            "recovered_6h": recovered, "coin_min_24h_pct": worst,
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
                          "first_price": pts_p[0][1] if pts_p else None, "last_price": pts_p[-1][1] if pts_p else None,
                          "distinct_prices": len({x[1] for x in pts_p})})
            fund_rows += hedge_days(conn, fconn, p, s)
            continue
        ds = pool_days(conn, p, s)
        rows += [{**x, "pool_id": p["id"], "stock": kind == "stock", "kind": kind} for x in ds]
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
        "still_pools": still,
        "still_share_line": STILL_SHARE,
        # 見込みと実際のずれが大きいプール（1日の損 = 値動きの損 + 置き直しの費用 の、見込みと実際の差のドル。大きい順に10）。
        # 前は割合（見込み ÷ 実際）の順で、値動きの小さいプールが上に来ていた（2026-10-04 オーナーの質問5）
        "misses": sorted([miss(x) for x in per_pool if x["gamma_pred"] is not None and x["gamma_real"] is not None],
                         key=lambda x: -x["gap_usd_day"])[:10],
        "spy": [x for x in per_pool if "SPY" in x["pair"].upper()],
        "funding": _summarize_funding(fund_rows, s),
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
    }


# --- 写しの結果をとっておく（計算は数秒〜数十秒。写しが変わるか、計算を変えたら作り直す） ---------------------

def _funding_rows(feeds_db: Path | None) -> int | None:
    """Lighter の記録の数（資金調達率と値段。1日1回増える。増えたら資金調達料・預け金の比べ方を作り直す）。"""
    if feeds_db is None or not feeds_db.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{feeds_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            n = c.execute("SELECT COUNT(*) FROM lighter_funding_history").fetchone()[0]
            try:
                n += c.execute("SELECT COUNT(*) FROM lighter_price_history").fetchone()[0]
            except sqlite3.OperationalError:
                pass
            return n
        finally:
            c.close()
    except sqlite3.Error:
        return None


def cached(data_dir: Path, config: Config, feeds_db: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    from . import trial_records
    copy = data_dir / trial_records.OLD_COPY
    if not copy.exists():
        return {"present": False, "text": "今の版のデータの写しがないので、さかのぼりの計算はまだできません。"}
    manifest_path = data_dir / trial_records.OLD_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    key = {"version": VERSION, "copied_at": manifest.get("copied_at"), "bytes": copy.stat().st_size,
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
