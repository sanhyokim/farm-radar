"""練習（ペーパートレード）の定期ジョブ。収集のあとに呼ぶ（M5a）。

- 持っている建玉の perp の資金調達率（Lighter の1時間ごとの実績）を取っておく
- 新しい記録の分だけ、建玉の損益を計算する
- 円のレートが取れていなかった台帳の行を埋める
- 危険判定のルールで見張り、仮想的に置き直す・閉じる（M5b。risk_job.py）
- 15分ごとの回だけ: 会場プログラムの見張り（contract_watch.py）と、USDG の外部の価格（2026-09-29 オーナー決定）
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from .. import hedges as hedge_mod
from ..config import Config
from ..fx import Frankfurter, fill_ledger_jpy
from ..tokens import TokenBook
from . import hedge_guard
from .paper import PaperExecutor
from . import contract_watch
from .risk_job import record_stable_price, run_risk

log = logging.getLogger(__name__)


def _hedges(hedges=None, lighter=None) -> dict:
    """ヘッジ先アダプターの辞書。古い呼び方（lighter=）も受け付ける。"""
    if hedges:
        return dict(hedges)
    a = hedge_mod.wrap(lighter)
    return {a.hedge_id: a} if a is not None else {}


def refresh_funding(conn: sqlite3.Connection, hedges, markets: set[tuple[str, int]], now: datetime,
                    hours: int = 6) -> int:
    """直近 hours 時間の資金調達率を hedge_funding 表に入れる（ヘッジ先ごと）。失敗しても止めない（その時間は0で推定の印）。"""
    hedges = _hedges(None, hedges) if not isinstance(hedges, dict) else hedges
    end = int(now.timestamp())
    n = 0
    for hid, mid in sorted(markets):
        a = hedges.get(hid)
        if a is None:
            continue
        try:
            rows = a.short_funding_hourly(mid, end - hours * 3600, end)
        except Exception as exc:
            log.warning("funding fetch failed", extra={"data": {"hedge": hid, "market_id": mid, "error": str(exc)}})
            continue
        conn.executemany("INSERT OR REPLACE INTO hedge_funding(hedge_id, market_id, ts, short_rate) VALUES (?,?,?,?)",
                         [(hid, mid, t // 3600 * 3600, r) for t, r in rows])
        n += len(rows)
    conn.commit()
    return n


def refresh_funding_rh(conn: sqlite3.Connection, adapter, pairs: dict[int, int], now: datetime, hours: int = 6) -> int:
    """Lighter の Robinhood Chain 版の資金調達率を hedge_funding 表に入れる（2026-10-04 オーナー決定 ②A）。

    pairs = 本体の市場の番号 → RH版の市場の番号。表には hedge_id "lighter_rh"・本体の番号で入れる（練習の保険は本体の番号を持つ）。
    """
    end = int(now.timestamp())
    n = 0
    for mid, rh_id in sorted(pairs.items()):
        try:
            rows = adapter.short_funding_hourly(rh_id, end - hours * 3600, end)
        except Exception as exc:
            log.warning("funding fetch failed", extra={"data": {"hedge": "lighter_rh", "market_id": rh_id,
                                                                "error": str(exc)}})
            continue
        conn.executemany("INSERT OR REPLACE INTO hedge_funding(hedge_id, market_id, ts, short_rate) VALUES (?,?,?,?)",
                         [("lighter_rh", mid, t // 3600 * 3600, r) for t, r in rows])
        n += len(rows)
    conn.commit()
    return n


def rh_pairs(config: Config, main_ids: set[int]) -> dict[int, int]:
    """練習の保険の市場（本体の番号）のうち、RH版にもあるもの（本体の番号 → RH版の番号）。RH版を使わない設定なら空。"""
    if not main_ids or not hedge_guard.use_rh(config):
        return {}
    table = hedge_guard._read(config.feeds.database_path, hedge_guard.rh_markets) or {}
    return {mid: v[0] for mid, v in table.items() if mid in main_ids}


def refresh_perp_fees(conn: sqlite3.Connection, hedges, now: datetime, max_age_hours: float = 1.0) -> int:
    """ヘッジ先ごとの市場の取引手数料を hedge_fees 表に入れる（1時間に1回まで。失敗しても止めない）。"""
    hedges = _hedges(None, hedges) if not isinstance(hedges, dict) else hedges
    n = 0
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    for hid, a in hedges.items():
        last = conn.execute("SELECT MAX(ts) FROM hedge_fees WHERE hedge_id=?", (hid,)).fetchone()[0]
        if last and (now - datetime.fromisoformat(last)).total_seconds() < max_age_hours * 3600:
            continue
        try:
            markets = a.markets()
        except Exception as exc:
            log.warning("perp fee fetch failed", extra={"data": {"hedge": hid, "error": str(exc)}})
            continue
        conn.executemany("INSERT OR REPLACE INTO hedge_fees(hedge_id, market_id, symbol, taker_pct, maker_pct, ts) "
                         "VALUES (?,?,?,?,?,?)",
                         [(hid, m.market_id, m.symbol, m.taker_pct, m.maker_pct, ts) for m in markets.values()])
        n += len(markets)
    conn.commit()
    return n


def refresh_hedge_accounts(conn: sqlite3.Connection, config: Config, hedges, now: datetime) -> int:
    """アドレスを設定したヘッジ先だけ、担保の残高と建玉を読み取る（SPEC 5.2.1章。読み取りのみ。秘密鍵は使わない）。"""
    n = 0
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    for hv in config.hedge_venues:
        a = hedges.get(hv.hedge_id)
        if a is None or not hv.account_address:
            continue
        try:
            acc = a.account(hv.account_address)
            row = ((acc.collateral_usd, acc.available_usd,
                    json.dumps([p.__dict__ for p in acc.positions], ensure_ascii=False), None)
                   if acc is not None else (0.0, 0.0, "[]", "not_found"))
        except Exception as exc:
            # アドレスはログに出さない（ヘッジ先の名前とエラーの種類だけ）
            log.warning("hedge account read failed", extra={"data": {"hedge": hv.hedge_id, "error": type(exc).__name__}})
            row = (None, None, None, "error")
        conn.execute("INSERT OR REPLACE INTO hedge_accounts(hedge_id, ts, collateral_usd, available_usd, positions_json, "
                     "error) VALUES (?,?,?,?,?,?)", (hv.hedge_id, ts, *row))
        n += 1
    conn.commit()
    return n


def run_watch(conn: sqlite3.Connection, config: Config, tokens: TokenBook, rpc=None, venue=None, gt=None,
              now: datetime | None = None, lighter=None, hedges=None) -> list[str]:
    """会場プログラムの見張りと USDG の外部の価格（読み取りだけ）。変わったことの説明を返す。
    ついでに perp の取引手数料と、ヘッジ先の担保の残高も取り直す（開く・閉じる費用と画面に使う）。"""
    now = now or datetime.now(UTC)
    hedges = _hedges(hedges, lighter)
    if hedges:
        refresh_perp_fees(conn, hedges, now)
        refresh_hedge_accounts(conn, config, hedges, now)
    changes: list[str] = []
    if config.risk.contract_watch and rpc is not None and venue is not None:
        try:
            changes = contract_watch.check(conn, rpc, venue, tokens, now)
        except Exception:
            log.exception("contract watch failed")
    record_stable_price(conn, gt, tokens, now)
    return changes


def run_paper(conn: sqlite3.Connection, config: Config, tokens: TokenBook, lighter=None,
              fx: Frankfurter | None = None, now: datetime | None = None, rpc=None, venue=None, gt=None,
              fast: bool = False, hedges=None) -> int:
    """練習モードのときだけ動く。計算した行の数を返す。"""
    if config.mode != "paper":
        return 0
    now = now or datetime.now(UTC)
    hedges = _hedges(hedges, lighter)
    changes = [] if fast else run_watch(conn, config, tokens, rpc, venue, gt, now, hedges=hedges)
    positions = conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open'").fetchall()
    if not positions:
        if changes:
            run_risk(conn, config, tokens, PaperExecutor(conn, config, tokens, fx=fx, now=now), now, changes)
        return 0
    if hedges:
        markets = {(h.get("hedge_id") or "lighter", int(h["market_id"]))
                   for p in positions for h in json.loads(p["hedges_json"] or "[]")}
        if markets:
            # パソコンが止まっていた分もさかのぼって取る（最長7日）
            oldest = min(datetime.fromisoformat(p["last_ts"]) for p in positions)
            hours = min(168, max(6, int((now - oldest).total_seconds() // 3600) + 2))
            refresh_funding(conn, hedges, markets, now, hours)
            # Robinhood Chain のプールの保険は、RH版にその市場があれば RH版の資金調達率で積み上げる（②A）
            pairs = rh_pairs(config, {mid for hid, mid in markets if hid == "lighter"})
            if pairs:
                refresh_funding_rh(conn, hedges.get("lighter_rh") or hedge_mod.LighterRhHedge(), pairs, now, hours)
    ex = PaperExecutor(conn, config, tokens, fx=fx, now=now)
    n = 0
    for p in positions:
        try:
            n += ex.update(p)
        except Exception:
            log.exception("paper update failed", extra={"data": {"position": p["id"]}})
    try:
        fill_ledger_jpy(conn, fx)
    except Exception:
        log.exception("fx fill failed")
    # 見張り（失敗したら scheduler がエラーとして通知する）
    run_risk(conn, config, tokens, ex, now, changes)
    if n:
        log.info("paper positions updated", extra={"data": {"positions": len(positions), "rows": n}})
    return n
