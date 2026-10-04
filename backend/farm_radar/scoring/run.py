"""スコア計算の実行（1時間ごと。SPEC 3章・4章・5.1章）。

1. 最新の収集回のスナップショットを読む
2. オンチェーンのプール価格から各トークンのドル価格を出す（ステーブルコイン = $1 から順にたどる）
3. 値動き σ を計算する（自分の記録が7日分あれば自分の記録、なければ GeckoTerminal の1時間足で補う）
4. 出来高と置かれている額（GeckoTerminal）、ヘッジの資金調達率（Lighter）、ガス代（RPC）を集める
5. プールごとに 3.2章の計算をして、4章の判定と理由文を付けて scores 表に保存する
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import ScoringSettings, contract_address, mechanic_value
from ..db import database as db
from ..execution import hedge_guard
from ..external.geckoterminal import GeckoTerminal, PoolMarket
from .. import hedges as hedge_mod
from ..external.lighter import Lighter
from ..hedges import base as hedge_base
from ..tokens import TokenBook
from . import volatility as vol
from .judge import Judgement, SignalParams, Warn, judge
from .model import Evaluation, ModelParams, PoolInputs, TokenSide, evaluate, swap_price_impact
from .prices import PoolPrice, usd_prices

log = logging.getLogger(__name__)

EXTERNAL_SOURCE = "geckoterminal:1h"
VOLUME_TEXT = "取引量が不自然に多い（見せかけの取引の可能性）"
# 報酬トークンそのものを預けるプール（WETH-LUTE、USDG-LUTE など）。預けたコインとボーナスの両方が、
# 同じコインの値下がりで減る（2026-09-30 オーナー追加。SPEC 4章。軽微 = 最高でも🟡）
REWARD_HELD_TEXT = "ボーナスのコインを持つため、値下がりを二重に受けます"
REWARD_DECIMALS = 18   # 既定値。会場ファイルの contracts.reward_token.decimals があればそちらを使う


@dataclass
class MarginBasis:
    """保険に預けるお金を「探す」と同じ自動の計算で決めるための数字（2026-10-03 直し。前は総額の 55% / 40% / 5% 決め打ち）。

    - withstand_rise: 値段がこれだけ上がっても強制決済されない額を預ける（opportunities.hedge_withstand_rise_pct。仮 50%）
    - reserve_usd: 予備（チェーンのガス代の分。opportunities.reserve_usd。2026-10-02 13:44 JST オーナー決定）
    - mmf_fallback: Lighter の維持の割合が読めないときの仮の値（guard.hedge_mmf_fallback）
    - lighter_db: Lighter の銘柄ごとの証拠金の割合を読む feeds の保存（無ければ仮の値）
    """
    withstand_rise: float = 0.5
    reserve_usd: float = 20.0
    mmf_fallback: float = 0.05
    lighter_db: Path | None = None
    # 市場ごとに max(withstand_rise, stay_days のうちのいちばんの上げ)（2026-10-04 オーナー決定 A。hedge_guard.withstand_for）
    per_market: bool = False
    stay_days: float = 14.0


@dataclass
class ScoreContext:
    venue: dict[str, Any]
    tokens: TokenBook
    settings: ScoringSettings
    stale_after_minutes: int
    rpc: Any = None                        # RpcClient（ガス価格の読み取りだけに使う）
    gt: GeckoTerminal | None = None
    lighter: Lighter | None = None         # 古い呼び方（hedges がなければ Lighter のアダプターに包む）
    hedges: dict[str, Any] | None = None   # ヘッジ先アダプター（SPEC 5.2.1章）。hedge_id → アダプター
    margin: MarginBasis = field(default_factory=MarginBasis)   # 保険に預けるお金の決め方（2026-10-03）


def model_params(s: ScoringSettings, gas_usd_per_tx: float) -> ModelParams:
    return ModelParams(
        c_total=s.total_capital_usd, lp_share=s.allocation_lp,
        ranges=tuple(x / 100 for x in s.ranges_pct), rebalance_wait_minutes=s.rebalance_wait_minutes,
        gas_usd_per_tx=gas_usd_per_tx, swap_ratio=s.swap_ratio,
        hedge_taker_fee=s.hedge_taker_fee_pct / 100, count_funding_income=s.count_funding_income,
        reward_sell_hours=s.reward_sell_hours,
    )


def signal_params(s: ScoringSettings) -> SignalParams:
    return SignalParams(s.green_min_pct, s.yellow_min_pct, s.green_min_tvl_usd, s.reward_token_7d_major_pct,
                        s.too_high_pct)


def _ts(v: str) -> int:
    return int(datetime.fromisoformat(v).timestamp())


def _pool_price(r: sqlite3.Row) -> PoolPrice | None:
    if r["price"] is None or r["sqrt_price_x96"] is None or r["token0_decimals"] is None:
        return None
    return PoolPrice(r["token0"].lower(), r["token1"].lower(), int(r["token0_decimals"]), int(r["token1_decimals"]),
                     float(r["price"]), int(r["liquidity_total"] or 0), int(r["sqrt_price_x96"]))


_SNAP_SQL = """SELECT s.*, p.token0, p.token1, p.token0_symbol, p.token1_symbol, p.token0_decimals,
                      p.token1_decimals, p.address
               FROM pool_snapshots s JOIN pools p ON p.id = s.pool_id"""


def own_series(conn: sqlite3.Connection, venue_id: str, stables: frozenset[str], since: datetime
               ) -> tuple[dict[str, list[tuple[int, float]]], dict[str, list[tuple[int, float]]]]:
    """自分の記録から (トークンのドル価格の並び, プール価格の並び) を作る。"""
    rows = conn.execute(_SNAP_SQL + " WHERE p.venue_id=? AND s.ts>=? ORDER BY s.ts",
                        (venue_id, since.isoformat(timespec="seconds"))).fetchall()
    by_run: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)
    tok: dict[str, list[tuple[int, float]]] = defaultdict(list)
    pool: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for run_rows in by_run.values():
        t = _ts(run_rows[0]["block_time"] or run_rows[0]["ts"])
        pps = [p for p in (_pool_price(r) for r in run_rows) if p]
        for token, usd in usd_prices(pps, stables).items():
            tok[token].append((t, usd))
        for r in run_rows:
            if r["price"]:
                pool[r["pool_id"]].append((t, float(r["price"])))
    return dict(tok), dict(pool)


def merge_series(ext: list[tuple[int, float]], own: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """自分の記録を優先し、それより前の時間だけ外部の足で補う。"""
    if not own:
        return list(ext)
    first = own[0][0]
    return [p for p in ext if p[0] < first] + list(own)


def external_series(conn: sqlite3.Connection, gt: GeckoTerminal | None, tokens: set[str], now: datetime,
                    days: float, refresh_hours: float, own: dict[str, list[tuple[int, float]]] | None = None,
                    max_fetch_seconds: float = 900) -> dict[str, list[tuple[int, float]]]:
    """GeckoTerminal の1時間足で補った価格の並び（token_prices 表にためる）。

    自分の記録がある時間は自分の記録を使い、足りない昔の分だけ外部の足を使う。
    一度ためた足で7日分がそろえば、外部サイトにはもう聞かない（自分の記録がない
    トークンだけ、refresh_hours ごとに取り直す）。GeckoTerminal は回数制限が厳しいため。
    """
    own = own or {}
    since = now - timedelta(days=days, hours=2)
    now_s = int(now.timestamp())
    stale = []
    for t in sorted(tokens):
        m = merge_series(db.token_price_series(conn, t, EXTERNAL_SOURCE, since), own.get(t, []))
        if not vol.covers(m, now_s, days) or m[-1][0] < now_s - refresh_hours * 3600:
            stale.append(t)
    if stale and gt is not None:
        log.info("fetching external hourly prices", extra={"data": {"tokens": len(stale)}})
        started = time.monotonic()
        fetched = failures = 0
        try:
            markets = gt.tokens(stale)
        except Exception as exc:
            log.warning("geckoterminal tokens failed", extra={"data": {"error": str(exc)}})
            markets = {}
        for t in stale:
            m = markets.get(t)
            if not m or not m.top_pool:
                continue
            # 外部サイトの調子が悪いときに、計算全体が長く止まらないようにする（残りは次の回に取る）
            if failures >= 3 or time.monotonic() - started > max_fetch_seconds:
                log.warning("external fetch stopped early", extra={"data": {"fetched": fetched, "failures": failures}})
                break
            try:
                bars = gt.hourly_usd(m.top_pool, t, limit=int(days * 24) + 3)
            except Exception as exc:
                failures += 1
                log.warning("geckoterminal ohlcv failed", extra={"data": {"token": t, "error": str(exc)}})
                continue
            failures = 0
            fetched += 1
            # 足の時刻は始まりなので、終値の時刻（1時間後）で保存する
            db.insert_token_prices(conn, [
                (t, datetime.fromtimestamp(ts + 3600, UTC).isoformat(timespec="seconds"), px, EXTERNAL_SOURCE, None)
                for ts, px in bars
            ])
            conn.commit()
        log.info("external hourly prices fetched", extra={"data": {
            "fetched": fetched, "seconds": round(time.monotonic() - started)}})
    return {t: merge_series(db.token_price_series(conn, t, EXTERNAL_SOURCE, since), own.get(t, [])) for t in tokens}


def _grid(series: list[tuple[int, float]] | None, now_s: int, days: float, stable: bool) -> list[tuple[int, float]]:
    start = now_s - int(days * 86400)
    if stable:
        return [(h, 1.0) for h in range(start - start % 3600, now_s + 1, 3600)]
    return vol.hourly_grid(series or [], start, now_s)


def _sigma(grid: list[tuple[int, float]]) -> float | None:
    return vol.daily_sigma([r for _, r in vol.hourly_returns(grid)])


def _change(grid: list[tuple[int, float]]) -> float | None:
    return grid[-1][1] / grid[0][1] - 1 if len(grid) >= 2 and grid[0][1] > 0 else None


def choose_hedges(hedges: dict[str, Any], tokens: TokenBook, token_addrs: set[str], now: datetime, days: float,
                  default_taker_pct: float) -> dict[str, dict[str, Any]]:
    """トークンごとに、使えるヘッジ先を比べて一番安いところを選ぶ（SPEC 5.2.1章。2026-09-29 オーナー追加）。

    比べるのは「開く＋閉じるの取引手数料 + 1日の資金調達料」（hedges.base.round_trip_cost）。
    資金調達が取れなかったヘッジ先は選ばない。返り値: トークンのアドレス → 選んだヘッジ先（候補の一覧つき）。
    """
    end = int(now.timestamp())
    start = end - int(days * 86400)
    fees: dict[str, dict[int, float | None]] = {}
    for hid, a in hedges.items():
        try:
            fees[hid] = {m.market_id: m.taker_pct for m in a.markets().values()}
        except Exception as exc:
            log.warning("hedge markets failed", extra={"data": {"hedge": hid, "error": str(exc)}})
            fees[hid] = {}
    funding: dict[tuple[str, int], float | None] = {}
    out: dict[str, dict[str, Any]] = {}
    for t in sorted(token_addrs):
        if tokens.is_stable(t):
            continue
        cands = []
        for ref in tokens.perp_candidates(t):
            a = hedges.get(ref.venue)
            if a is None:
                continue
            key = (ref.venue, ref.market_id)
            if key not in funding:
                try:
                    funding[key] = hedge_base.funding_daily(a, ref.market_id, start, end)
                except Exception as exc:
                    log.warning("funding failed", extra={"data": {"hedge": ref.venue, "market_id": ref.market_id,
                                                                  "error": str(exc)}})
                    funding[key] = None
            taker = fees.get(ref.venue, {}).get(ref.market_id)
            cost = hedge_base.round_trip_cost(taker, funding[key], default_taker_pct)
            cands.append({"hedge_id": ref.venue, "name": getattr(a, "name", ref.venue), "symbol": ref.symbol,
                          "market_id": ref.market_id,
                          "taker_pct": taker if taker is not None else default_taker_pct,
                          "funding_daily": funding[key], "cost": cost})
        usable = [c for c in cands if c["cost"] is not None]
        if usable:
            best = min(usable, key=lambda c: c["cost"])
            out[t] = {**best, "candidates": cands}
        elif cands:
            out[t] = {"hedge_id": None, "candidates": cands}
    return out


def venue_warnings(venue: dict[str, Any]) -> list[Warn]:
    return [Warn(w.get("code", "C4"), w.get("level", "minor"), w.get("title_ja") or w.get("key", ""))
            for w in venue.get("warnings") or []]


def score_venue(conn: sqlite3.Connection, ctx: ScoreContext, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    s = ctx.settings
    venue_id = ctx.venue["id"]
    run = conn.execute(
        "SELECT * FROM collection_runs WHERE venue_id=? AND status IN ('ok','partial') ORDER BY id DESC LIMIT 1",
        (venue_id,),
    ).fetchone()
    if run is None:
        log.info("no snapshots to score", extra={"data": {"venue": venue_id}})
        return []
    log.info("scoring started", extra={"data": {"venue": venue_id, "run_id": run["id"]}})
    latest = conn.execute(_SNAP_SQL + " WHERE s.run_id=?", (run["id"],)).fetchall()
    age_min = (now - datetime.fromisoformat(run["finished_at"] or run["started_at"])).total_seconds() / 60
    stale = age_min > ctx.stale_after_minutes

    stables = ctx.tokens.stablecoins
    now_s = int(now.timestamp())
    days = s.volatility_days
    since = now - timedelta(days=days, hours=2)
    tok_own, pool_own = own_series(conn, venue_id, stables, since)
    prices = usd_prices([p for p in (_pool_price(r) for r in latest) if p], stables)

    reward_token = (contract_address(ctx.venue, "reward_token") or "").lower()
    reward_decimals = int(((ctx.venue.get("contracts") or {}).get("reward_token") or {}).get("decimals")
                          or REWARD_DECIMALS)

    # どのプールが自分の記録だけで足りるか
    own_ok: dict[str, bool] = {}
    need_ext: set[str] = set()
    for r in latest:
        t0, t1 = r["token0"].lower(), r["token1"].lower()
        ok = vol.covers(pool_own.get(r["pool_id"], []), now_s, days) and all(
            t in stables or vol.covers(tok_own.get(t, []), now_s, days) for t in (t0, t1))
        own_ok[r["pool_id"]] = ok
        if not ok:
            need_ext |= {t for t in (t0, t1) if t not in stables}
    reward_own = vol.covers(tok_own.get(reward_token, []), now_s, days)
    if reward_token and not reward_own:
        need_ext.add(reward_token)
    ext = external_series(conn, ctx.gt, need_ext, now, days, s.external_refresh_hours, tok_own) if need_ext else {}

    grids: dict[tuple[str, str], list[tuple[int, float]]] = {}

    def token_grid(token: str, src: str) -> list[tuple[int, float]]:
        key = (token, src)
        if key not in grids:
            series = tok_own.get(token) if src == "own" else ext.get(token)
            grids[key] = _grid(series, now_s, days, token in stables)
        return grids[key]

    # 報酬トークン（UP）の7日の変化
    reward_change = _change(token_grid(reward_token, "own" if reward_own else "external")) if reward_token else None
    reward_trend_daily = (1 + reward_change) ** (1 / days) - 1 if reward_change is not None and reward_change > -1 else None
    reward_usd = prices.get(reward_token)

    # 出来高・置かれている額（GeckoTerminal）
    markets: dict[str, PoolMarket] = {}
    reward_volume = None
    if ctx.gt is not None:
        try:
            markets = ctx.gt.pools([r["address"] for r in latest])
            if reward_token:
                reward_volume = ctx.gt.tokens([reward_token]).get(reward_token)
        except Exception as exc:
            log.warning("geckoterminal pools failed", extra={"data": {"error": str(exc)}})
    if reward_usd is None and reward_volume is not None and reward_volume.price_usd:
        # 会場の中に報酬トークンの値段の道がない（Alandale の LUTE は集中流動性のプールにない）ときは、
        # GeckoTerminal のトークン価格を使う（他の DEX も含めた値段）
        reward_usd = reward_volume.price_usd

    # ヘッジ先を選ぶ（トークンごとに一番安いところ。SPEC 5.2.1章）
    hedges = ctx.hedges if ctx.hedges is not None else (
        {a.hedge_id: a} if (a := hedge_mod.wrap(ctx.lighter)) is not None else {})
    token_addrs = {t.lower() for r in latest for t in (r["token0"], r["token1"])}
    funding = choose_hedges(hedges, ctx.tokens, token_addrs, now, days, s.hedge_taker_fee_pct)

    # ガス代
    gas_usd = 0.0
    weth_usd = prices.get(ctx.tokens.wrapped_native) if ctx.tokens.wrapped_native else None
    if ctx.rpc is not None and weth_usd:
        try:
            gas_usd = s.gas_units_per_tx * ctx.rpc.gas_price() / 1e18 * weth_usd
        except Exception as exc:
            log.warning("gas price failed", extra={"data": {"error": str(exc)}})

    params = model_params(s, gas_usd)
    margin_table = hedge_guard.lighter_margin_table(ctx.margin.lighter_db) if ctx.margin.lighter_db else {}
    sparams = signal_params(s)
    base_warns = venue_warnings(ctx.venue)
    if reward_change is not None and reward_change * 100 <= s.reward_token_7d_major_pct:
        base_warns.append(Warn("C2", "major", f"報酬トークンが7日で {reward_change * 100:.0f}%"))

    liq_med = liquidity_medians(conn, venue_id, now - timedelta(hours=24))

    ts = now.isoformat(timespec="seconds")
    out = []
    for r in latest:
        row = _score_pool(r, ctx, params, sparams, base_warns, prices, own_ok[r["pool_id"]], token_grid,
                          pool_own, markets, funding, reward_usd, reward_trend_daily, reward_change,
                          reward_volume.volume_24h_usd if reward_volume else None, stale, age_min, now_s, days,
                          liq_med.get(r["pool_id"]), reward_decimals, margin_table)
        row.update({"pool_id": r["pool_id"], "ts": ts, "venue_id": venue_id, "block_number": run["block_number"]})
        is_stock = row.pop("_is_stock")
        db.insert_score(conn, row)
        conn.execute("UPDATE pools SET is_stock_pair=?, has_perp=? WHERE id=?",
                     (is_stock, row["has_perp"], r["pool_id"]))
        out.append(row)
    conn.commit()
    counts = defaultdict(int)
    for row in out:
        counts[row["signal"]] += 1
    log.info("scoring finished", extra={"data": {"venue": venue_id, "pools": len(out), "signals": dict(counts),
                                                 "gas_usd_per_tx": round(gas_usd, 4)}})
    return out


def _split_for(m: MarginBasis, c_total: float, table: dict[int, tuple[float | None, float | None]],
               hedged: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """プール・保険・予備の分け方。hedged = 保険を掛けるコインごとの (記号, 選んだヘッジ先)。"""
    needs: dict[str, dict[str, Any]] = {}
    for sym, ch in hedged:
        imf = mmf = None
        mid = ch.get("market_id")
        if ch.get("hedge_id") == "lighter" and mid is not None and int(mid) in table:
            imf, mmf = table[int(mid)]
        from_lighter = mmf is not None
        mmf = mmf if mmf is not None else m.mmf_fallback
        w = m.withstand_rise
        if m.per_market and m.lighter_db is not None and ch.get("hedge_id") == "lighter" and mid is not None:
            w = max(w, hedge_guard.withstand_table(m.lighter_db, m.stay_days).get(int(mid), 0.0))
        needs[sym] = {"need": hedge_guard.margin_need(imf, mmf, w), "imf": imf, "mmf": mmf,
                      "from_lighter": from_lighter, "hedge_id": ch.get("hedge_id"), "withstand_rise_pct": w * 100}
    sp = hedge_guard.split(c_total, m.reserve_usd, [(0.5, n["need"]) for n in needs.values()])
    return {"lp": sp["pool"] / c_total, "hedge_margin": sp["hedge_margin"] / c_total, "reserve": sp["reserve"] / c_total,
            "lp_usd": sp["pool"], "hedge_margin_usd": sp["hedge_margin"], "reserve_usd": sp["reserve"],
            "margin_per_pool": sp["margin_per_pool"], "withstand_rise_pct": m.withstand_rise * 100, "needs": needs,
            "basis": "auto"}


def liquidity_medians(conn: sqlite3.Connection, venue_id: str, since: datetime) -> dict[str, tuple[int, int]]:
    """プールごとの、直近の記録の流動性の中央値 (プール全体, レンジ内のステーク分)。"""
    rows = conn.execute(
        """SELECT s.pool_id, s.liquidity_total, s.liquidity_staked_inrange FROM pool_snapshots s
           JOIN pools p ON p.id = s.pool_id WHERE p.venue_id=? AND s.ts>=?""",
        (venue_id, since.isoformat(timespec="seconds"))).fetchall()
    tot: dict[str, list[int]] = defaultdict(list)
    stk: dict[str, list[int]] = defaultdict(list)
    for pid, lt, ls in rows:
        if lt is not None:
            tot[pid].append(int(lt))
        if ls is not None:
            stk[pid].append(int(ls))
    return {pid: (statistics.median_low(tot[pid]), statistics.median_low(stk[pid]) if stk[pid] else 0)
            for pid in tot}


def others_liquidity(latest: int, median: int | None) -> int:
    """取り分の分母に使う「他の人の流動性」= 24時間の中央値と最新の値の大きい方（安全側。2026-09-29 オーナー決定）。"""
    return max(latest, median or 0)


def _score_pool(r, ctx, params, sparams, base_warns, prices, own_ok, token_grid, pool_own, markets, funding,
                reward_usd, reward_trend_daily, reward_change, reward_volume, stale, age_min, now_s, days,
                liq_med: tuple[int, int] | None = None, reward_decimals: int = REWARD_DECIMALS,
                margin_table: dict[int, tuple[float | None, float | None]] | None = None) -> dict[str, Any]:
    tokens = ctx.tokens
    t0, t1 = r["token0"].lower(), r["token1"].lower()
    src = "own" if own_ok else "external"
    g0, g1 = token_grid(t0, src), token_grid(t1, src)
    pair_grid = (vol.hourly_grid(pool_own.get(r["pool_id"], []), now_s - int(days * 86400), now_s)
                 if own_ok else vol.ratio_grid(g0, g1))
    sig0 = 0.0 if tokens.is_stable(t0) else _sigma(g0)
    sig1 = 0.0 if tokens.is_stable(t1) else _sigma(g1)
    sig_pair = _sigma(pair_grid)
    # 直近24時間の値動き（2026-10-04 オーナー決定 ①A。控えめの見込みと「値動きが急に大きくなった」の印に使う）
    sig_pair24 = vol.recent_sigma(vol.hourly_returns(pair_grid), now_s)
    sig24 = {r["token0_symbol"]: 0.0 if tokens.is_stable(t0) else vol.recent_sigma(vol.hourly_returns(g0), now_s),
             r["token1_symbol"]: 0.0 if tokens.is_stable(t1) else vol.recent_sigma(vol.hourly_returns(g1), now_s)}
    is_stock = tokens.is_stock(t0) or tokens.is_stock(t1)
    split = {}
    for t, g in ((t0, g0), (t1, g1)):
        if tokens.is_stock(t):
            so, sc = vol.split_sigma(vol.hourly_returns(g))
            split[tokens.stock_tokens[t]] = {"sigma_open": so, "sigma_closed": sc}

    def side(t: str, sig: float | None, dec) -> TokenSide | None:
        usd = prices.get(t)
        if usd is None or sig is None or dec is None:
            return None
        ch = funding.get(t) or {}
        fund = ch.get("funding_daily") if ch.get("hedge_id") else None
        taker = ch.get("taker_pct")
        return TokenSide(usd=usd, decimals=int(dec), sigma_usd=sig, stable=tokens.is_stable(t),
                         hedgeable=fund is not None, funding_cost_daily=fund or 0.0,
                         taker_fee=taker / 100 if fund is not None and taker is not None else None)

    s0, s1 = side(t0, sig0, r["token0_decimals"]), side(t1, sig1, r["token1_decimals"])
    # プール・保険・予備の分け方（2026-10-03 直し。「探す」と同じ自動の計算）: 保険を掛ける値動きするコインごとに、
    # プールの中身の約半分（幅の真ん中に置くと半分ずつ）× 売り1ドルあたりに預けるお金
    split_info = _split_for(ctx.margin, params.c_total, margin_table or {},
                            [(r[f"token{i}_symbol"], funding.get(t) or {}) for i, (t, sd) in enumerate(((t0, s0), (t1, s1)))
                             if sd is not None and not sd.stable and sd.hedgeable])
    params = replace(params, lp_share=split_info["lp"])
    market = markets.get((r["address"] or "").lower())
    fee = (r["fee"] or 0) / 1e6
    tvl = market.reserve_usd if market else None
    volume = market.volume_24h_usd if market else None
    volume_used = volume
    pool_warns = list(base_warns)
    reward_token = (contract_address(ctx.venue, "reward_token") or "").lower()
    if reward_token and reward_token in (t0, t1):
        pool_warns.append(Warn("RWD", "minor", REWARD_HELD_TEXT))
    sus_x = ctx.settings.volume_suspicious_tvl_multiple
    if volume is not None and tvl and volume > sus_x * tvl:
        # 見せかけの取引かもしれないので、手数料収入は0として計算する（安全側。2026-09-29 オーナー決定。
        # 前の「TVL × 倍率 までに抑える」ルールを置き換えた）
        volume_used = 0.0
        pool_warns.append(Warn("VOL", "minor", VOLUME_TEXT))
    fees_day = volume_used * fee if volume_used is not None else None
    eff = int(r["reward_rate_effective_raw"] or 0)
    alive = r["gauge_alive"] is None or bool(r["gauge_alive"])
    reward_usd_day = eff * 86400 / 10 ** reward_decimals * reward_usd if (reward_usd and alive) else 0.0

    missing = None
    if stale:
        missing = f"最新のデータが {age_min:.0f} 分前のもので古い"
    elif s0 is None or s1 is None:
        missing = "トークンのドル価格か値動きが分からない"
    elif sig_pair is None:
        missing = "2つのトークンの比率の値動きが分からない"
    elif reward_usd is None:
        missing = "報酬トークンのドル価格が分からない"

    notes = []
    if r["epoch_just_flipped"]:
        notes.append("エポック更新直後のため、ボーナスの値が落ち着いていない可能性があります。")
    if not alive:
        notes.append("ゲージが止まっていて、ボーナスは出ません。")
    elif eff == 0:
        notes.append("今週のボーナスはまだ配られていません。")
    if volume_used is not None and volume_used != volume:
        notes.append(f"{VOLUME_TEXT}。手数料の収入は0として計算しています（取引量がTVLの{sus_x:g}倍超え）。")
    if src == "external":
        notes.append("値動きは外部データ（GeckoTerminal）で補っています。")
    if any(w.code == "RWD" for w in pool_warns):
        notes.append(REWARD_HELD_TEXT + "。")

    manual = _manual_bonus(r, ctx.venue, reward_decimals, reward_usd if alive else None)
    if manual:
        notes.append(MANUAL_TEXT)

    ev: Evaluation | None = None
    slip = slip_src = None
    l_total_now, l_staked_now = int(r["liquidity_total"] or 0), int(r["liquidity_staked_inrange"] or 0)
    l_total = others_liquidity(l_total_now, liq_med[0] if liq_med else None)
    l_staked = others_liquidity(l_staked_now, liq_med[1] if liq_med else None)
    if missing is None:
        # 両替のずれは「今」の流動性で見積もる（今の方が少なければ、ずれは大きくなる = 安全側）
        slip, slip_src = _slippage(ctx.settings, params, float(r["price"]), l_total_now, s0, s1, tokens, t0, t1)
        inp = PoolInputs(
            price=float(r["price"]), token0=s0, token1=s1, fee=fee,
            unstaked_fee=(r["unstaked_fee"] or 0) / 1e6,
            liquidity_total=l_total, liquidity_staked=l_staked,
            reward_usd_day=reward_usd_day, fees_usd_day=fees_day, sigma_pair=sig_pair,
            reward_trend_daily=reward_trend_daily, slippage=slip,
            # ステークがなく、LPが取引手数料を受け取れない会場（Alandale の CL。会場ファイルの mechanics）
            rewards_only=mechanic_value(ctx.venue, "lp_receives_swap_fees", True) is False,
        )
        ev = evaluate(inp, params)
    j: Judgement = judge(ev, sparams, warnings=pool_warns, tvl_usd=tvl, missing=missing, notes=notes)

    details = {
        # プール・保険・予備の分け方（割合とドル。練習を始めるときも同じ決め方で分ける）
        "split": split_info,
        "inputs": {
            "price": r["price"], "usd": {r["token0_symbol"]: prices.get(t0), r["token1_symbol"]: prices.get(t1)},
            "sigma_token": {r["token0_symbol"]: sig0, r["token1_symbol"]: sig1}, "sigma_pair": sig_pair,
            "sigma_pair_24h": sig_pair24, "sigma_token_24h": sig24,
            "sigma_stock_split": split, "vol_source": src,
            "reward_usd_day": reward_usd_day, "fees_usd_day": fees_day, "fee": fee,
            "volume_24h_usd": volume, "tvl_usd": tvl,
            "reward_token_usd": reward_usd, "reward_token_change_7d": reward_change, "reward_token_trend_daily": reward_trend_daily,
            "emission_pressure": (reward_usd_day / reward_volume) if reward_volume else None,
            "gas_usd_per_tx": params.gas_usd_per_tx,
            "reward_sell_hours": params.reward_sell_hours,
            "slippage": slip, "slippage_source": slip_src,
            "liquidity_total": str(l_total), "liquidity_staked_inrange": str(l_staked),
            "liquidity_latest": {"total": str(l_total_now), "staked": str(l_staked_now)},
            "liquidity_median_24h": ({"total": str(liq_med[0]), "staked": str(liq_med[1])} if liq_med else None),
            "volume_used_usd": volume_used,
            "perp": {sym: ((funding.get(t) or {}).get("symbol") if (funding.get(t) or {}).get("hedge_id") else None)
                     for sym, t in ((r["token0_symbol"], t0), (r["token1_symbol"], t1))},
            # 選んだヘッジ先と、比べた候補（SPEC 5.2.1章）。ステーブルコインとヘッジできないトークンは None
            "hedge": {sym: funding.get(t) for sym, t in ((r["token0_symbol"], t0), (r["token1_symbol"], t1))
                      if not tokens.is_stable(t)},
            "notes": notes,
            # 運営が手で足したボーナス（判定には入れない。2026-09-30 オーナー条件4）
            "manual_bonus": _with_share(manual, ev.best if ev else None, reward_usd_day),
        },
        "ranges": [
            {"r_pct": x.r * 100, "net": x.net, "net_pct": x.net / params.c_total * 100, "income": x.income,
             "mode": x.mode, "income_staked": x.income_staked, "income_unstaked": x.income_unstaked,
             "gamma": x.gamma, "rebalance": x.rebalance, "hedge": x.hedge, "haircut": x.haircut,
             "direction_risk": x.direction_risk, "in_range_ratio": x.in_range_ratio,
             "in_range_ratio_hold": x.in_range_ratio_hold, "rebalances_per_day": x.rebalances_per_day,
             "mode_sell_now": x.mode_sell_now, "haircut_sell_now": x.haircut_sell_now,
             "net_sell_now": x.net_sell_now, "net_sell_now_pct": x.net_sell_now / params.c_total * 100,
             **_depth(x, params, float(r["price"]), l_total, l_staked, s0, s1)}
            for x in (ev.rows if ev else ())
        ],
        # 参考値（判定には使わない）: 報酬をすぐ売る前提（2026-09-29 オーナー指示）
        "sell_now": ({
            "hours": params.reward_sell_hours, "best_r": ev.best_sell_now.r * 100,
            "net_daily_pct": ev.net_daily_pct_sell_now, "net_usd": ev.best_sell_now.net_sell_now,
            "income": _sell_income(ev.best_sell_now),
            "haircut": ev.best_sell_now.haircut_sell_now, "mode": ev.best_sell_now.mode_sell_now,
        } if ev else None),
    }
    b = ev.best if ev else None
    return {
        "best_r": b.r * 100 if b else None,
        "income": b.income if b else None, "gamma": b.gamma if b else None,
        "rebalance": b.rebalance if b else None, "hedge": b.hedge if b else None,
        "haircut": b.haircut if b else None, "direction_risk": b.direction_risk if b else None,
        "net_daily_pct": ev.net_daily_pct if ev else None,
        "net_daily_pct_lp": ev.net_daily_pct_lp if ev else None,
        "mode": b.mode if b else None,
        "in_range_ratio": b.in_range_ratio if b else None,
        "in_range_ratio_hold": b.in_range_ratio_hold if b else None,
        "sigma_pair": sig_pair, "sigma_token0": sig0, "sigma_token1": sig1, "vol_source": src,
        "has_perp": int(ev.has_perp) if ev else None,
        "epoch_just_flipped": r["epoch_just_flipped"], "tvl_usd": tvl,
        "signal": j.signal, "reason_ja": j.reason_ja,
        "warnings_json": json.dumps([w.__dict__ for w in j.warnings], ensure_ascii=False),
        "details_json": json.dumps(details, ensure_ascii=False, default=str),
        "_is_stock": int(is_stock),
    }


MANUAL_TEXT = ("今週は運営が手で足したボーナスがあります。続く保証がないので、判定の計算には入れていません"
               "（いつものボーナスだけで判定しています）。")


def _raw(r, key: str) -> int | None:
    """スナップショットの行から、最小単位の数（文字列で保存）を取り出す。列がない古い行は None。"""
    if key not in r.keys() or r[key] in (None, ""):
        return None
    return int(r[key])


def _manual_bonus(r, venue: dict[str, Any], decimals: int, token_usd: float | None) -> dict[str, Any] | None:
    """今のエポックに運営が手で足したボーナス（プール全体）。なければ None。

    週ごとの量で配る会場（Alandale）だけが記録する。1日あたりは「週の分を7日に均等に割った」参考値。
    """
    manual = _raw(r, "reward_manual_raw")
    if not manual:
        return None
    total = _raw(r, "reward_epoch_total_raw")
    length = int((((venue.get("mechanics") or {}).get("epoch") or {}).get("length_seconds")) or 7 * 86400)
    amount = manual / 10 ** decimals
    usd = amount * token_usd if token_usd else None
    return {
        "symbol": ((venue.get("contracts") or {}).get("reward_token") or {}).get("symbol"),
        "amount": amount, "usd": usd,
        "epoch_total": total / 10 ** decimals if total is not None else None,
        "regular": (total - manual) / 10 ** decimals if total is not None else None,
        "usd_day_spread": usd * 86400 / length if usd is not None else None,
        "epoch_start": r["epoch_start"],
    }


def _with_share(manual: dict[str, Any] | None, best, reward_usd_day: float) -> dict[str, Any] | None:
    """手で足した分も入れたら、あなたの1日の見込みがいくら増えるか（参考。判定には使わない）。"""
    if manual is None:
        return None
    extra = None
    if best is not None and reward_usd_day > 0 and manual.get("usd_day_spread") is not None:
        # いつものボーナスと同じ取り分で受け取ると仮定する（取り分 = 自分の報酬 ÷ プール全体の報酬）
        extra = manual["usd_day_spread"] * best.income_staked / reward_usd_day
    return {**manual, "your_extra_usd_day": extra}


def _sell_income(x) -> float:
    """参考値「すぐ売る前提」の収入（選んだ受け取り方の分）。"""
    if x.mode_sell_now == "unstaked":
        return x.income_unstaked
    return x.income_staked


def _slippage(s: ScoringSettings, params: ModelParams, price: float, l_total: int, t0: TokenSide, t1: TokenSide,
              tokens: TokenBook, a0: str, a1: str) -> tuple[float, str]:
    """両替のずれ（割合）と、その出どころ（"pool" = プールの流動性から計算 / "fallback" = 初期値）。"""
    trade = s.slippage_trade_usd if s.slippage_trade_usd is not None else params.c_lp
    impact = swap_price_impact(trade, l_total, price, t0, t1)
    if impact is not None:
        return impact, "pool"
    calm = all(tokens.is_stable(a) or tokens.is_stock(a) for a in (a0, a1))
    pct = s.slippage_fallback_stable_stock_pct if calm else s.slippage_fallback_other_pct
    return pct / 100, "fallback"


def _depth(x, params: ModelParams, price: float, l_total: int, l_staked: int, t0: TokenSide, t1: TokenSide
           ) -> dict[str, float | None]:
    """そのレンジ幅の中にある、プール全体とステーク分の流動性のドル換算と、自分の取り分（確認用）。"""
    per_usd = x.liquidity_mine / params.c_lp if params.c_lp > 0 else 0.0   # 1ドルあたりの L
    if per_usd <= 0:
        return {"pool_inrange_usd": None, "staked_inrange_usd": None, "share_staked": None, "share_total": None}
    return {
        "pool_inrange_usd": l_total / per_usd,
        "staked_inrange_usd": l_staked / per_usd,
        "share_staked": x.liquidity_mine / (l_staked + x.liquidity_mine),
        "share_total": x.liquidity_mine / (l_total + x.liquidity_mine),
    }
