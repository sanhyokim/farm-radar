"""通知する出来事を alerts 表に記録し、まだ送っていないものを Telegram で送る（SPEC 8章・9章）。

alerts 表は「送るものの箱」。記録するときは送らず、send_pending がまとめて送って notified_at を入れる。
Telegram の設定がなくても記録は残るので、あとから画面（/api/alerts）で見られる。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..db import database as db
from ..logging_setup import redact

log = logging.getLogger(__name__)

SIGNAL_MARK = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
KIND_MARK = {"signal_green": "🟢", "signal_drop": "🔻", "reward_rate_drop": "⚠️", "error": "🛠"}
MAX_PER_MESSAGE = 10     # 一度にたくさんあるときは1通にまとめる


class Sender(Protocol):
    def send(self, text: str) -> None: ...


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _reason_short(reason: str | None) -> str:
    """判定の理由（3行）から、通知に入れる短い一言（2行目）を取り出す。"""
    lines = [x for x in (reason or "").split("\n") if x]
    return lines[1] if len(lines) > 1 else (lines[0] if lines else "")


def detect_signal_changes(conn: sqlite3.Connection, venue_id: str, venue_name: str, ts: str) -> int:
    """その回のスコア（ts）と、同じプールの1つ前のスコアを比べて、通知する変化を alerts に記録する。

    - 新しく🟢になった（1つ前が🟢以外、または初めてのスコア）
    - 🟢から🔴に落ちた
    """
    rows = conn.execute(
        """SELECT s.pool_id, s.signal, s.net_daily_pct, s.best_r, s.reason_ja, p.token0_symbol, p.token1_symbol,
                  (SELECT s2.signal FROM scores s2 WHERE s2.pool_id = s.pool_id AND s2.ts < s.ts
                   ORDER BY s2.ts DESC LIMIT 1) AS prev
           FROM scores s JOIN pools p ON p.id = s.pool_id WHERE s.venue_id=? AND s.ts=?""",
        (venue_id, ts),
    ).fetchall()
    added = 0
    for r in rows:
        pair = f"{r['token0_symbol']}/{r['token1_symbol']}"
        if r["signal"] == "green" and r["prev"] != "green":
            kind, level = "signal_green", "info"
            rng = f"（最適レンジ±{r['best_r']:g}%）" if r["best_r"] is not None else ""
            msg = f"🟢 {pair}（{venue_name}）純日利 {_pct(r['net_daily_pct'])}{rng}。理由: {_reason_short(r['reason_ja'])}"
        elif r["signal"] == "red" and r["prev"] == "green":
            kind, level = "signal_drop", "warning"
            msg = f"🟢→🔴 {pair}（{venue_name}）純日利 {_pct(r['net_daily_pct'])}。理由: {_reason_short(r['reason_ja'])}"
        else:
            continue
        added += db.insert_alert(
            conn, ts=datetime.fromisoformat(ts), venue_id=venue_id, pool_id=r["pool_id"], kind=kind, level=level,
            message_ja=msg, data={"prev": r["prev"], "now": r["signal"], "net_daily_pct": r["net_daily_pct"]},
            dedupe_key=f"{kind}:{r['pool_id']}:{ts}",
        )
    conn.commit()
    return added


def record_error(conn: sqlite3.Connection, venue_id: str, what: str, detail: str, *,
                 now: datetime | None = None, repeat_minutes: int = 60) -> bool:
    """エラーを通知の箱に入れる（SPEC 9章）。同じ種類のエラーは repeat_minutes に1回だけ。"""
    now = now or datetime.now(UTC)
    bucket = int(now.timestamp() // (repeat_minutes * 60))
    added = db.insert_alert(
        conn, ts=now, venue_id=venue_id, pool_id=None, kind="error", level="warning",
        message_ja=f"🛠 {what}: {redact(detail)[:300]}", data={"what": what},
        dedupe_key=f"error:{venue_id}:{what}:{bucket}",
    )
    conn.commit()
    return added


def _line(conn: sqlite3.Connection, a: sqlite3.Row) -> str:
    msg = a["message_ja"]
    if a["kind"] == "reward_rate_drop" and a["pool_id"]:
        p = conn.execute("SELECT token0_symbol, token1_symbol FROM pools WHERE id=?", (a["pool_id"],)).fetchone()
        pair = f"{p[0]}/{p[1]}" if p else a["pool_id"]
        msg = f"⚠️ {pair}: {msg}"
    return msg


def send_pending(conn: sqlite3.Connection, sender: Sender | None, *, now: datetime | None = None,
                 max_age_hours: float = 24) -> int:
    """まだ送っていない通知を送る。送れたら notified_at を入れる。送った件数を返す。

    max_age_hours より古いものは送らない（パソコンを長く止めていたとき、古い通知がまとめて届かないように）。
    送れなかったときは notified_at を入れず、次の回にもう一度送る。
    """
    if sender is None:
        return 0
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT * FROM alerts WHERE notified_at IS NULL AND ts>=? ORDER BY ts, id",
                        (since,)).fetchall()
    if not rows:
        return 0
    sent = 0
    for i in range(0, len(rows), MAX_PER_MESSAGE):
        chunk = rows[i:i + MAX_PER_MESSAGE]
        text = "\n\n".join(_line(conn, a) for a in chunk)
        if len(rows) > 1 and i == 0:
            text = f"お知らせ {len(rows)}件\n\n" + text
        try:
            sender.send(text)
        except Exception as exc:
            log.warning("notification send failed", extra={"data": {"error": redact(str(exc))}})
            break
        stamp = now.isoformat(timespec="seconds")
        conn.executemany("UPDATE alerts SET notified_at=? WHERE id=?", [(stamp, a["id"]) for a in chunk])
        conn.commit()
        sent += len(chunk)
    if sent:
        log.info("notifications sent", extra={"data": {"count": sent,
                                                       "kinds": sorted({a['kind'] for a in rows[:sent]})}})
    return sent
