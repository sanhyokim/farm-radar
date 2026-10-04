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

from ..app_settings import target_apr_pct
from ..config import Config, ConfigError, contract_address, load_venue, practice_allowed
from ..db import database as db
from ..risk.rules import LEVEL_JA, STAGE, Finding, PortfolioInput, PositionInput, check_portfolio, check_position
from ..scoring.run import EXTERNAL_SOURCE, merge_series, own_series
from ..tokens import TokenBook
from ..views import JST, series_change
from . import bonus_drop, hedge_guard, loss_lines
from .base import PositionRef
from .paper import REFERENCE, PaperError, PaperExecutor, latest_score, official_sql

log = logging.getLogger(__name__)

ALERT_LEVEL = {"caution": "warning", "rebalance": "info", "exit": "major", "emergency": "major", "info": "info"}
EMOJI = {"caution": "⚠️", "rebalance": "🔁", "exit": "🚪", "emergency": "🚨", "info": "ℹ️"}
# 記録の段階は「注意」でも、強く知らせるもの（2026-10-04 オーナー決定 A: 段階1のプールのお金の減りは知らせて手で出る）
ALERT_KIND = {"pool_funds_drop": "major"}
STAGE1_MARKS = (1, 6, 24)          # 段階1の合図のあと「残っていたら」を記録する時間


def record_event(conn: sqlite3.Connection, now: datetime, position_id: int | None, level: str, kind: str,
                 message_ja: str, action: str, data: dict[str, Any] | None = None, venue_id: str = "-",
                 pool_id: str | None = None) -> int:
    """見張りの記録を1つ書き、通知の箱（alerts）にも入れる。"""
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    if kind in STAGE:
        data = {**(data or {}), "stage": STAGE[kind]}          # 早く出る4段階のどれか（N4b）
    cur = conn.execute("INSERT INTO risk_events(ts, position_id, level, kind, message_ja, action, data_json) "
                       "VALUES (?,?,?,?,?,?,?)", (ts, position_id, level, kind, message_ja, action,
                                                  json.dumps(data or {}, default=str)))
    eid = int(cur.lastrowid)
    label = LEVEL_JA.get(level, level)
    db.insert_alert(conn, ts=now, venue_id=venue_id, pool_id=pool_id, kind=f"paper_{level}",
                    level=ALERT_KIND.get(kind, ALERT_LEVEL[level]),
                    message_ja=f"{EMOJI[level]} 練習（{label}）: {message_ja}{_action_ja(action)}",
                    data={"risk_event": eid}, dedupe_key=f"paper_risk:{eid}")
    conn.commit()
    return eid


def _action_ja(action: str) -> str:
    return {"rebalanced": " → 置き直しました。", "closed": " → この建玉を閉じました。",
            "closed_pool": " → この建玉だけ閉じました（ほかの建玉はそのままです）。",
            "closed_all": " → 全部の建玉を閉じ、新しく始めるのを止めました。",
            "closed_reference": " → 参考の練習だけ閉じました（評価の建玉はそのままです）。",
            "skipped_gas": " → ガス代が高いので見送りました（次の回にもう一度調べます）。",
            "hedge_matched": " → 保険の量を中身に合わせました。",
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


def today_net(conn: sqlite3.Connection, now: datetime, reference: bool | None = None) -> float:
    """今日（日本時間の0時から）の練習の損益。reference=False なら合否用の建玉だけ、True なら参考の練習だけ
    （2026-10-01 案B: 参考の損で評価の建玉まで閉じないように分ける）。"""
    start = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    where = "" if reference is None else (" AND p.purpose='reference'" if reference else f" AND {official_sql('p.')}")
    row = conn.execute("SELECT SUM(n.net) FROM position_pnl n JOIN positions p ON p.id=n.position_id "
                       "WHERE p.is_paper=1 AND n.ts>=?" + where, (start.isoformat(timespec="seconds"),)).fetchone()
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
                   own_tok: dict | None = None, own_pool: dict | None = None, target: float | None = None,
                   below_needed: int = 3) -> PositionInput | None:
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
    value_now, value_then = own_values(conn, pos["id"])
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
        value_now=value_now, value_1h_ago=value_then,
        token_moves=token_moves(conn, pos, tokens, snap["ts"], own_tok or {}, own_pool or {}) if tokens else (),
        hedge_cost_day=(float(st.get("funding_paid", 0.0)) / hours * 24) if hours > 0 and pos["hedges_json"]
        and json.loads(pos["hedges_json"]) else None,
        net_apr_pct=float(score["net_daily_pct"]) * 365 if score and score["net_daily_pct"] is not None else None,
        target_apr_pct=target,
        below_target_count=int(st.get("below_target_n") or 0),
        below_target_needed=below_needed,
        started_below_target=pred.get("net_apr_pct") is not None and pred.get("target_apr_pct") is not None
        and float(pred["net_apr_pct"]) < float(pred["target_apr_pct"]),
    )


