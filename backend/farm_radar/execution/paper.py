"""練習（ペーパートレード）の建玉を、本物のデータで動かす（SPEC 7.4章・12.1章。M5a）。

お金は動かさない。ウォレットも秘密鍵も使わない。15分ごとの記録（実際のプール価格・報酬の毎秒量・ステーク流動性）
から「この建玉を持っていたらどうなったか」を計算し、positions / position_pnl / ledger 表に書く。

損益の6区分（SPEC 7.6章。すべて「開いてからの累計」で持ち、行ごとに差分を書く）:
- 収入: 受け取った報酬トークン × 受け取った時の値段（ステーク）/ 手数料の推定（ステークしない）
- 方向: 開いた時のトークンの量をそのまま持っていた場合の値動き = 開いた時の量 × 今の値段 − LPに入れた額
- ガンマ: LPの今の中身の価値 − 開いた時の量をそのまま持っていた場合の価値（v3 の式）
- ヘッジ: perp の仮想の売りの損益 − 資金調達の支払い − 取引手数料
- 報酬トークン値下がり: 持っている報酬トークン ×（今の値段 − 受け取った時の値段）（SPEC 7.6章 6.）
- その他: 両替の手数料とずれ、ガス代（開く時・閉じる時。受け取りのガス代は1日1回分）
参考として「受け取ってから reward_sell_hours 時間で売った場合」の値下がりも記録する（M4 の参考値と比べるため）。

注意（推定の部分。画面にも書く）:
- 受け取りは15分ごとに記録する（値段を細かく付けるため）。ガス代は1日1回受け取る前提で数える。
- perp の値段は、プールから出したドル価格で代用する（Lighter の値段との差は小さいとみなす）。
- ステークしない建玉の手数料は、スコア計算の「1日の手数料」（GeckoTerminal の取引量 × 手数料率。
  取引量が不自然に多ければ0）から取り分を推定する。
- 置き直し（リバランス）と離脱は M5b（risk_job.py が危険判定のルールで決めて、ここの rebalance / close_position を呼ぶ）。
  置き直した後も「開いてからの累計」が続くように、それまでの方向・ガンマ・ヘッジの損益を state の off に固定し、
  新しいレンジの中身（base）から先を足していく。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Config, ConfigError, load_venue, practice_allowed
from ..fx import Frankfurter, rate_for
from ..scoring.prices import PoolPrice, usd_prices
from ..tokens import TokenBook
from .base import ClaimResult, CloseResult, HedgeResult, PositionRef, SwapResult

log = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

REWARD_DECIMALS = 18
CATS = ("income", "direction", "gamma", "hedge", "haircut", "other")


class PaperError(Exception):
    """練習の建玉を作れない・閉じられない（理由はオーナー向けの日本語）。"""


def lp_amounts(liquidity: float, price: float, lower: float, upper: float, d0: int, d1: int) -> tuple[float, float]:
    """v3 の式で、流動性 L を [lower, upper] に置いたときの、今の価格でのトークンの量（小数点調整済み）。"""
    k = 10 ** (d1 - d0)
    sa, sb = math.sqrt(lower * k), math.sqrt(upper * k)
    sp = min(max(math.sqrt(price * k), sa), sb)
    x_raw = liquidity * (1 / sp - 1 / sb)
    y_raw = liquidity * (sp - sa)
    return x_raw / 10 ** d0, y_raw / 10 ** d1


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class Market:
    """記録（pool_snapshots）から、ある時点のプールの状態とトークンのドル価格を読む。"""
    conn: sqlite3.Connection
    stables: frozenset[str]
    _prices: dict[int, dict[str, float]] = field(default_factory=dict)

    def pool(self, pool_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT p.*, v.name AS venue_name FROM pools p LEFT JOIN venues v ON v.id = p.venue_id WHERE p.id=?",
            (pool_id,)).fetchone()
        if row is None:
            raise PaperError("このプールは見つかりません。")
        return row

    def latest(self, pool_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pool_snapshots WHERE pool_id=? AND price IS NOT NULL "
                                 "ORDER BY ts DESC LIMIT 1", (pool_id,)).fetchone()

    def after(self, pool_id: str, since: str, until: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM pool_snapshots WHERE pool_id=? AND ts>? AND price IS NOT NULL"
        args: list[Any] = [pool_id, since]
        if until:
            q += " AND ts<=?"
            args.append(until)
        return self.conn.execute(q + " ORDER BY ts", args).fetchall()

    def prices(self, run_id: int) -> dict[str, float]:
        """その回の記録のすべてのプールから出したドル価格（{トークン(小文字): ドル}）。"""
        if run_id not in self._prices:
            rows = self.conn.execute(
                """SELECT s.price, s.liquidity_total, s.sqrt_price_x96, p.token0, p.token1, p.token0_decimals,
                          p.token1_decimals FROM pool_snapshots s JOIN pools p ON p.id = s.pool_id
                   WHERE s.run_id=? AND s.price IS NOT NULL""", (run_id,)).fetchall()
            pps = [PoolPrice(r["token0"], r["token1"], int(r["token0_decimals"]), int(r["token1_decimals"]),
                             float(r["price"]), int(r["liquidity_total"] or 0), int(r["sqrt_price_x96"] or 0))
                   for r in rows if r["token0_decimals"] is not None and r["sqrt_price_x96"]]
            self._prices[run_id] = usd_prices(pps, self.stables)
        return self._prices[run_id]


def latest_score(conn: sqlite3.Connection, pool_id: str, at: str | None = None) -> sqlite3.Row | None:
    q = "SELECT * FROM scores WHERE pool_id=?"
    args: list[Any] = [pool_id]
    if at:
        q += " AND ts<=?"
        args.append(at)
    return conn.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()


WEEKDAY_JA = "月火水木金土日"


# 参考の練習（2026-10-01 オーナー提案【3】案B）。合否に使う建玉は、この印がないもの
REFERENCE = "reference"


def official_sql(alias: str = "") -> str:
    """SQL の条件: 合否に使う建玉（参考の練習でない）。alias は表の別名（"p." など）。"""
    return f"COALESCE({alias}purpose, '') <> '{REFERENCE}'"


OFFICIAL_SQL = official_sql()


def is_reference(pos: sqlite3.Row | dict[str, Any]) -> bool:
    try:
        return (pos["purpose"] or "") == REFERENCE
    except (IndexError, KeyError):
        return False


def running_evaluation_end(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    """進行中の2週間の評価の終わり（なければ None）。途中でやめた評価と、終わった評価は含めない。"""
    row = conn.execute("SELECT ends_at, status FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["status"] != "running":
        return None
    end = datetime.fromisoformat(row["ends_at"])
    return end if now < end else None


def interrupt_evaluation_if_empty(conn: sqlite3.Connection, now: datetime, last_pair: str, reason: str) -> bool:
    """評価の建玉が全部閉じたら、評価を「中断」にする（2026-10-01 オーナー決定 C。合否は出さない）。

    閉じた理由は問わない（緊急離脱・離脱のルール・オーナーが閉じた）。1つでも残っていれば評価はそのまま続く。
    """
    row = conn.execute("SELECT id, status, ends_at FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["status"] != "running" or _iso(now) >= row["ends_at"]:
        return False
    # 参考の練習は評価の建玉に数えない（2026-10-01 案B）
    if conn.execute(f"SELECT 1 FROM positions WHERE is_paper=1 AND status='open' AND {OFFICIAL_SQL} LIMIT 1").fetchone():
        return False
    conn.execute("UPDATE evaluations SET status='interrupted', ends_at=?, note=? WHERE id=?",
                 (_iso(now), json.dumps({"last_pair": last_pair, "reason": reason}, ensure_ascii=False), row["id"]))
    conn.commit()
    log.warning("evaluation interrupted", extra={"data": {"evaluation": row["id"], "last_pair": last_pair,
                                                          "reason": reason}})
    return True


def evaluation_block_message(end: datetime) -> str:
    j = end.astimezone(JST)
    return (f"評価中のため、新しい練習は始められません（評価は {j.month}/{j.day}({WEEKDAY_JA[j.weekday()]}) "
            f"{j:%H:%M} まで）。評価の対象を、始めたときの建玉のまま守るためです。")


def _range_row(details: dict[str, Any], r_pct: float) -> dict[str, Any] | None:
    for x in details.get("ranges") or []:
        if abs(float(x.get("r_pct", -1)) - r_pct) < 1e-9:
            return x
    return None


class PaperExecutor:
    """練習の建玉を作る・閉じる・受け取る（SPEC 12.1章の形）。"""

    def __init__(self, conn: sqlite3.Connection, config: Config, tokens: TokenBook,
                 fx: Frankfurter | None = None, now: datetime | None = None):
        self.conn = conn
        self.config = config
        self.tokens = tokens
        self.fx = fx
        self.now = now or datetime.now(UTC)
        self.market = Market(conn, tokens.stablecoins)

    # --- 上限と状態 -------------------------------------------------------------------------

    def check_can_open(self, venue_id: str, capital: float, reference: bool = False) -> None:
        """上限（config.yaml の limits。変更はオーナーだけ）と、モード・停止の確認。守れなければ PaperError。

        参考の練習（2026-10-01 案B）は、evaluation.reference_outside_limits が true なら、合計と会場ごとの上限の
        計算に入れない（参考の建玉どうしでも、普通の建玉に対しても）。1つの金額の上限と1日の件数の上限は守る。
        """
        if self.config.mode != "paper":
            raise PaperError("今は「見るだけ」モードです。練習するには config.yaml の mode を paper にしてください。")
        st = self.conn.execute("SELECT stopped FROM paper_state WHERE id=1").fetchone()
        if st and st["stopped"]:
            raise PaperError("練習は「停止」中です。再開してから試してください。")
        lim = self.config.limits
        if capital > float(lim.get("position_usd", capital)):
            raise PaperError(f"1つの建玉の上限（${lim['position_usd']:,.0f}）を超えています。")
        outside = self.config.evaluation.reference_outside_limits
        caps = not (reference and outside)
        where = f" AND {OFFICIAL_SQL}" if outside else ""
        open_rows = self.conn.execute("SELECT venue_id, capital FROM positions WHERE is_paper=1 AND status='open'"
                                      + where).fetchall()
        total = sum(r["capital"] for r in open_rows) + capital
        if caps and "total_usd" in lim and total > float(lim["total_usd"]):
            raise PaperError(f"建玉の合計の上限（${lim['total_usd']:,.0f}）を超えます。")
        venue_total = sum(r["capital"] for r in open_rows if r["venue_id"] == venue_id) + capital
        if caps and "per_venue_share" in lim and "total_usd" in lim and \
                venue_total > float(lim["total_usd"]) * float(lim["per_venue_share"]):
            cap = float(lim["total_usd"]) * float(lim["per_venue_share"])
            raise PaperError(f"1つの会場に置ける上限（合計の{float(lim['per_venue_share']) * 100:.0f}% = "
                             f"${cap:,.0f}）を超えます。先にほかの建玉を閉じてください。")
        day_start = (self.now - timedelta(hours=24)).isoformat(timespec="seconds")
        trades = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE is_paper=1 AND (opened_at>=? OR closed_at>=?)",
            (day_start, day_start)).fetchone()[0]
        if "trades_per_day" in lim and trades + 1 > int(lim["trades_per_day"]):
            raise PaperError(f"1日の取引の上限（{lim['trades_per_day']}件）に達しています。")

    def check_not_already_open(self, pool_id: str) -> None:
        """同じプールで練習中の建玉があれば、新しく開かない（2026-09-30 オーナー指示。画面を通さない呼び出しでも重ならない）。"""
        row = self.conn.execute("SELECT id FROM positions WHERE is_paper=1 AND status='open' AND pool_id=? LIMIT 1",
                                (pool_id,)).fetchone()
        if row is not None:
            raise PaperError("このプールはすでに練習中です。同じプールで2つ目の練習は開けません。")

    def check_not_in_evaluation(self) -> None:
        """2週間の評価の間は、新しい練習を始めない（2026-09-30 オーナー決定①。config.yaml の evaluation.block_new_practice）。

        評価の対象を始めたときの建玉のまま守るため。持っている建玉の計算・見張り・置き直しはそのまま続く。
        """
        if not self.config.evaluation.block_new_practice:
            return
        end = running_evaluation_end(self.conn, self.now)
        if end is not None:
            raise PaperError(evaluation_block_message(end))

    def check_reference_room(self) -> None:
        """参考の練習は、同時に evaluation.reference_max_open 個まで。"""
        n = self.conn.execute("SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='open' AND purpose=?",
                              (REFERENCE,)).fetchone()[0]
        mx = self.config.evaluation.reference_max_open
        if n >= mx:
            raise PaperError(f"参考の練習は同時に{mx}つまでです。先にほかの参考の練習を閉じてください。")

    def check_practice_venue(self, venue_id: str, venue_name: str | None = None) -> None:
        """観察だけの会場（会場ファイルの practice: false。M6 の Alandale）では練習を始めない。"""
        try:
            venue = load_venue(venue_id, self.config.root)
        except (OSError, ConfigError):
            raise PaperError("この会場の設定が見つからないので、練習を始められません。") from None
        if not practice_allowed(venue):
            raise PaperError(f"{venue_name or venue.get('name') or venue_id} は観察だけの会場です。"
                             "練習と2週間の評価には入れていません。")

    # --- 開く ---------------------------------------------------------------------------------

    def open_position(self, pool_id: str, capital: float, lower: float | None = None,
                      upper: float | None = None, r_pct: float | None = None,
                      purpose: str | None = None) -> PositionRef:
        """建玉を作る。レンジを指定しなければ、最新のスコアの最適レンジ（±r%）を使う。

        purpose='reference' は参考の練習（2026-10-01 案B）: 評価の間も始められ、評価の合否には使わない。
        """
        if purpose not in (None, REFERENCE):
            raise PaperError("練習の種類が分かりません。")
        reference = purpose == REFERENCE
        pool = self.market.pool(pool_id)
        self.check_practice_venue(pool["venue_id"], pool["venue_name"])
        self.check_not_already_open(pool_id)
        if reference:
            self.check_reference_room()
        else:
            self.check_not_in_evaluation()
        snap = self.market.latest(pool_id)
        score = latest_score(self.conn, pool_id)
        if snap is None or score is None or score["best_r"] is None:
            raise PaperError("このプールはまだ計算できていないので、練習を始められません。")
        self.check_can_open(pool["venue_id"], capital, reference=reference)
        details = json.loads(score["details_json"] or "{}")
        inp = details.get("inputs") or {}
        price = float(snap["price"])
        r_pct = r_pct if r_pct is not None else float(score["best_r"])
        if lower is None or upper is None:
            lower, upper = price * (1 - r_pct / 100), price * (1 + r_pct / 100)
        pred = _range_row(details, r_pct) or {}
        prices = self.market.prices(snap["run_id"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        u0, u1 = prices.get(t0), prices.get(t1)
        if u0 is None or u1 is None:
            raise PaperError("トークンのドル価格が分からないので、練習を始められません。")
        d0, d1 = int(pool["token0_decimals"]), int(pool["token1_decimals"])
        s = self.config.scoring
        c_lp = capital * s.allocation_lp
        liq = _liquidity_for_range(c_lp, price, lower, upper, u0, u1, d0, d1)
        x0, y0 = lp_amounts(liq, price, lower, upper, d0, d1)
        c_lp_open = x0 * u0 + y0 * u1

        # 開く時の費用: 両替（手数料 + ずれ）とガス代2回（スコア計算と同じ値を使う）
        fee = float(inp.get("fee") or (snap["fee"] or 0) / 1e6)
        slip = float(inp.get("slippage") or 0.0)
        gas = float(inp.get("gas_usd_per_tx") or 0.0)
        swap_cost = c_lp * s.swap_ratio * (fee + slip)
        hedges = []
        for tok, amt, usd, sym in ((t0, x0, u0, pool["token0_symbol"]), (t1, y0, u1, pool["token1_symbol"])):
            # ヘッジ先: スコア計算が選んだ一番安いところ（SPEC 5.2.1章）。なければトークンの対応表の1つ目
            chosen = ((inp.get("hedge") or {}).get(sym) or {})
            perp = self.tokens.perp_for(tok)
            if self.tokens.is_stable(tok) or amt <= 0 or (perp is None and not chosen):
                continue
            hedges.append({"token": tok, "symbol": sym,
                           "hedge_id": chosen.get("hedge_id") or perp.venue,
                           "hedge_name": chosen.get("name") or perp.venue.capitalize(),
                           "perp": chosen.get("symbol") or perp.symbol,
                           "market_id": int(chosen["market_id"]) if chosen.get("market_id") is not None else perp.market_id,
                           "size": amt, "entry": usd})
        hedge_fee = sum(h["size"] * h["entry"] * self.taker_fee(h["market_id"], h.get("hedge_id") or "lighter") for h in hedges)
        costs = swap_cost + 2 * gas + hedge_fee

        started_red = 1 if score["signal"] == "red" else 0
        predicted = {
            "score_ts": score["ts"], "signal": score["signal"], "r_pct": r_pct, "mode": pred.get("mode"),
            "net": pred.get("net"), "income": pred.get("income"), "gamma": pred.get("gamma"),
            "rebalance": pred.get("rebalance"), "hedge": pred.get("hedge"), "haircut": pred.get("haircut"),
            "direction_risk": pred.get("direction_risk"), "net_sell_now": pred.get("net_sell_now"),
            "in_range_ratio": pred.get("in_range_ratio"), "c_total": s.total_capital_usd,
        }
        mode = pred.get("mode") or score["mode"] or "staked"
        up_price = self._reward_price(prices, pool["venue_id"])
        state = _new_state(snap, costs, up_price, gas)
        # 始めた費用の内訳（2026-09-29 オーナー追加: 両替のずれがいくら含まれるかを画面に出す）
        state["open_breakdown"] = {
            "swap_usd": c_lp * s.swap_ratio, "fee_pct": fee * 100, "slippage_pct": slip * 100,
            "slippage_source": inp.get("slippage_source"),
            "swap_fee": c_lp * s.swap_ratio * fee, "slippage": c_lp * s.swap_ratio * slip,
            "gas": 2 * gas, "hedge_fee": hedge_fee, "total": costs,
        }
        ts = _iso(self.now)
        # 報酬などは「開いた時刻」から数える（最新の記録が少し前のものでも、その間の分は数えない）
        state["prev"]["ts"] = max(ts, snap["ts"])
        cur = self.conn.execute(
            """INSERT INTO positions(pool_id, is_paper, opened_at, capital, r, lower, upper, status, venue_id, mode,
                 liquidity, amount0, amount1, price_open, usd0_open, usd1_open, c_lp, hedges_json, started_red,
                 signal_open, predicted_json, last_ts, state_json, purpose)
               VALUES (?,1,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pool_id, ts, capital, r_pct / 100, lower, upper, pool["venue_id"], mode, str(liq), x0, y0, price, u0, u1,
             c_lp_open, json.dumps(hedges), started_red, score["signal"], json.dumps(predicted), snap["ts"],
             json.dumps(state), purpose))
        pid = int(cur.lastrowid)
        led = [("deposit", pool["token0_symbol"], x0, u0, "LPに入れる"),
               ("deposit", pool["token1_symbol"], y0, u1, "LPに入れる"),
               ("cost", "USD", swap_cost, 1.0, "両替の手数料とずれ（開く時）"),
               ("cost", "USD", 2 * gas, 1.0, "ガス代（開く時 2回）")]
        led += [("hedge_open", h["perp"], h["size"], h["entry"], "perp の仮想の売り") for h in hedges]
        if hedge_fee:
            led.append(("cost", "USD", hedge_fee, 1.0, "perp の取引手数料"))
        for kind, token, amount, usd, note in led:
            self._ledger(ts, pid, kind, token, amount, usd, note)
        # 開いた時の行（その他 = 開く時の費用）
        self._pnl_row(pid, state["prev"]["ts"], {**{c: 0.0 for c in CATS}, "other": -costs}, 0.0,
                      in_range=1.0 if lower <= price <= upper else 0.0, reward_amount=0.0,
                      value=capital - costs, estimated=False, detail={"event": "open"})
        self.conn.commit()
        log.info("paper position opened", extra={"data": {"position": pid, "pool": pool_id, "r_pct": r_pct,
                                                          "mode": mode, "started_red": bool(started_red)}})
        return PositionRef(pid)

    def _reward_price(self, prices: dict[str, float], venue_id: str | None) -> float | None:
        rt = self._reward_token(venue_id)
        return prices.get(rt) if rt else None

    def _reward_token(self, venue_id: str | None) -> str | None:
        """建玉の会場の報酬トークン（その会場のプールの最新の記録から）。

        2026-10-02 直し（N1）: 前は会場を区別せずに最新の記録を見ていたので、同じ回に up. のあとで Alandale を読むと
        Alandale の報酬トークンになり、UP の値段が前の回のまま使われていた（docs/cases/early-exit-up-2026-10-01.md）。
        """
        row = self.conn.execute(
            "SELECT s.reward_token FROM pool_snapshots s JOIN pools p ON p.id = s.pool_id "
            "WHERE s.reward_token IS NOT NULL AND (? IS NULL OR p.venue_id = ?) ORDER BY s.ts DESC LIMIT 1",
            (venue_id, venue_id)).fetchone()
        return row[0].lower() if row and row[0] else None

    # --- 毎回の計算（15分ごとの記録1つごと） --------------------------------------------------

    def update(self, pos: sqlite3.Row, until: str | None = None) -> int:
        """前回の続きから until までの記録で、損益を計算して position_pnl に書く。書いた行の数を返す。"""
        pool = self.market.pool(pos["pool_id"])
        snaps = self.market.after(pos["pool_id"], pos["last_ts"], until)
        if not snaps:
            return 0
        st = json.loads(pos["state_json"])
        hedges = json.loads(pos["hedges_json"] or "[]")
        liq = float(pos["liquidity"])
        d0, d1 = int(pool["token0_decimals"]), int(pool["token1_decimals"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        s = self.config.scoring
        gap_s = self.config.snapshot_minutes * 60 * 1.5
        sell_s = s.reward_sell_hours * 3600
        rt = self._reward_token(pos["venue_id"] or pool["venue_id"])
        n = 0
        for snap in snaps:
            prev = st["prev"]
            dt = (_ts(snap["ts"]) - _ts(prev["ts"])).total_seconds()
            if dt <= 0:
                continue
            prices = self.market.prices(snap["run_id"])
            u0 = prices.get(t0, st["last_prices"].get(t0))
            u1 = prices.get(t1, st["last_prices"].get(t1))
            up = prices.get(rt, st["last_prices"].get("reward")) if rt else None
            if u0 is None or u1 is None:
                continue
            st["last_prices"] = {t0: u0, t1: u1, "reward": up}
            price = float(snap["price"])
            inside = lambda p: 1.0 if pos["lower"] <= p <= pos["upper"] else 0.0  # noqa: E731
            f = (inside(prev["price"]) + inside(price)) / 2
            reward_amt = 0.0
            if pos["mode"] == "unstaked":
                sc = latest_score(self.conn, pos["pool_id"], snap["ts"])
                fees_day = float((json.loads(sc["details_json"] or "{}").get("inputs") or {}).get("fees_usd_day")
                                 or 0.0) if sc else 0.0
                share = liq / (prev["liq_total"] + liq) if liq > 0 else 0.0
                st["fees_usd"] += fees_day * (1 - prev["unstaked_fee"]) * share * f * dt / 86400
            elif prev["alive"] and up is not None:
                share = liq / (prev["liq_staked"] + liq) if liq > 0 else 0.0
                reward_amt = prev["rate"] / 10 ** REWARD_DECIMALS * dt * share * f
                if reward_amt > 0:
                    st["held"] += reward_amt
                    st["held_value"] += reward_amt * up
                    st["lots"].append([_ts(snap["ts"]).timestamp(), reward_amt, up])
                    self._ledger(snap["ts"], pos["id"], "claim", "UP", reward_amt, up, "報酬の受け取り（記録）")
            st["costs"] += float(st.get("gas", 0.0)) * dt / 86400        # 受け取りのガス代は1日1回分
            for h in hedges:
                rate = self._funding(h["market_id"], _ts(prev["ts"]), h.get("hedge_id") or "lighter")
                if rate is None:
                    st["funding_missing"] = True
                    rate = 0.0
                hp = prices.get(h["token"], h["entry"])
                st["funding_paid"] += h["size"] * hp * rate * dt / 3600
            # 「すぐ売る」場合: 受け取ってから reward_sell_hours たった分を、今の値段で売ったことにする
            now_s = _ts(snap["ts"]).timestamp()
            keep = []
            for lot in st["lots"]:
                if now_s - lot[0] >= sell_s and up is not None:
                    st["sold_haircut"] += lot[1] * (up - lot[2])
                else:
                    keep.append(lot)
            st["lots"] = keep
            cum = _cumulative(pos, st, hedges, price, u0, u1, up, prices, d0, d1)
            delta = {c: cum[c] - st["cum"][c] for c in CATS}
            delta_sell = cum["haircut_sell"] - st["cum"]["haircut_sell"]
            st["cum"] = cum
            estimated = dt > gap_s
            self._pnl_row(pos["id"], snap["ts"], delta, delta_sell, in_range=f, reward_amount=reward_amt,
                          value=pos["capital"] + sum(cum[c] for c in CATS), estimated=estimated,
                          detail={"price": price, "usd0": u0, "usd1": u1, "reward_usd": up,
                                  "dt_s": dt, **({"gap": True} if estimated else {})})
            st["prev"] = _prev_of(snap)
            # レンジの外に出た時刻（置き直しの判定に使う。M5b）
            if pos["lower"] <= price <= pos["upper"]:
                st.pop("out_since", None)
            else:
                st.setdefault("out_since", snap["ts"])
            n += 1
        self.conn.execute("UPDATE positions SET last_ts=?, state_json=? WHERE id=?",
                          (st["prev"]["ts"], json.dumps(st), pos["id"]))
        self.conn.commit()
        return n

    def _funding(self, market_id: int, at: datetime, hedge_id: str = "lighter") -> float | None:
        hour = int(at.timestamp()) // 3600 * 3600
        row = self.conn.execute("SELECT short_rate FROM hedge_funding WHERE hedge_id=? AND market_id=? AND ts<=? AND ts>? "
                                "ORDER BY ts DESC LIMIT 1", (hedge_id, market_id, hour, hour - 3 * 3600)).fetchone()
        if row is None and hedge_id == "lighter":
            # 前の版（M5c まで）の記録
            row = self.conn.execute("SELECT short_rate FROM funding_rates WHERE market_id=? AND ts<=? AND ts>? "
                                    "ORDER BY ts DESC LIMIT 1", (market_id, hour, hour - 3 * 3600)).fetchone()
        return float(row[0]) if row else None

    def claim(self, ref: PositionRef) -> ClaimResult:
        """練習では報酬は15分ごとの計算で受け取ったことにしている。ここでは直近の受け取りを返す。"""
        row = self.conn.execute("SELECT token, amount, price_usd FROM ledger WHERE position_id=? AND kind='claim' "
                                "ORDER BY ts DESC LIMIT 1", (ref.position_id,)).fetchone()
        return ClaimResult(row["token"], row["amount"], row["price_usd"]) if row else ClaimResult("UP", 0.0, 0.0)

    def swap_to_usdg(self, token: str, amount: float, max_slippage: float) -> SwapResult:
        raise NotImplementedError("練習の両替は、閉じる時・置き直す時の中で計算します（_exit_costs）。")

    def hedge_adjust(self, symbol: str, target_size: float) -> HedgeResult:
        raise NotImplementedError("練習のヘッジの量の調整は、置き直す時の中で計算します（rebalance）。")

    def hedge_close(self, symbol: str) -> HedgeResult:
        raise NotImplementedError("ヘッジを閉じるのは close_position の中で行います（付録A 4章の手順3）。")

    # --- 置き直し（M5b） ------------------------------------------------------------------------

    def taker_fee(self, market_id: int | None, hedge_id: str = "lighter") -> float:
        """perp の取引手数料（割合）。Lighter から取った市場ごとの今の値（2日以内）を使い、なければ config の値。

        開く・置き直す・閉じる時の費用に必ず入れる（2026-09-29 オーナー指示。今は0%でも、変わったら自動で反映）。
        """
        if market_id is not None:
            since = _iso(self.now - timedelta(days=2))
            row = self.conn.execute("SELECT taker_pct FROM hedge_fees WHERE hedge_id=? AND market_id=? AND ts>=? "
                                    "AND taker_pct IS NOT NULL", (hedge_id, int(market_id), since)).fetchone()
            if row is None and hedge_id == "lighter":
                row = self.conn.execute("SELECT taker_pct FROM perp_fees WHERE market_id=? AND ts>=?",
                                        (int(market_id), since)).fetchone()
            if row is not None:
                return float(row[0]) / 100
        return self.config.scoring.hedge_taker_fee_pct / 100

    def _costs_now(self, pool_id: str) -> tuple[float, float, float]:
        """最新のスコアの入力から、手数料率・両替のずれ・ガス代（1回）を返す。"""
        score = latest_score(self.conn, pool_id)
        inp = (json.loads(score["details_json"] or "{}").get("inputs") or {}) if score else {}
        return float(inp.get("fee") or 0.0), float(inp.get("slippage") or 0.0), float(inp.get("gas_usd_per_tx") or 0.0)

    def gas_too_high(self, pool_id: str) -> tuple[bool, float]:
        gas = self._costs_now(pool_id)[2]
        return gas > self.config.risk.max_gas_usd_per_tx, gas

    def rebalance(self, ref: PositionRef, r_pct: float | None = None, reason: str = "") -> dict[str, Any]:
        """今の価格を中心に、新しいレンジ（±r%。指定がなければ最新のスコアの最適レンジ）に置き直す。

        費用はスコア計算の置き直しと同じ: 両替（LPの額 × swap_ratio × (手数料 + ずれ)）+ ガス代2回 + perp の手数料。
        """
        pos = self._open_pos(ref.position_id)
        self.update(pos)
        pos = self._open_pos(ref.position_id)
        pool = self.market.pool(pos["pool_id"])
        st = json.loads(pos["state_json"])
        hedges = json.loads(pos["hedges_json"] or "[]")
        snap = self.market.latest(pos["pool_id"])
        price = float(snap["price"])
        prices = self.market.prices(snap["run_id"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        u0, u1 = prices.get(t0, st["last_prices"].get(t0)), prices.get(t1, st["last_prices"].get(t1))
        up = st["last_prices"].get("reward")
        d0, d1 = int(pool["token0_decimals"]), int(pool["token1_decimals"])
        x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"], d0, d1)
        v_lp = x * u0 + y * u1
        score = latest_score(self.conn, pos["pool_id"])
        if r_pct is None:
            r_pct = float(score["best_r"]) if score and score["best_r"] is not None else pos["r"] * 100
        fee, slip, gas = self._costs_now(pos["pool_id"])
        s = self.config.scoring
        swap_cost = v_lp * s.swap_ratio * (fee + slip)
        lower, upper = price * (1 - r_pct / 100), price * (1 + r_pct / 100)
        liq = _liquidity_for_range(v_lp, price, lower, upper, u0, u1, d0, d1)
        a0, a1 = lp_amounts(liq, price, lower, upper, d0, d1)
        # ヘッジ: ここまでの損益を固定し、新しい中身の量に合わせて建て直す
        new_hedges, hedge_fee = [], 0.0
        for h in hedges:
            hp = prices.get(h["token"], h["entry"])
            size = a0 if h["token"] == t0 else a1
            hedge_fee += abs(size - h["size"]) * hp * self.taker_fee(h["market_id"], h.get("hedge_id") or "lighter")
            new_hedges.append({**h, "size": size, "entry": hp})
        cost = swap_cost + 2 * gas + hedge_fee
        cum = _cumulative(pos, st, hedges, price, u0, u1, up, prices, d0, d1)
        st["off"] = {"direction": cum["direction"], "gamma": cum["gamma"],
                     "hedge": cum["hedge"] + st["funding_paid"]}      # 資金調達は funding_paid の累計でそのまま引き続ける
        st["base"] = {"value": a0 * u0 + a1 * u1}
        st["costs"] += cost
        st["rebalances"] = int(st.get("rebalances", 0)) + 1
        st["rebalance_cost"] = float(st.get("rebalance_cost", 0.0)) + cost
        st["rebalance_slippage"] = float(st.get("rebalance_slippage", 0.0)) + v_lp * s.swap_ratio * slip
        st.pop("out_since", None)
        ts = _iso(self.now)
        row_ts = max(ts, _iso(_ts(pos["last_ts"]) + timedelta(seconds=1)))
        self.conn.execute(
            "UPDATE positions SET liquidity=?, lower=?, upper=?, r=?, amount0=?, amount1=?, hedges_json=? WHERE id=?",
            (str(liq), lower, upper, r_pct / 100, a0, a1, json.dumps(new_hedges), pos["id"]))
        pos = self._open_pos(pos["id"])
        cum2 = _cumulative(pos, st, new_hedges, price, u0, u1, up, prices, d0, d1)
        delta = {c: cum2[c] - st["cum"][c] for c in CATS}
        delta_sell = cum2["haircut_sell"] - st["cum"]["haircut_sell"]
        st["cum"] = cum2
        self._pnl_row(pos["id"], row_ts, delta, delta_sell, in_range=1.0, reward_amount=0.0,
                      value=pos["capital"] + sum(cum2[c] for c in CATS), estimated=False,
                      detail={"event": "rebalance", "reason": reason, "r_pct": r_pct})
        note = "置き直し"
        for kind, token, amount, usd, text in (
                ("withdraw", pool["token0_symbol"], x, u0, f"{note}: LPから引き出す"),
                ("withdraw", pool["token1_symbol"], y, u1, f"{note}: LPから引き出す"),
                ("cost", "USD", swap_cost, 1.0, f"{note}: 両替の手数料とずれ"),
                ("cost", "USD", 2 * gas, 1.0, f"{note}: ガス代（2回）"),
                ("deposit", pool["token0_symbol"], a0, u0, f"{note}: 新しいレンジ ±{r_pct:g}% に入れる"),
                ("deposit", pool["token1_symbol"], a1, u1, f"{note}: 新しいレンジ ±{r_pct:g}% に入れる"),
                *[("hedge_adjust", h["perp"], h["size"], h["entry"], f"{note}: perp の売りの量を合わせる")
                  for h in new_hedges]):
            self._ledger(ts, pos["id"], kind, token, amount, usd, text)
        if hedge_fee:
            self._ledger(ts, pos["id"], "cost", "USD", hedge_fee, 1.0, f"{note}: perp の取引手数料")
        self.conn.execute("UPDATE positions SET last_ts=?, state_json=? WHERE id=?", (row_ts, json.dumps(st), pos["id"]))
        self.conn.commit()
        log.info("paper position rebalanced", extra={"data": {"position": pos["id"], "r_pct": r_pct,
                                                              "cost_usd": round(cost, 4)}})
        return {"r_pct": r_pct, "lower": lower, "upper": upper, "cost_usd": cost}

    def _open_pos(self, pid: int) -> sqlite3.Row:
        pos = self.conn.execute("SELECT * FROM positions WHERE id=? AND is_paper=1", (pid,)).fetchone()
        if pos is None:
            raise PaperError("この建玉は見つかりません。")
        if pos["status"] != "open":
            raise PaperError("この建玉はもう閉じています。")
        return pos

    # --- 閉じる（付録A 4章の順番: 1 引き出して受け取る → 2 USDG に両替 → 3 ヘッジを閉じる → 4 記録と通知） ----

    def close_position(self, ref: PositionRef, reason: str = "manual") -> CloseResult:
        pos = self._open_pos(ref.position_id)
        self.update(pos)
        pos = self._open_pos(ref.position_id)
        pool = self.market.pool(pos["pool_id"])
        st = json.loads(pos["state_json"])
        hedges = json.loads(pos["hedges_json"] or "[]")
        snap = self.market.latest(pos["pool_id"])
        price = float(snap["price"])
        prices = self.market.prices(snap["run_id"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        u0, u1 = prices.get(t0, st["last_prices"].get(t0)), prices.get(t1, st["last_prices"].get(t1))
        up = st["last_prices"].get("reward")
        d0, d1 = int(pool["token0_decimals"]), int(pool["token1_decimals"])
        x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"], d0, d1)
        cc = self._close_costs(pos, hedges, prices, x, y, u0, u1)
        slip, gas, max_slip, parts = cc["slippage"], cc["gas"], cc["max_slip"], cc["parts"]
        swap_cost, swap_gas, hedge_fee, close_cost = cc["swap_cost"], cc["swap_gas"], cc["hedge_fee"], cc["total"]
        st["costs"] += close_cost
        st["close_cost"] = close_cost
        cum = _cumulative(pos, st, hedges, price, u0, u1, up, prices, d0, d1)
        delta = {c: cum[c] - st["cum"][c] for c in CATS}
        delta_sell = cum["haircut_sell"] - st["cum"]["haircut_sell"]
        st["cum"] = cum
        st["exit_steps"] = {"swap_parts": parts, "slippage_pct": slip * 100}
        ts = _iso(self.now)
        net = sum(cum[c] for c in CATS)
        row_ts = max(ts, _iso(_ts(pos["last_ts"]) + timedelta(seconds=1)))
        self._pnl_row(pos["id"], row_ts, delta, delta_sell, in_range=None, reward_amount=0.0,
                      value=pos["capital"] + net, estimated=False, detail={"event": "close", "reason": reason})
        split = f"（ずれ{slip * 100:.2f}%が上限{max_slip * 100:g}%を超えるので{parts}回に分けて売る想定）" if parts > 1 else ""
        for kind, token, amount, usd, note in (
                ("withdraw", pool["token0_symbol"], x, u0, "手順1: LPから引き出す"),
                ("withdraw", pool["token1_symbol"], y, u1, "手順1: LPから引き出す"),
                ("cost", "USD", gas, 1.0, "手順1: ガス代（引き出しと受け取り）"),
                *([("sell_reward", "UP", st["held"], up, "手順2: 持っていた報酬トークンを USDG に両替")]
                  if st["held"] > 0 and up else []),
                ("cost", "USD", swap_cost, 1.0, f"手順2: 両替の手数料とずれ{split}"),
                ("cost", "USD", swap_gas, 1.0, f"手順2: ガス代（両替 {parts}回）"),
                *[("hedge_close", h["perp"], h["size"], prices.get(h["token"], h["entry"]), "手順3: perp の仮想の売りを閉じる")
                  for h in hedges]):
            self._ledger(ts, pos["id"], kind, token, amount, usd, note)
        if hedge_fee:
            self._ledger(ts, pos["id"], "cost", "USD", hedge_fee, 1.0, "手順3: perp の取引手数料")
        self.conn.execute("UPDATE positions SET status='closed', closed_at=?, close_reason=?, last_ts=?, state_json=? "
                          "WHERE id=?", (ts, reason, row_ts, json.dumps(st), pos["id"]))
        self.conn.commit()
        interrupt_evaluation_if_empty(self.conn, self.now, f"{pool['token0_symbol']}/{pool['token1_symbol']}", reason)
        log.info("paper position closed", extra={"data": {"position": pos["id"], "reason": reason,
                                                          "net_usd": round(net, 2), "swap_parts": parts}})
        return CloseResult(pos["id"], net)

    def _close_costs(self, pos: sqlite3.Row, hedges: list[dict[str, Any]], prices: dict[str, float],
                     x: float, y: float, u0: float, u1: float) -> dict[str, Any]:
        """閉じる時の費用: 両替（値動きする側だけ。ずれが上限を超えるなら分けて売る）+ ガス代 + perp の手数料。"""
        pool = self.market.pool(pos["pool_id"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        fee, slip, gas = self._costs_now(pos["pool_id"])
        volatile = (0.0 if self.tokens.is_stable(t0) else x * u0) + (0.0 if self.tokens.is_stable(t1) else y * u1)
        # 手順2: ずれが上限（max_swap_slippage_pct）を超えるなら、分けて売る（1回あたりのずれが上限に収まる回数）
        max_slip = self.config.risk.max_swap_slippage_pct / 100
        parts = max(1, math.ceil(slip / max_slip)) if max_slip > 0 and slip > max_slip else 1
        swap_cost = volatile * (fee + slip / parts)
        swap_gas = gas * parts
        hedge_fee = sum(h["size"] * prices.get(h["token"], h["entry"])
                        * self.taker_fee(h["market_id"], h.get("hedge_id") or "lighter") for h in hedges)
        return {"slippage": slip, "gas": gas, "max_slip": max_slip, "parts": parts, "swap_cost": swap_cost,
                "swap_gas": swap_gas, "hedge_fee": hedge_fee, "total": swap_cost + gas + swap_gas + hedge_fee}

    def estimate_close_cost(self, ref: PositionRef) -> float:
        """今閉じたらかかる費用の見込み（閉じない。ボーナスが減ったときの比べ方に使う。2026-09-30 オーナー決定③）。"""
        pos = self._open_pos(ref.position_id)
        pool = self.market.pool(pos["pool_id"])
        st = json.loads(pos["state_json"] or "{}")
        hedges = json.loads(pos["hedges_json"] or "[]")
        snap = self.market.latest(pos["pool_id"])
        price = float(snap["price"])
        prices = self.market.prices(snap["run_id"])
        t0, t1 = pool["token0"].lower(), pool["token1"].lower()
        last = st.get("last_prices") or {}
        u0, u1 = prices.get(t0, last.get(t0)), prices.get(t1, last.get(t1))
        x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"],
                          int(pool["token0_decimals"]), int(pool["token1_decimals"]))
        return float(self._close_costs(pos, hedges, prices, x, y, u0 or 0.0, u1 or 0.0)["total"])

    # --- 書き込み -------------------------------------------------------------------------------

    def _ledger(self, ts: str, pid: int, kind: str, token: str, amount: float, usd: float | None, note: str) -> None:
        fx = rate_for(self.conn, _ts(ts), self.fx) if self.fx is not False else None
        value = amount * usd if usd is not None else None
        self.conn.execute(
            """INSERT INTO ledger(ts, is_paper, tx_hash, kind, token, amount, price_usd, price_jpy, position_id, note,
                 value_usd, fx_rate, fx_date) VALUES (?,1,NULL,?,?,?,?,?,?,?,?,?,?)""",
            (ts, kind, token, amount, usd, usd * fx.jpy_per_usd if (fx and usd is not None) else None, pid, note,
             value, fx.jpy_per_usd if fx else None, fx.rate_date if fx else None))

    def _pnl_row(self, pid: int, ts: str, delta: dict[str, float], delta_sell: float, *, in_range: float | None,
                 reward_amount: float, value: float, estimated: bool, detail: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO position_pnl(position_id, ts, income, direction, gamma, hedge, other, net,
                 is_estimated, haircut, haircut_sell, in_range, reward_amount, value_usd, detail_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, ts, delta["income"], delta["direction"], delta["gamma"], delta["hedge"], delta["other"],
             sum(delta[c] for c in CATS), int(estimated), delta["haircut"], delta_sell, in_range, reward_amount,
             value, json.dumps(detail)))


def _liquidity_for_range(c_lp: float, price: float, lower: float, upper: float, u0: float, u1: float,
                         d0: int, d1: int) -> float:
    x, y = lp_amounts(1.0, price, lower, upper, d0, d1)
    per_l = x * u0 + y * u1
    return c_lp / per_l if per_l > 0 else 0.0


def _prev_of(snap: sqlite3.Row) -> dict[str, Any]:
    return {"ts": snap["ts"], "price": float(snap["price"]),
            "rate": float(int(snap["reward_rate_effective_raw"] or 0)),
            "liq_staked": float(int(snap["liquidity_staked_inrange"] or 0)),
            "liq_total": float(int(snap["liquidity_total"] or 0)),
            "alive": snap["gauge_alive"] is None or bool(snap["gauge_alive"]),
            "unstaked_fee": (snap["unstaked_fee"] or 0) / 1e6}


def _new_state(snap: sqlite3.Row, costs: float, up_price: float | None, gas: float) -> dict[str, Any]:
    return {"prev": _prev_of(snap), "held": 0.0, "held_value": 0.0, "lots": [], "sold_haircut": 0.0,
            "fees_usd": 0.0, "funding_paid": 0.0, "costs": costs, "gas": gas, "funding_missing": False,
            "last_prices": {"reward": up_price},
            "cum": {**{c: 0.0 for c in CATS}, "other": -costs, "haircut_sell": 0.0}}


def _cumulative(pos: sqlite3.Row, st: dict[str, Any], hedges: list[dict[str, Any]], price: float, u0: float,
                u1: float, up: float | None, prices: dict[str, float], d0: int, d1: int) -> dict[str, float]:
    x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"], d0, d1)
    v_lp = x * u0 + y * u1
    # 置き直した後は、置き直した時の中身（base）からの変化に、それまでの分（off）を足す
    base = st.get("base") or {"value": pos["c_lp"]}
    off = st.get("off") or {}
    v_hold = pos["amount0"] * u0 + pos["amount1"] * u1
    hedge_pnl = sum(-h["size"] * (prices.get(h["token"], h["entry"]) - h["entry"]) for h in hedges)
    up_now = up if up is not None else 0.0
    haircut = st["held"] * up_now - st["held_value"]
    sell = st["sold_haircut"] + sum(lot[1] * (up_now - lot[2]) for lot in st["lots"])
    return {
        "income": st["held_value"] + st["fees_usd"],
        "direction": off.get("direction", 0.0) + v_hold - base["value"],
        "gamma": off.get("gamma", 0.0) + v_lp - v_hold,
        "hedge": off.get("hedge", 0.0) + hedge_pnl - st["funding_paid"],
        "haircut": haircut,
        "other": -st["costs"],
        "haircut_sell": sell,
    }
