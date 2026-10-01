"""練習の建玉の見張り（SPEC 8.5章・12.2章・付録A 3章。M5b）。

損益の計算（jobs.run_paper）のあとに呼ぶ。建玉ごとに記録から今の状態を集めて risk/rules.py で調べ、
見つかった危険のうち一番強いものに合わせて、仮想的に動く:
- 緊急離脱: 1つのプールだけの危険（プールのお金の減少）は、その建玉だけ閉じる。会場全体の危険（会場プログラムの変化・
  USDG・今日の損）は、全部閉じて新しく始めるのを止める（2026-10-01 オーナー決定 C。どちらもガス代が高くても実行する）
- 離脱: その建玉を閉じる（ガス代が上限を超えていたら見送り、記録する）
- 置き直し: 最新のスコアの最適レンジで、今の価格を中心に置き直す（ガス代が上限を超えていたら見送り）
- 注意: 記録するだけ
すべて risk_events 表に理由つきで記録し、alerts 表にも入れて通知する（Telegram を設定していれば届く）。
同じ注意や見送りが続いているあいだは、最初の1回だけ記録する（状態が消えたら、また記録する）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Config, contract_address, load_venue
from ..db import database as db
from ..risk.rules import LEVEL_JA, Finding, PortfolioInput, PositionInput, check_portfolio, check_position
from ..scoring.run import EXTERNAL_SOURCE, merge_series, own_series
from ..tokens import TokenBook
from ..views import JST, series_change
from . import bonus_drop
from .base import PositionRef
from .paper import PaperError, PaperExecutor, latest_score

log = logging.getLogger(__name__)

ALERT_LEVEL = {"caution": "warning", "rebalance": "info", "exit": "major", "emergency": "major", "info": "info"}
EMOJI = {"caution": "⚠️", "rebalance": "🔁", "exit": "🚪", "emergency": "🚨", "info": "ℹ️"}


def record_event(conn: sqlite3.Connection, now: datetime, position_id: int | None, level: str, kind: str,
                 message_ja: str, action: str, data: dict[str, Any] | None = None, venue_id: str = "-",
                 pool_id: str | None = None) -> int:
    """見張りの記録を1つ書き、通知の箱（alerts）にも入れる。"""
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO risk_events(ts, position_id, level, kind, message_ja, action, data_json) "
                       "VALUES (?,?,?,?,?,?,?)", (ts, position_id, level, kind, message_ja, action,
                                                  json.dumps(data or {}, default=str)))
    eid = int(cur.lastrowid)
    label = LEVEL_JA.get(level, level)
    db.insert_alert(conn, ts=now, venue_id=venue_id, pool_id=pool_id, kind=f"paper_{level}", level=ALERT_LEVEL[level],
                    message_ja=f"{EMOJI[level]} 練習（{label}）: {message_ja}{_action_ja(action)}",
                    data={"risk_event": eid}, dedupe_key=f"paper_risk:{eid}")
    conn.commit()
    return eid


def _action_ja(action: str) -> str:
    return {"rebalanced": " → 置き直しました。", "closed": " → この建玉を閉じました。",
            "closed_pool": " → この建玉だけ閉じました（ほかの建玉はそのままです）。",
            "closed_all": " → 全部の建玉を閉じ、新しく始めるのを止めました。",
            "skipped_gas": " → ガス代が高いので見送りました（次の回にもう一度調べます）。",
            "none": "", "stopped": "", "resumed": ""}.get(action, "")


def set_stopped(conn: sqlite3.Connection, stopped: bool, reason: str, now: datetime) -> None:
    conn.execute("INSERT INTO paper_state(id, stopped, updated_at, reason) VALUES (1,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET stopped=excluded.stopped, updated_at=excluded.updated_at, "
                 "reason=excluded.reason", (int(stopped), now.isoformat(timespec="seconds"), reason))
    conn.commit()


def close_all(conn: sqlite3.Connection, ex: PaperExecutor, reason: str) -> list[int]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM positions WHERE is_paper=1 AND status='open'")]
    done = []
    for pid in ids:
        try:
            ex.close_position(PositionRef(pid), reason=reason)
            done.append(pid)
        except PaperError:
            log.warning("close failed", extra={"data": {"position": pid}})
    return done


def reward_change_24h(conn: sqlite3.Connection, config: Config, tokens: TokenBook, venue_id: str,
                      now: datetime, own: dict[str, list[tuple[int, float]]] | None = None) -> tuple[str, float | None]:
    """報酬トークンの24時間の変化（画面のホームと同じ作り方）。"""
    try:
        v = load_venue(venue_id, config.root)
    except Exception:
        return "報酬トークン", None
    token = (contract_address(v, "reward_token") or "").lower()
    if not token:
        return "報酬トークン", None
    since = now - timedelta(days=2)
    if own is None:
        own, _ = own_series(conn, venue_id, tokens.stablecoins, since)
    series = merge_series(db.token_price_series(conn, token, EXTERNAL_SOURCE, since), own.get(token, []))
    row = conn.execute("""SELECT CASE WHEN lower(token0)=? THEN token0_symbol ELSE token1_symbol END FROM pools
                          WHERE lower(token0)=? OR lower(token1)=? LIMIT 1""", (token, token, token)).fetchone()
    return (row[0] if row and row[0] else "報酬トークン"), series_change(series, int(now.timestamp()), 24)


def today_net(conn: sqlite3.Connection, now: datetime) -> float:
    start = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    row = conn.execute("SELECT SUM(n.net) FROM position_pnl n JOIN positions p ON p.id=n.position_id "
                       "WHERE p.is_paper=1 AND n.ts>=?", (start.isoformat(timespec="seconds"),)).fetchone()
    return float(row[0] or 0.0)


# 「1時間前」の記録として使う範囲: 55分前より前で、90分前より新しいもの（パソコンが止まっていて古い記録しかないときは比べない）
HOUR_AGO_MIN = timedelta(minutes=55)
HOUR_AGO_MAX = timedelta(minutes=90)


def _hour_ago_row(conn: sqlite3.Connection, pool_id: str, snap_ts: str, column: str) -> sqlite3.Row | None:
    t = datetime.fromisoformat(snap_ts)
    return conn.execute(f"SELECT * FROM pool_snapshots WHERE pool_id=? AND ts<=? AND ts>=? AND {column} IS NOT NULL "
                        "ORDER BY ts DESC LIMIT 1",
                        (pool_id, (t - HOUR_AGO_MIN).isoformat(timespec="seconds"),
                         (t - HOUR_AGO_MAX).isoformat(timespec="seconds"))).fetchone()


def _liquidity_at(conn: sqlite3.Connection, pool_id: str, snap_ts: str) -> float | None:
    row = _hour_ago_row(conn, pool_id, snap_ts, "liquidity_total")
    return float(int(row["liquidity_total"])) if row is not None else None


def pool_funds(snap: sqlite3.Row, then: sqlite3.Row | None, dec0: int | None, dec1: int | None
               ) -> tuple[float | None, float | None]:
    """プールのお金（2026-10-01 オーナー決定 A）。今と1時間前のコインの量を、どちらも今の値段で数える（token1 の単位）。

    値段が動いただけでは変わらず、お金が引き出されたときだけ減る。読めないときは None。
    """
    price = snap["price"]
    if dec0 is None or dec1 is None or price is None or not price == price:
        return None, None

    def value(row: sqlite3.Row | None) -> float | None:
        if row is None or row["balance0_raw"] is None or row["balance1_raw"] is None:
            return None
        return int(row["balance0_raw"]) / 10 ** dec0 * float(price) + int(row["balance1_raw"]) / 10 ** dec1

    return value(snap), value(then)


def token_moves(conn: sqlite3.Connection, pos: sqlite3.Row, tokens: TokenBook, snap_ts: str,
                own_tok: dict[str, list[tuple[int, float]]], own_pool: dict[str, list[tuple[int, float]]]
                ) -> tuple[tuple[str, float | None, float | None], ...]:
    """値動きする側（ステーブル以外）のトークンの、1時間と24時間の変化（プール価格から。投げ売りの判定用）。"""
    pool = conn.execute("SELECT token0, token1, token0_symbol, token1_symbol FROM pools WHERE id=?",
                        (pos["pool_id"],)).fetchone()
    if pool is None:
        return ()
    now_s = int(datetime.fromisoformat(snap_ts).timestamp())
    t0, t1 = pool["token0"].lower(), pool["token1"].lower()
    ps = own_pool.get(pos["pool_id"], [])
    out = []
    for tok, sym, other in ((t0, pool["token0_symbol"], t1), (t1, pool["token1_symbol"], t0)):
        if tokens.is_stable(tok):
            continue
        if tokens.is_stable(other) and ps:
            # 相手がステーブルなら、このプールの価格そのもの（token1 建ての token0 の値段）で見る
            series = ps if tok == t0 else [(t, 1 / p) for t, p in ps if p > 0]
        else:
            series = own_tok.get(tok, [])
        out.append((sym or tok[:8], series_change(series, now_s, 1), series_change(series, now_s, 24)))
    return tuple(out)


def position_input(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime, tokens: TokenBook | None = None,
                   own_tok: dict | None = None, own_pool: dict | None = None) -> PositionInput | None:
    snap = conn.execute("SELECT * FROM pool_snapshots WHERE pool_id=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1",
                        (pos["pool_id"],)).fetchone()
    if snap is None:
        return None
    pool = conn.execute("SELECT token0_symbol, token1_symbol, token0_decimals, token1_decimals FROM pools WHERE id=?",
                        (pos["pool_id"],)).fetchone()
    st = json.loads(pos["state_json"] or "{}")
    out_since = st.get("out_since")
    minutes_out = ((datetime.fromisoformat(snap["ts"]) - datetime.fromisoformat(out_since)).total_seconds() / 60
                   if out_since else None)
    score = latest_score(conn, pos["pool_id"])
    pred = json.loads(pos["predicted_json"] or "{}")
    opened = datetime.fromisoformat(pos["opened_at"])
    hours = max(0.0, (datetime.fromisoformat(snap["ts"]) - opened).total_seconds() / 3600)
    window = min(hours, 24.0)
    since = (datetime.fromisoformat(snap["ts"]) - timedelta(hours=window)).isoformat(timespec="seconds")
    inc = conn.execute("SELECT SUM(income) FROM position_pnl WHERE position_id=? AND ts>?",
                       (pos["id"], since)).fetchone()[0]
    funds_now, funds_then = pool_funds(snap, _hour_ago_row(conn, pos["pool_id"], snap["ts"], "balance0_raw"),
                                       pool["token0_decimals"] if pool else None, pool["token1_decimals"] if pool else None)
    return PositionInput(
        pair=f"{pool['token0_symbol']}/{pool['token1_symbol']}" if pool else pos["pool_id"],
        lower=pos["lower"], upper=pos["upper"], price=float(snap["price"]), started_red=bool(pos["started_red"]),
        minutes_out_of_range=minutes_out,
        signal=score["signal"] if score else None,
        rebalance_net_pct=score["net_daily_pct"] if score else None,
        predicted_income_day=pred.get("income"),
        actual_income_day=(float(inc or 0.0) / window * 24) if window > 0 else None,
        income_hours=window,
        liquidity_now=float(int(snap["liquidity_total"])) if snap["liquidity_total"] is not None else None,
        liquidity_1h_ago=_liquidity_at(conn, pos["pool_id"], snap["ts"]),
        funds_now=funds_now, funds_1h_ago=funds_then, funds_unit=(pool["token1_symbol"] or "") if pool else "",
        token_moves=token_moves(conn, pos, tokens, snap["ts"], own_tok or {}, own_pool or {}) if tokens else (),
        hedge_cost_day=(float(st.get("funding_paid", 0.0)) / hours * 24) if hours > 0 and pos["hedges_json"]
        and json.loads(pos["hedges_json"]) else None,
    )


def usdg_prices(conn: sqlite3.Connection, tokens: TokenBook, now: datetime, n: int) -> tuple[float, ...]:
    """USDG（ステーブルコイン）の外部の価格の、新しい順の直近 n 回（1時間より古いものは使わない）。"""
    since = (now - timedelta(hours=1)).astimezone(UTC).isoformat(timespec="seconds")
    out = []
    for tok in sorted(tokens.stablecoins):
        rows = conn.execute("SELECT price FROM stable_prices WHERE token=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                            (tok, since, n)).fetchall()
        prices = tuple(float(r[0]) for r in rows)
        if len(prices) >= n and (not out or max(prices) < max(out)):
            out = list(prices)
    return tuple(out)


def record_stable_price(conn: sqlite3.Connection, gt, tokens: TokenBook, now: datetime) -> dict[str, float]:
    """USDG の外部の価格を GeckoTerminal から取って記録する（取れなければ何もしない＝回数に数えない）。"""
    if gt is None or not tokens.stablecoins:
        return {}
    try:
        got = gt.tokens(tokens.stablecoins)
    except Exception as exc:
        log.warning("stable price fetch failed", extra={"data": {"error": str(exc)}})
        return {}
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    out = {}
    for tok, m in got.items():
        if m.price_usd:
            conn.execute("INSERT OR REPLACE INTO stable_prices(ts, token, price, source) VALUES (?,?,?,?)",
                         (ts, tok, m.price_usd, "geckoterminal"))
            out[tok] = m.price_usd
    conn.commit()
    return out


def run_risk(conn: sqlite3.Connection, config: Config, tokens: TokenBook, ex: PaperExecutor,
             now: datetime | None = None, contract_changes: list[str] | None = None) -> list[int]:
    """持っている建玉を見張る。記録した risk_events の id を返す。"""
    if config.mode != "paper":
        return []
    now = now or datetime.now(UTC)
    s = config.risk
    positions = conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open' ORDER BY id").fetchall()
    if not positions:
        # 建玉がなくても、会場プログラムの変化は記録して、新しく始めるのを止める
        if contract_changes:
            f = check_portfolio(PortfolioInput(contract_changes=tuple(contract_changes)), s)[0]
            return [_emergency(conn, ex, f, now, None, "-", None)]
        return []
    venue_id = positions[0]["venue_id"]
    own_tok, own_pool = own_series(conn, venue_id, tokens.stablecoins, now - timedelta(days=2))
    symbol, change = reward_change_24h(conn, config, tokens, venue_id, now, own_tok)
    pf = PortfolioInput(reward_symbol=symbol, reward_change_24h=change, today_net_usd=today_net(conn, now),
                        open_capital_usd=sum(p["capital"] for p in positions),
                        usdg_prices=usdg_prices(conn, tokens, now, s.emergency_usdg_times),
                        contract_changes=tuple(contract_changes or ()))
    events: list[int] = []

    # 全体の緊急離脱（会場プログラムの変化・USDG・今日の損）
    for f in check_portfolio(pf, s):
        events.append(_emergency(conn, ex, f, now, None, venue_id, None))
        return events

    for pos in positions:
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if pos["status"] != "open":
            continue
        inp = position_input(conn, pos, now, tokens, own_tok, own_pool)
        if inp is None:
            continue
        findings = check_position(inp, pf, s)
        st = json.loads(pos["state_json"] or "{}")
        active_before = set(st.get("risk_active") or [])
        active_now: set[str] = set()
        top = findings[0] if findings else None
        if top and top.level == "emergency":
            # 1つのプールだけの危険は、その建玉だけ閉じる（2026-10-01 オーナー決定 C）。ほかの建玉と評価は続く
            ex.close_position(PositionRef(pos["id"]), reason=f"emergency:{top.kind}")
            events.append(record_event(conn, now, pos["id"], "emergency", top.kind, top.message_ja, "closed_pool",
                                       top.data, pos["venue_id"], pos["pool_id"]))
            continue
        if top and top.level in ("exit", "rebalance"):
            too_high, gas = ex.gas_too_high(pos["pool_id"])
            key = f"skip:{top.kind}"
            if too_high:
                active_now.add(key)
                if key not in active_before:
                    events.append(record_event(
                        conn, now, pos["id"], top.level, top.kind,
                        top.message_ja + f"（ガス代 ${gas:,.2f}/回 が上限 ${s.max_gas_usd_per_tx:,.2f} を超えています）",
                        "skipped_gas", top.data, pos["venue_id"], pos["pool_id"]))
            elif top.level == "exit":
                ex.close_position(PositionRef(pos["id"]), reason=f"risk:{top.kind}")
                events.append(record_event(conn, now, pos["id"], "exit", top.kind, top.message_ja, "closed",
                                           top.data, pos["venue_id"], pos["pool_id"]))
                continue
            else:
                res = ex.rebalance(PositionRef(pos["id"]), reason=top.kind)
                events.append(record_event(
                    conn, now, pos["id"], "rebalance", top.kind,
                    top.message_ja + f"新しいレンジは ±{res['r_pct']:g}%、費用 ${res['cost_usd']:,.2f}。",
                    "rebalanced", {**top.data, **res}, pos["venue_id"], pos["pool_id"]))
        for f in findings:
            if f.level != "caution":
                continue
            active_now.add(f.kind)
            if f.kind not in active_before:
                events.append(record_event(conn, now, pos["id"], "caution", f.kind, f.message_ja, "none", f.data,
                                           pos["venue_id"], pos["pool_id"]))
        # 置き直した建玉は state が新しくなっているので、読み直してから書く
        cur = conn.execute("SELECT status, state_json FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if cur["status"] == "open":
            st = json.loads(cur["state_json"] or "{}")
            st["risk_active"] = sorted(active_now)
            conn.execute("UPDATE positions SET state_json=? WHERE id=?", (json.dumps(st), pos["id"]))
            conn.commit()
    # ボーナスが減ったときの比べ方（2026-09-30 オーナー決定③）。今は記録と知らせだけ
    events += bonus_drop.run(conn, config, ex, now)
    if events:
        log.info("paper risk events", extra={"data": {"events": events}})
    return events


def _emergency(conn: sqlite3.Connection, ex: PaperExecutor, f: Finding, now: datetime, position_id: int | None,
               venue_id: str, pool_id: str | None) -> int:
    closed = close_all(conn, ex, reason=f"emergency:{f.kind}")
    set_stopped(conn, True, f"緊急離脱: {f.message_ja}", now)
    return record_event(conn, now, position_id, "emergency", f.kind, f.message_ja, "closed_all",
                        {**f.data, "closed": closed}, venue_id, pool_id)


# --- オーナーの操作（SPEC 12.5章: 停止・全部閉じる・再開。画面のボタンと Telegram の両方から使う） ------------

def paper_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT stopped, updated_at, reason FROM paper_state WHERE id=1").fetchone()
    return {"stopped": bool(row and row["stopped"]), "since": row["updated_at"] if row else None,
            "reason": row["reason"] if row else None}


def owner_stop(conn: sqlite3.Connection, now: datetime, via: str) -> str:
    if paper_state(conn)["stopped"]:
        return "練習はもう「停止」中です。"
    set_stopped(conn, True, f"オーナーが停止（{via}）", now)
    record_event(conn, now, None, "info", "owner_stop",
                 f"オーナーが練習を停止しました（{via}）。新しい建玉は作れません。持っている建玉の計算と見張りは続けます。",
                 "stopped")
    return "練習を停止しました。新しい建玉は作れません（持っている建玉の計算と見張りは続けます）。再開は /resume。"


def owner_resume(conn: sqlite3.Connection, now: datetime, via: str) -> str:
    if not paper_state(conn)["stopped"]:
        return "練習は止まっていません。"
    set_stopped(conn, False, f"オーナーが再開（{via}）", now)
    record_event(conn, now, None, "info", "owner_resume", f"オーナーが練習を再開しました（{via}）。", "resumed")
    return "練習を再開しました。新しい建玉を作れます。"


def owner_exit_all(conn: sqlite3.Connection, config: Config, tokens: TokenBook, now: datetime, via: str) -> str:
    ex = PaperExecutor(conn, config, tokens, now=now)
    closed = close_all(conn, ex, reason="owner_exit_all")
    set_stopped(conn, True, f"オーナーが全部閉じた（{via}）", now)
    record_event(conn, now, None, "info", "owner_exit_all",
                 f"オーナーが練習の建玉を全部閉じました（{via}。{len(closed)}件）。新しく始めるのも止めています。",
                 "closed_all", {"closed": closed})
    if not closed:
        return "持っている建玉はありませんでした。新しく始めるのは止めています（再開は /resume）。"
    return f"練習の建玉を{len(closed)}件、全部閉じました。新しく始めるのも止めています（再開は /resume）。"