def own_values(conn: sqlite3.Connection, position_id: int) -> tuple[float | None, float | None]:
    """自分の建玉の値打ち（今と1時間前。position_pnl の value_usd）。1時間前は、1時間前かそれより前の最後の記録
    （1時間20分より古ければ使わない。始めたばかりで1時間分の記録がないときも None）。"""
    last = conn.execute("SELECT ts, value_usd FROM position_pnl WHERE position_id=? AND value_usd IS NOT NULL "
                        "ORDER BY ts DESC LIMIT 1", (position_id,)).fetchone()
    if last is None:
        return None, None
    t = datetime.fromisoformat(last["ts"])
    then = conn.execute("SELECT ts, value_usd FROM position_pnl WHERE position_id=? AND value_usd IS NOT NULL AND ts<=? "
                        "ORDER BY ts DESC LIMIT 1",
                        (position_id, (t - timedelta(hours=1)).isoformat(timespec="seconds"))).fetchone()
    if then is None or t - datetime.fromisoformat(then["ts"]) > timedelta(minutes=80):
        return float(last["value_usd"]), None
    return float(last["value_usd"]), float(then["value_usd"])


def _exit_value(conn: sqlite3.Connection, ex: PaperExecutor, position_id: int) -> float | None:
    """今出たら手もとに残る額（建玉の値打ち − 閉じる費用の見込み）。閉じた建玉は、閉じたときの値打ち（費用は引いてある）。"""
    pos = conn.execute("SELECT status FROM positions WHERE id=?", (position_id,)).fetchone()
    v, _ = own_values(conn, position_id)
    if pos is None or v is None:
        return None
    if pos["status"] != "open":
        return v
    try:
        return v - ex.estimate_close_cost(PositionRef(position_id))
    except (PaperError, TypeError, ValueError, KeyError):
        log.warning("close cost estimate failed", extra={"data": {"position": position_id}})
        return None


def start_stage1_watch(conn: sqlite3.Connection, ex: PaperExecutor, pos: sqlite3.Row, pair: str, f: Finding,
                       now: datetime) -> None:
    """段階1の合図（プールのお金の減り）のときに出ていたら、の額を記録する（2026-10-04 オーナー決定 A の追加1）。"""
    v, _ = own_values(conn, pos["id"])
    conn.execute("INSERT INTO stage1_watch(position_id, pool_id, pair, ts, drop_pct, funds_1h_ago, unit, value_usd, "
                 "exit_value_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                 (pos["id"], pos["pool_id"], pair, now.astimezone(UTC).isoformat(timespec="seconds"),
                  f.data.get("drop_pct"), f.data.get("funds_1h_ago"), f.data.get("unit"), v,
                  _exit_value(conn, ex, pos["id"])))
    conn.commit()


