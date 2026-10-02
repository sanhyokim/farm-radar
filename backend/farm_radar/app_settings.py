"""画面の「設定」で変える値（N2b）。今は狙い利回り（年%）だけ。

狙い利回りはオーナーが決める線（SPEC 13.1 の3）。使うのは、練習での自動の出入り・出るタイミングの知らせ・
一覧の並べ替えだけで、本番で入るかどうかは毎回オーナーが手で決める。リスク上限（config.yaml の limits）とは別のもの。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .config import Config

TARGET_KEY = "target_apr_pct"
TARGET_MIN, TARGET_MAX = 0.0, 1000.0


class SettingsError(ValueError):
    pass


def target_apr_pct(conn: sqlite3.Connection, config: Config) -> float:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (TARGET_KEY,)).fetchone()
    try:
        return float(row[0]) if row else config.opportunities.target_apr_pct
    except (TypeError, ValueError):
        return config.opportunities.target_apr_pct


def view(conn: sqlite3.Connection, config: Config) -> dict[str, Any]:
    row = conn.execute("SELECT updated_at FROM app_settings WHERE key=?", (TARGET_KEY,)).fetchone()
    return {"target_apr_pct": target_apr_pct(conn, config), "default_target_apr_pct": config.opportunities.target_apr_pct,
            "updated_at": row[0] if row else None, "min": TARGET_MIN, "max": TARGET_MAX}


def set_target_apr_pct(conn: sqlite3.Connection, value: Any, now: datetime | None = None) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise SettingsError("狙い利回りは数で入れてください（例: 30）。") from None
    if not TARGET_MIN < x <= TARGET_MAX:
        raise SettingsError(f"狙い利回りは {TARGET_MIN:g} より大きく {TARGET_MAX:g} 以下にしてください（年%）。")
    ts = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (TARGET_KEY, repr(x), ts))
    conn.commit()
    return x
