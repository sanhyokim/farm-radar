"""「予定とメモ」（学ぶタブ）と「見送り中の会場」（会場タブ）（SPEC 7.2・7.5章。2026-09-30 オーナー追加）。

中身は docs/plans.yaml に書き、画面はそれを読んで表示する（ファイルを直せば画面も変わる）。
日付のあるものは、期限の7日前から「近い」（soon）として目立たせる。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .execution import evaluation
from .views import JST

SOON_DAYS = 7


def load(root: Path) -> dict[str, Any]:
    path = root / "docs" / "plans.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _evaluation_end(conn: sqlite3.Connection | None) -> tuple[str | None, str]:
    """進行中（または終わった）評価の終わりの時刻と、その出どころの説明。

    日付はアプリの記録（評価を始めたときの行）から読んだものだけ（2026-10-01 オーナー決定）。
    途中でやめた・中断した評価と、まだ始めていないときは日付なし。
    """
    if conn is None:
        return None, "アプリの記録を読めませんでした"
    cur = evaluation.current(conn)
    if cur is None:
        return None, "アプリの記録に評価がありません（まだ始まっていません）"
    if cur["status"] in ("stopped", "interrupted"):
        what = "途中でやめました" if cur["status"] == "stopped" else "中断しました"
        return None, f"アプリの記録: 前の評価は{what}（もう一度始めると日付が出ます）"
    s, e = (datetime.fromisoformat(cur[k]).astimezone(JST) for k in ("started_at", "ends_at"))
    return cur["ends_at"], (f"アプリの記録: 評価を始めた {s.month}/{s.day} {s:%H:%M}、"
                            f"終わる {e.month}/{e.day} {e:%H:%M}（日本時間）")


def due_state(due: datetime | None, now: datetime) -> tuple[float | None, str | None]:
    """期限までの日数と状態（past=過ぎた / soon=7日以内 / later=まだ先）。期限のないものは (None, None)。"""
    if due is None:
        return None, None
    days = (due - now).total_seconds() / 86400
    if days <= 0:
        return days, "past"
    return days, ("soon" if days <= SOON_DAYS else "later")


def plan_items(root: Path, conn: sqlite3.Connection | None, now: datetime) -> list[dict[str, Any]]:
    out = []
    for p in load(root).get("plans") or []:
        source = p.get("source")
        if p.get("due_from") == "evaluation":
            due_s, rec = _evaluation_end(conn)
            source = f"{source}。{rec}" if source else rec
        else:
            due_s = p.get("due")
        due = datetime.fromisoformat(str(due_s)) if due_s else None
        days, state = due_state(due, now)
        out.append({
            "key": p.get("key"), "group": p.get("group") or "メモ", "title": p.get("title"),
            "text": (p.get("text") or "").strip(), "source": source,
            "due": due.isoformat(timespec="seconds") if due else None, "days_left": days, "state": state,
        })
    return out


def skipped_venues(root: Path) -> list[dict[str, Any]]:
    keys = ("key", "name", "chain", "reason", "decided", "source")
    return [{k: v.get(k) for k in keys} for v in load(root).get("skipped_venues") or []]