def update_stage1_watch(conn: sqlite3.Connection, ex: PaperExecutor, now: datetime) -> None:
    """合図のあと 1・6・24 時間たったら「残っていたら（そのときに出たら）」の額を書く。途中で閉じたら閉じたときの額。"""
    rows = conn.execute("SELECT * FROM stage1_watch WHERE stay_24h_usd IS NULL").fetchall()
    for w in rows:
        pos = conn.execute("SELECT status, closed_at, close_reason FROM positions WHERE id=?",
                           (w["position_id"],)).fetchone()
        if pos is None:
            continue
        t0 = datetime.fromisoformat(w["ts"])
        closed = pos["status"] != "open"
        sets: dict[str, Any] = {}
        for h in STAGE1_MARKS:
            col = f"stay_{h}h_usd"
            if w[col] is None and (closed or now >= t0 + timedelta(hours=h)):
                sets[col] = _exit_value(conn, ex, w["position_id"])
        if closed and w["closed_at"] is None:
            sets["closed_at"], sets["close_reason"] = pos["closed_at"], pos["close_reason"]
        if sets:
            conn.execute(f"UPDATE stage1_watch SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                         (*sets.values(), w["id"]))
    conn.commit()


MARGIN_LOG_SPACING = timedelta(minutes=55)     # 預け金の減り方は1時間に1回（15分ごとの見張りの4回に1回）


def log_margin(conn: sqlite3.Connection, pos: sqlite3.Row, ms: dict[str, Any] | None, now: datetime) -> None:
    """保険の預け金の今の状態を記録する（2026-10-04 オーナーのお願い1。1時間に1回）。"""
    if not ms:
        return
    last = conn.execute("SELECT MAX(ts) FROM hedge_margin_log WHERE position_id=?", (pos["id"],)).fetchone()[0]
    if last and now - datetime.fromisoformat(last) < MARGIN_LOG_SPACING:
        return
    st = json.loads(pos["state_json"] or "{}")
    conn.execute("INSERT INTO hedge_margin_log(position_id, ts, margin_usd, hedge_pnl_usd, equity_usd, maintenance_usd, "
                 "buffer_frac, notional_usd, rebalances) VALUES (?,?,?,?,?,?,?,?,?)",
                 (pos["id"], now.isoformat(timespec="seconds"), ms["margin_usd"], ms["hedge_pnl_usd"], ms["equity_usd"],
                  ms["maintenance_usd"], ms["buffer_frac"], ms["notional_usd"], int(st.get("rebalances", 0))))
    conn.commit()


def record_topup(conn: sqlite3.Connection, ex: PaperExecutor, pos: sqlite3.Row, ms: dict[str, Any] | None,
                 line_frac: float, now: datetime) -> dict[str, Any] | None:
    """預け金を「足したとしたら」を記録する（2026-10-04 オーナー決定 ③A。本物のお金を始めるまでは実際には足さない）。

    前に足したとしたらの分も入れた余裕が、はじめの余裕の line_frac を切ったら、はじめの余裕まで戻す額を1回と数える。
    練習の建玉そのもの（プール・売りの量）は変えない。そのため、売りを減らしたあとの損の減り方は入っていない（回数は多めに出る）。
    """
    if not ms or not ms.get("buffer_initial_usd") or ms["buffer_initial_usd"] <= 0:
        return None
    added = conn.execute("SELECT COALESCE(SUM(topup_usd), 0) FROM hedge_topup_log WHERE position_id=?",
                         (pos["id"],)).fetchone()[0]
    initial = ms["buffer_initial_usd"]
    frac = (ms["buffer_usd"] + added) / initial
    if frac >= line_frac:
        return None
    topup = initial - (ms["buffer_usd"] + added)
    try:
        est = ex.topup_estimate(PositionRef(pos["id"]), topup)
    except Exception:  # noqa: BLE001  見込みが出せなくても、回数と額は残す
        log.warning("topup estimate failed", extra={"data": {"position": pos["id"]}})
        est = {}
    conn.execute("INSERT INTO hedge_topup_log(position_id, ts, buffer_frac, line_frac, topup_usd, pool_usd, "
                 "volatile_before_usd, volatile_after_usd, short_before_usd, short_after_usd, cost_usd, detail_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pos["id"], now.isoformat(timespec="seconds"), frac, line_frac, topup, est.get("pool_usd"),
                  est.get("volatile_before_usd"), est.get("volatile_after_usd"), est.get("short_before_usd"),
                  est.get("short_after_usd"), est.get("cost_usd"), json.dumps(est.get("parts") or {})))
    conn.commit()
    return {"topup_usd": topup, "buffer_frac": frac, **est}


