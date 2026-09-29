"""通知のまとめ役。収集プロセス（scheduler）から呼ぶ。

- 送信: alerts 表の「まだ送っていないもの」を1分ごとに送る
- 毎朝のレポート: 日本時間 8:00（config の notify）。パソコンが寝ていて遅れたら、起きたときに送る
- /status などのコマンド: Telegram のロングポーリングで受け取る（オーナーの ID だけ）
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import database as db
from ..logging_setup import redact
from . import events, report
from .telegram import HELP_COMMANDS, Telegram, settings_from_env

log = logging.getLogger(__name__)


def _home() -> dict[str, Any]:
    from .. import api          # 画面のホームと同じデータを使う（ここで読み込むのは、起動を軽くするため）
    return api.home()


class Notifier:
    def __init__(self, config: Config, telegram: Telegram | None,
                 home: Callable[[], dict[str, Any]] = _home, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.config = config
        self.telegram = telegram
        self.home = home
        self.now = now
        self._offset: int | None = None

    @classmethod
    def from_env(cls, config: Config, env: Mapping[str, str] | None = None) -> Notifier:
        s = settings_from_env(os.environ if env is None else env)
        if s is None:
            log.info("telegram not configured; alerts are recorded but not sent")
            return cls(config, None)
        log.info("telegram configured", extra={"data": {"owner_id_set": True}})
        return cls(config, Telegram(s))

    def _conn(self):
        return db.connect(Path(self.config.database_path))

    # --- 送信 ---------------------------------------------------------------------------

    def tick(self) -> None:
        """送っていないお知らせと、送れていない今日のレポートを送る。"""
        conn = self._conn()
        try:
            events.send_pending(conn, self.telegram, now=self.now(), max_age_hours=self.config.notify.max_age_hours)
            self._send_unsent_report(conn)
        finally:
            conn.close()

    def error(self, venue_id: str, what: str, detail: str) -> None:
        conn = self._conn()
        try:
            events.record_error(conn, venue_id, what, detail, now=self.now(),
                                repeat_minutes=self.config.notify.error_repeat_minutes)
        finally:
            conn.close()

    # --- 毎朝のレポート ---------------------------------------------------------------------

    def daily_report(self) -> bool:
        """今日のレポートを作って送る。今日の分がもうあれば作らない。作ったら True。"""
        now = self.now()
        conn = self._conn()
        try:
            if conn.execute("SELECT 1 FROM daily_reports WHERE day=?", (report.jst_day(now),)).fetchone():
                self._send_unsent_report(conn)
                return False
            top = report.top_scores(conn, self.config.notify.daily_report_top)
            body, learning = report.build_daily_report(conn, self.home(), top, now)
            made = report.save_daily_report(conn, now, body, learning, top[0]["pool_id"] if top else None)
            log.info("daily report created", extra={"data": {"day": report.jst_day(now),
                                                             "learning": learning[0] if learning else None}})
            self._send_unsent_report(conn)
            return made
        finally:
            conn.close()

    def report_due(self) -> bool:
        """起動したとき、今日のレポートの時刻を過ぎているのにまだ作っていなければ True。"""
        n = self.config.notify
        local = self.now().astimezone(report.JST)
        if (local.hour, local.minute) < (n.daily_report_hour_jst, n.daily_report_minute):
            return False
        conn = self._conn()
        try:
            return conn.execute("SELECT 1 FROM daily_reports WHERE day=?", (report.jst_day(self.now()),)).fetchone() is None
        finally:
            conn.close()

    def _send_unsent_report(self, conn) -> None:
        if self.telegram is None:
            return
        row = conn.execute("SELECT day, body_ja FROM daily_reports WHERE day=? AND sent_at IS NULL",
                           (report.jst_day(self.now()),)).fetchone()
        if row is None:
            return
        try:
            self.telegram.send(row["body_ja"])
        except Exception as exc:
            log.warning("daily report send failed", extra={"data": {"error": redact(str(exc))}})
            return
        conn.execute("UPDATE daily_reports SET sent_at=? WHERE day=?",
                     (self.now().isoformat(timespec="seconds"), row["day"]))
        conn.commit()
        log.info("daily report sent", extra={"data": {"day": row["day"]}})

    # --- コマンド（SPEC 12.5章） -------------------------------------------------------------

    def answer(self, text: str) -> str:
        """オーナーから届いたコマンドへの返事。"""
        cmd = (text or "").strip().split()[0].split("@")[0].lower() if (text or "").strip() else ""
        if cmd == "/status":
            return report.status_text(self.home(), self.now())
        if cmd == "/report":
            conn = self._conn()
            try:
                row = conn.execute("SELECT body_ja FROM daily_reports ORDER BY day DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            return row["body_ja"] if row else "まだ朝のレポートがありません。毎朝8時（日本時間）に作ります。"
        if cmd in ("/stop", "/exit_all", "/resume"):
            return report.M5_ONLY
        return report.HELP

    def handle_update(self, u: dict[str, Any]) -> str | None:
        """届いた1件を処理する。オーナー以外からのものは無視する（返事もしない）。返した文を返す。"""
        msg = u.get("message") or {}
        sender = (msg.get("from") or {}).get("id")
        chat = (msg.get("chat") or {}).get("id")
        if self.telegram is None or sender != self.telegram.owner_id or chat != self.telegram.owner_id:
            log.warning("telegram message ignored (not the owner)")
            return None
        reply = self.answer(msg.get("text") or "")
        self.telegram.send(reply)
        return reply

    def poll_once(self, wait_seconds: int = 25) -> int:
        if self.telegram is None:
            return 0
        ups = self.telegram.updates(self._offset, wait_seconds)
        for u in ups:
            self._offset = int(u["update_id"]) + 1
            try:
                self.handle_update(u)
            except Exception as exc:
                log.warning("telegram command failed", extra={"data": {"error": redact(str(exc))}})
        return len(ups)

    def run_bot(self, stop: threading.Event) -> None:
        """コマンドを待ち続ける（別のスレッドで動かす）。通信が切れても、少し待ってやり直す。"""
        if self.telegram is None:
            return
        delay = 5.0
        log.info("telegram bot polling started", extra={"data": {"commands": HELP_COMMANDS}})
        while not stop.is_set():
            try:
                self.poll_once()
                delay = 5.0
            except Exception as exc:
                log.warning("telegram polling failed", extra={"data": {"error": redact(str(exc)),
                                                                       "retry_seconds": delay}})
                stop.wait(delay)
                delay = min(delay * 2, 300.0)
