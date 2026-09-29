"""回数制限（429）を守るための共通の仕組み（2026-09-30 オーナー条件。M6。SPEC 5.1章・5.3章）。

- 同じサイトへの呼び出しは、会場やジョブが別でも1つの間隔を共有する（このプロセスの中で）。
  会場ごとに GeckoTerminal の読み手を作っても、合わせて「6秒に1回」を超えない。
- 429 を受けたら、時刻とサイトをデータベースの rate_limits 表に書く（収集とAPIのどちらのプロセスでも）。
  状態ページに24時間の回数を出し、直近に 429 があれば観察だけの会場の読み取りを休む。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_lock = threading.Lock()
_next_ok: dict[str, float] = {}         # サイト → 次に呼んでよい時刻（time.monotonic）
_sink: Path | str | None = None          # 429 を書くデータベース


def host_of(url: str) -> str:
    """URL のサイト名だけ（パスや鍵、ユーザー名は含めない）。"""
    return (urlparse(url).hostname or "").lower() or "unknown"


def wait_turn(host: str, min_interval: float, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> None:
    """同じサイトへの前の呼び出しから min_interval 秒たつまで待つ（ほかの読み手の分も数える）。"""
    if min_interval <= 0:
        return
    with _lock:
        now = clock()
        at = max(now, _next_ok.get(host, 0.0))
        _next_ok[host] = at + min_interval     # 次の人の番を先に予約してから待つ
    if at > now:
        sleep(at - now)


def reset() -> None:
    """間隔の記録を消す（テスト用。偽の時計を使うテストどうしが影響しないように）。"""
    with _lock:
        _next_ok.clear()


def set_sink(db_path: Path | str | None) -> None:
    """429 を書くデータベースを決める（起動時に1回）。None なら書かない（テストなど）。"""
    global _sink
    _sink = db_path


def record(host: str, kind: str, now: datetime | None = None) -> None:
    """429 を受けた。ログに出し、データベースに1行書く（書けなくても処理は止めない）。"""
    log.warning("rate limited", extra={"data": {"host": host, "kind": kind}})
    if _sink is None:
        return
    try:
        conn = sqlite3.connect(str(_sink), timeout=10)
        try:
            conn.execute("INSERT INTO rate_limits(ts, host, kind) VALUES (?,?,?)",
                         ((now or datetime.now(UTC)).isoformat(timespec="seconds"), host, kind))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("rate limit record failed", extra={"data": {"error": str(exc)}})


def recent(conn: sqlite3.Connection, now: datetime, minutes: float) -> list[sqlite3.Row]:
    """直近 minutes 分の 429 の記録。"""
    since = (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return conn.execute("SELECT * FROM rate_limits WHERE ts>=? ORDER BY ts", (since,)).fetchall()


def counts_24h(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """直近24時間の 429 の回数（サイトごと）。"""
    out: dict[str, int] = {}
    for r in recent(conn, now, 24 * 60):
        out[r["host"]] = out.get(r["host"], 0) + 1
    return out