def topup_rows(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """画面・まとめ用: 建玉ごとの「足したとしたら」の回数・合計の額・費用と、最後の回の中身。"""
    out = []
    for r in conn.execute("""SELECT position_id, COUNT(*) n, SUM(topup_usd) total, SUM(cost_usd) cost, MAX(ts) at
                             FROM hedge_topup_log GROUP BY position_id ORDER BY MAX(ts) DESC LIMIT ?""", (limit,)):
        last = conn.execute("SELECT * FROM hedge_topup_log WHERE position_id=? ORDER BY ts DESC, id DESC LIMIT 1",
                            (r["position_id"],)).fetchone()
        pos = conn.execute("SELECT p.status, s.token0_symbol, s.token1_symbol FROM positions p "
                           "LEFT JOIN pools s ON s.id = p.pool_id WHERE p.id=?", (r["position_id"],)).fetchone()
        out.append({"position_id": r["position_id"], "pair": f"{pos['token0_symbol']}/{pos['token1_symbol']}" if pos else None,
                    "status": pos["status"] if pos else None, "count": r["n"], "total_usd": r["total"],
                    "cost_usd": r["cost"], "at": r["at"], "last": {k: last[k] for k in last.keys() if k != "detail_json"}})
    return out


def margin_log_rows(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """画面・まとめ用: 建玉ごとの、預け金の始め・今・いちばん低いとき・減った割合・置き直しの回数（新しい建玉から）。"""
    out = []
    pids = [r[0] for r in conn.execute("SELECT position_id FROM hedge_margin_log GROUP BY position_id "
                                       "ORDER BY MAX(ts) DESC LIMIT ?", (limit,))]
    for pid in pids:
        rows = conn.execute("SELECT * FROM hedge_margin_log WHERE position_id=? ORDER BY ts", (pid,)).fetchall()
        pos = conn.execute("SELECT p.status, p.pool_id, s.token0_symbol, s.token1_symbol FROM positions p "
                           "LEFT JOIN pools s ON s.id = p.pool_id WHERE p.id=?", (pid,)).fetchone()
        first, last = rows[0], rows[-1]
        low = min(rows, key=lambda r: r["equity_usd"] if r["equity_usd"] is not None else float("inf"))
        margin = first["margin_usd"] or 0.0
        out.append({
            "position_id": pid, "pair": f"{pos['token0_symbol']}/{pos['token1_symbol']}" if pos else None,
            "status": pos["status"] if pos else None, "since": first["ts"], "at": last["ts"], "points": len(rows),
            "margin_usd": margin, "equity_usd": last["equity_usd"], "low_equity_usd": low["equity_usd"], "low_at": low["ts"],
            "change_pct": (last["equity_usd"] / margin - 1) * 100 if margin and last["equity_usd"] is not None else None,
            "buffer_frac": last["buffer_frac"], "rebalances": last["rebalances"],
            # 置き直し1回あたりの減り（ドル。置き直しがあったときだけ）
            "per_rebalance_usd": ((last["equity_usd"] - margin) / last["rebalances"])
            if last["rebalances"] and last["equity_usd"] is not None else None,
        })
    return out


def stage1_watch_rows(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """画面・まとめ用: 新しい順。差（残っていたら − 出ていたら）も付ける。"""
    out = []
    for w in conn.execute("SELECT * FROM stage1_watch ORDER BY ts DESC LIMIT ?", (limit,)).fetchall():
        d = dict(w)
        ex_v = d["exit_value_usd"]
        for h in STAGE1_MARKS:
            st = d[f"stay_{h}h_usd"]
            d[f"diff_{h}h_usd"] = None if st is None or ex_v is None else st - ex_v
        out.append(d)
    return out


# 段階2の「続けて下回った」は、15分ごとの見張り1回を1回と数える（市場が開くころの5分ごとの見張りで早く数えすぎないように）
BELOW_COUNT_SPACING = timedelta(minutes=14)


def count_below_target(conn: sqlite3.Connection, pos: sqlite3.Row, target: float | None) -> None:
    """最新のスコアの残る利回りが狙い利回りより低ければ、続けて下回った回数を1つ増やす（上なら0に戻す）。state に書く。"""
    score = latest_score(conn, pos["pool_id"])
    if target is None or score is None or score["net_daily_pct"] is None:
        return
    st = json.loads(pos["state_json"] or "{}")
    last = st.get("below_target_ts")
    if last and datetime.fromisoformat(score["ts"]) - datetime.fromisoformat(last) < BELOW_COUNT_SPACING:
        return
    below = float(score["net_daily_pct"]) * 365 < target
    st["below_target_n"] = int(st.get("below_target_n") or 0) + 1 if below else 0
    st["below_target_ts"] = score["ts"]
    conn.execute("UPDATE positions SET state_json=? WHERE id=?", (json.dumps(st), pos["id"]))
    conn.commit()


def better_place(conn: sqlite3.Connection, config: Config, ex: PaperExecutor, pos: sqlite3.Row, now: datetime,
                 target: float | None) -> Finding | None:
    """段階4（N4b）: もっと良い場所があるか。（利回りの差 × 残りの日数）が、移る費用の guard.better_cost_multiple 倍より大きければ出る。

    比べる先は、練習ができる会場（自分で読む会場）のほかのプールで、判定が🔴でなく、狙い利回り以上のもの。
    残りの日数は、比べる先の会場の次の切り替えまで（その先のボーナスは分からないので数えない）。
    移る費用 = 今の建玉を閉じる費用の見込み ＋ 比べる先で始める費用（両替の手数料とずれ・ガス代2回）。
    移った先で練習を始めるのは自分で（アプリ任せの練習は N6）。
    """
    from .bonus_drop import _next_flip

    cur = latest_score(conn, pos["pool_id"])
    if cur is None or cur["net_daily_pct"] is None:
        return None
    open_pools = {r[0] for r in conn.execute("SELECT pool_id FROM positions WHERE is_paper=1 AND status='open'")}
    venues = []
    for vid in config.venues:
        try:
            if practice_allowed(load_venue(vid, config.root)):
                venues.append(vid)
        except (OSError, ConfigError):
            continue
    if not venues:
        return None
    rows = conn.execute(
        f"""SELECT s.* FROM scores s JOIN pools p ON p.id = s.pool_id
            WHERE p.venue_id IN ({",".join("?" * len(venues))}) AND s.ts = (SELECT MAX(ts) FROM scores WHERE pool_id = s.pool_id)
              AND s.net_daily_pct IS NOT NULL AND COALESCE(s.signal, '') <> 'red'
            ORDER BY s.net_daily_pct DESC LIMIT 5""", venues).fetchall()
    capital = float(pos["capital"])
    s = config.scoring
    try:
        close_cost = ex.estimate_close_cost(PositionRef(pos["id"]))
    except PaperError:
        return None
    for alt in rows:
        if alt["pool_id"] in open_pools or alt["pool_id"] == pos["pool_id"]:
            continue
        alt_apr = float(alt["net_daily_pct"]) * 365
        if target is not None and alt_apr < target:
            continue
        diff_day = (float(alt["net_daily_pct"]) - float(cur["net_daily_pct"])) / 100 * capital
        if diff_day <= 0:
            continue
        snap = conn.execute("SELECT * FROM pool_snapshots WHERE pool_id=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1",
                            (alt["pool_id"],)).fetchone()
        p = conn.execute("SELECT venue_id, token0_symbol, token1_symbol FROM pools WHERE id=?", (alt["pool_id"],)).fetchone()
        flip = _next_flip(config, p["venue_id"], now, snap) if snap is not None and p is not None else None
        if flip is None:
            continue
        days = max(0.0, (flip - now).total_seconds() / 86400)
        alt_details = json.loads(alt["details_json"] or "{}")
        inp = alt_details.get("inputs") or {}
        fee, slip, gas = float(inp.get("fee") or 0.0), float(inp.get("slippage") or 0.0), float(inp.get("gas_usd_per_tx") or 0.0)
        # 移った先でプールに置く割合（スコアの分け方。古い記録で無ければ前の決め打ち）
        lp_share = float((alt_details.get("split") or {}).get("lp") or s.allocation_lp)
        open_cost = capital * lp_share * s.swap_ratio * (fee + slip) + 2 * gas
        move_cost = close_cost + open_cost
        gain = diff_day * days
        mult = config.guard.better_cost_multiple
        if gain > mult * move_cost and gain > 0:
            pair = f"{p['token0_symbol']}/{p['token1_symbol']}"
            cur_apr = float(cur["net_daily_pct"]) * 365
            return Finding("exit", "better_place",
                           f"{pair} の残る利回り（年{alt_apr:.1f}%）が、今の場所（年{cur_apr:.1f}%）より1日 ${diff_day:,.2f} 多く、"
                           f"次の切り替えまでの{days:.1f}日で ${gain:,.2f} です。移る費用 ${move_cost:,.2f} の{mult:g}倍より大きいので出ます"
                           "（移った先の練習は「探す」から自分で始めます。アプリが自動で移るのは N6）。",
                           {"to_pool": alt["pool_id"], "to_pair": pair, "to_apr_pct": alt_apr, "cur_apr_pct": cur_apr,
                            "diff_usd_day": diff_day, "days": days, "gain_usd": gain, "move_cost_usd": move_cost,
                            "close_cost_usd": close_cost, "open_cost_usd": open_cost, "multiple": mult})
    return None


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
    # 段階1の合図のあとの「残っていたら」（2026-10-04 オーナー決定 A の追加1）。建玉がなくなっても書く
    update_stage1_watch(conn, ex, now)
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
    # 今日の損は、合否用の建玉と参考の練習で分けて数える（2026-10-01 案B）
    refs = [p for p in positions if (p["purpose"] or "") == REFERENCE]
    mains = [p for p in positions if (p["purpose"] or "") != REFERENCE]
    # 今日の損で全部閉じる決まりは、損の線（N4b。guard.loss_lines の 1日・すべて止める）に置きかえた
    pf = PortfolioInput(reward_symbol=symbol, reward_change_24h=change, today_net_usd=None,
                        open_capital_usd=sum(p["capital"] for p in mains),
                        usdg_prices=usdg_prices(conn, tokens, now, s.emergency_usdg_times),
                        contract_changes=tuple(contract_changes or ()))
    events: list[int] = []
    target = target_apr_pct(conn, config)
    mmf_table = hedge_guard._mmf_table(config.feeds.database_path)

    # 全体の緊急離脱（会場プログラムの変化・USDG・今日の損）
    for f in check_portfolio(pf, s):
        events.append(_emergency(conn, ex, f, now, None, venue_id, None))
        return events
    # 損の線（N4b）: 注意・新しく入らない は記録して知らせる。すべて止める は全部閉じて、新しく始めるのも止める
    loss = loss_lines.status(conn, config.guard, now)
    events += _loss_line_events(conn, ex, loss, now, venue_id)
    if loss["level"] == "stop":
        return events
    # 参考の練習の今日の損が基準を超えたら、参考の練習だけ閉じる（評価の建玉と、新しく始められるかはそのまま）
    if refs:
        ref_pf = PortfolioInput(today_net_usd=today_net(conn, now, True), open_capital_usd=sum(p["capital"] for p in refs))
        for f in check_portfolio(ref_pf, s):
            closed = []
            for p in refs:
                try:
                    ex.close_position(PositionRef(p["id"]), reason=f"risk:{f.kind}")
                    closed.append(p["id"])
                except PaperError:
                    log.warning("close failed", extra={"data": {"position": p["id"]}})
            events.append(record_event(conn, now, None, "exit", f.kind, "参考の練習: " + f.message_ja,
                                       "closed_reference", {**f.data, "closed": closed}, venue_id, None))
            positions = mains
            break

    for pos in positions:
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if pos["status"] != "open":
            continue
        count_below_target(conn, pos, target)
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        inp = position_input(conn, pos, now, tokens, own_tok, own_pool, target, config.guard.below_target_times)
        if inp is None:
            continue
        findings = check_position(inp, pf, s)
        # 保険の強制決済までの余裕（N4b）。0以下なら段階1（すぐ閉じる）、はじめの半分を切ったら注意
        ms = hedge_guard.margin_status(conn, config, pos, mmf_table=mmf_table)
        log_margin(conn, pos, ms, now)
        topup = record_topup(conn, ex, pos, ms, config.guard.hedge_topup_buffer_frac, now)
        if topup is not None:
            events.append(record_event(
                conn, now, pos["id"], "caution", "hedge_topup_if",
                f"{inp.pair}: 保険の預け金の余裕がはじめの{config.guard.hedge_topup_buffer_frac * 100:g}%を切りました。"
                f"本物のお金なら ${topup['topup_usd']:,.2f} を足すところです（練習では足さず、記録だけ。"
                f"見込みの費用 ${topup.get('cost_usd') or 0:,.2f}、売りは ${topup.get('short_before_usd') or 0:,.0f} → "
                f"${topup.get('short_after_usd') or 0:,.0f} に減らす）。",
                "recorded", {k: v for k, v in topup.items() if k != "parts"}, pos["venue_id"], pos["pool_id"]))
        if ms and ms["state"] == "liquidated":
            findings = [Finding("emergency", "hedge_liquidation", hedge_guard.message_ja(inp.pair, ms), ms), *findings]
        elif ms and ms["state"] == "alert":
            findings = sorted([*findings, Finding("caution", "hedge_margin_low", hedge_guard.message_ja(inp.pair, ms), ms)],
                              key=lambda f: -f.rank)
        if not any(f.level in ("exit", "emergency") for f in findings):
            better = better_place(conn, config, ex, pos, now, target)
            if better is not None:
                findings = sorted([*findings, better], key=lambda f: -f.rank)
        st = json.loads(pos["state_json"] or "{}")
        active_before = set(st.get("risk_active") or [])
        active_now: set[str] = set()
        top = findings[0] if findings else None
        rebalanced = False
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
                rebalanced = True
                res = ex.rebalance(PositionRef(pos["id"]), reason=top.kind)
                events.append(record_event(
                    conn, now, pos["id"], "rebalance", top.kind,
                    top.message_ja + f"新しいレンジは ±{res['r_pct']:g}%、費用 ${res['cost_usd']:,.2f}。",
                    "rebalanced", {**top.data, **res}, pos["venue_id"], pos["pool_id"]))
        # 値段が幅から出たときの保険（N4b・追加の決定 11）: 境目の余裕の外に30分いたら中身に合わせる。直したあと1時間は直さない
        cur = conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if cur["status"] == "open" and not rebalanced:
            pool = ex.market.pool(cur["pool_id"])
            why = hedge_guard.adjust_due(cur, pool, inp.price, now, config)
            if why:
                res = ex.match_hedges(PositionRef(cur["id"]), reason=why)
                side = {"above": "幅の上に出たまま30分たったので、保険を閉じました（中身は値動きしないコインだけです）",
                        "below": "幅の下に出たまま30分たったので、保険を中身に合わせて大きくしました",
                        "inside": "値段が幅の中に戻って30分たったので、保険を中身に合わせて戻しました"}[why]
                events.append(record_event(
                    conn, now, cur["id"], "rebalance", "hedge_adjust",
                    f"{inp.pair}: {side}（境目の余裕は幅の{config.guard.hedge_edge_buffer_frac * 100:g}%。費用 ${res['fee_usd']:,.2f}）。",
                    "hedge_matched", {"zone": why, **res}, cur["venue_id"], cur["pool_id"]))
        for f in findings:
            if f.level != "caution":
                continue
            active_now.add(f.kind)
            if f.kind not in active_before:
                events.append(record_event(conn, now, pos["id"], "caution", f.kind, f.message_ja, "none", f.data,
                                           pos["venue_id"], pos["pool_id"]))
                if f.kind == "pool_funds_drop":
                    start_stage1_watch(conn, ex, pos, inp.pair, f, now)
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


def _loss_line_events(conn: sqlite3.Connection, ex: PaperExecutor, loss: dict[str, Any], now: datetime,
                      venue_id: str) -> list[int]:
    """損の線を越えたときの記録（内訳つき）。同じ線を越えたままのあいだは、最初の1回だけ記録する。"""
    before = loss_lines.active(conn)
    keys: set[str] = set()
    out: list[int] = []
    for row in loss["periods"]:
        level = row["level"]
        if level is None:
            continue
        key = f"{row['period']}:{level}"
        keys.add(key)
        data = {"period": row["period"], "level": level, "pct": row["pct"], "net_usd": row["net_usd"],
                "base_usd": row["base_usd"], "line_pct": row["lines"][level]["pct"], "since": row["since"],
                "breakdown": row["breakdown"], "main_cause": row["main_cause"], "provisional": True}
        if level == "stop":
            closed = close_all(conn, ex, reason=f"loss_line:{row['period']}")
            set_stopped(conn, True, f"損の線: {loss_lines.message_ja(row, level)}", now)
            out.append(record_event(conn, now, None, "emergency", "loss_line_stop", loss_lines.message_ja(row, level),
                                    "closed_all", {**data, "closed": closed}, venue_id, None))
            loss_lines.set_active(conn, keys, now)
            return out
        if key not in before:
            extra = "線より戻るまで、新しい練習は始めません。" if level == "no_new" else ""
            out.append(record_event(conn, now, None, "caution", f"loss_line_{level}",
                                    loss_lines.message_ja(row, level) + extra, "none", data, venue_id, None))
    loss_lines.set_active(conn, keys, now)
    return out


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
