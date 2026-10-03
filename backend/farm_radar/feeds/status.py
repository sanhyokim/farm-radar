"""一覧の保存の状態（画面のホームの「一覧の保存」と /api/feeds/status。N2a）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import FeedSettings
from . import store
from .sources import CADENCE_JA, SOURCES

STATUS_JA = {"ok": "保存できた", "error": "失敗（次の回にもう一度読みます）", "rate_limited": "不明（回数制限。次の回にもう一度）",
             "running": "読んでいる", "stuck": "止まった（次の回にもう一度読みます）", "none": "まだ"}
# この時間より前の成功が最後なら「遅れている」（決めた間隔の2回分 + ゆとり）
LATE_AFTER = {"15min": timedelta(minutes=40), "hourly": timedelta(hours=2, minutes=30), "daily": timedelta(hours=50)}
STUCK_AFTER = timedelta(minutes=30)
# 「新しく出てきたもの」に並べる一覧（DefiLlama の利回りは毎日たくさん増えるので、数だけ出す）
LIST_SOURCES = ("merkl_opportunities", "aero_articles", "aero_addresses", "merkl_chains", "merkl_protocols", "llama_chains",
                "llama_protocols", "across_chains", "relay_chains", "lifi_chains")
# Aero の開始（公式の発表の中の画像「TAKE OFF OCT 21 / 2026 8:00 PM EDT / OCT 22 00:00 UTC」。2026-10-01 確認。SPEC 13.1）
AERO_START = {"jst": "2026-10-22 09:00", "utc": "2026-10-22T00:00:00+00:00",
              "source": "https://aero.xyz/articles/aero-launch-update-all-systems-go/"}


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def status(settings: FeedSettings, now: datetime | None = None, new_limit: int = 30) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if not settings.database_path.exists():
        return {"enabled": False, "text": "一覧の保存はこのパソコンではまだ動いていません。", "sources": [], "new": [],
                "aero": {"start": AERO_START, "articles": []}}
    conn = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return _status(conn, settings, now, new_limit)
    finally:
        conn.close()


def _stored(conn: sqlite3.Connection, source_id: str) -> dict[str, int] | None:
    """一覧の中身を表に積み上げる読み取り（会場の見分け・金庫の見張り）の、今までにたまった数。

    この2つは、答えが出たものを次の回に読み直さない（会場の見分けは30日）ので、「最後の回に読んだ数」は0になることがある。
    たまった数を別に出す（2026-10-03 オーナー: venue_checks 0 の意味が分からない）。
    """
    table = {"venue_checks": "venue_checks", "vault_states": "vault_states"}.get(source_id)
    if table is None or not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                         (table,)).fetchone():
        return None
    if table == "venue_checks":
        n, ok = conn.execute("SELECT COUNT(*), COALESCE(SUM(verified = 1), 0) FROM venue_checks "
                             "WHERE verified IS NOT NULL").fetchone()
        return {"checked": int(n), "verified": int(ok)}
    n, err = conn.execute("SELECT COUNT(*), COALESCE(SUM(error IS NOT NULL), 0) FROM vault_states").fetchone()
    return {"vaults": int(n), "errors": int(err)}


def _status(conn: sqlite3.Connection, settings: FeedSettings, now: datetime, new_limit: int) -> dict[str, Any]:
    today_start = now.astimezone(store.JST).replace(hour=0, minute=0, second=0, microsecond=0)
    since_new = store.iso(now - timedelta(hours=48))
    rows = []
    any_problem = False
    for s in SOURCES:
        last = conn.execute("SELECT * FROM feed_runs WHERE source=? ORDER BY id DESC LIMIT 1", (s.id,)).fetchone()
        ok = conn.execute("SELECT * FROM feed_runs WHERE source=? AND status='ok' ORDER BY id DESC LIMIT 1",
                          (s.id,)).fetchone()
        state = last["status"] if last else "none"
        if state == "running" and now - _ts(last["started_at"]) > STUCK_AFTER:
            state = "stuck"
        late_after = max(LATE_AFTER[s.cadence], timedelta(minutes=2 * s.every_minutes + 40)) if s.every_minutes \
            else LATE_AFTER[s.cadence]
        late = ok is None or now - _ts(ok["started_at"]) > late_after
        new_today = conn.execute("SELECT COUNT(*) FROM feed_items WHERE source=? AND baseline=0 AND first_seen>=?",
                                 (s.id, store.iso(today_start))).fetchone()[0]
        any_problem = any_problem or late or state in ("error", "rate_limited", "stuck")
        rows.append({
            "id": s.id, "label": s.label_ja, "cadence": s.cadence, "cadence_ja": "約2時間ごと" if s.every_minutes >= 110 else CADENCE_JA["hourly"] if s.every_minutes >= 55
            else CADENCE_JA[s.cadence],
            "status": state, "status_ja": STATUS_JA[state], "late": late,
            "last_run_at": last["started_at"] if last else None, "last_ok_at": ok["started_at"] if ok else None,
            "items": ok["items"] if ok else None, "new_today": new_today,
            "gone_last": ok["gone_items"] if ok else None,
            "error": last["error"] if last and state in ("error", "rate_limited") else None,
            "stored": _stored(conn, s.id),
        })
    marks = ",".join("?" for _ in LIST_SOURCES)
    new = []
    for r in conn.execute(f"SELECT * FROM feed_items WHERE baseline=0 AND first_seen>=? AND source IN ({marks}) "
                          "ORDER BY first_seen DESC LIMIT ?", (since_new, *LIST_SOURCES, new_limit)):
        info = json.loads(r["info_json"] or "{}")
        new.append({"source": r["source"], "label": next(s.label_ja for s in SOURCES if s.id == r["source"]),
                    "key": r["key"], "name": r["name"], "chain": r["chain"], "first_seen": r["first_seen"],
                    "info": info})
    articles = []
    for r in conn.execute("SELECT * FROM feed_items WHERE source='aero_articles' ORDER BY first_seen DESC, rowid ASC"):
        info = json.loads(r["info_json"] or "{}")
        articles.append({"slug": r["key"], "title": r["name"], "date": info.get("date"), "url": info.get("url"),
                         "new": not r["baseline"], "first_seen": r["first_seen"]})
    # N5a: Aero の公式の住所のファイル（公開のコード置き場）。保存を始めた最初の回のあとに出た・変わったものに印を付ける
    addresses = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aero_address_files'").fetchone():
        base = {r[0]: r[1] for r in conn.execute("SELECT key, baseline FROM feed_items WHERE source='aero_addresses'")}
        for r in conn.execute("SELECT name, first_seen, changed_at, url FROM aero_address_files ORDER BY first_seen, name"):
            addresses.append({"name": r["name"], "first_seen": r["first_seen"], "changed_at": r["changed_at"],
                              "url": r["url"], "new": base.get(r["name"]) == 0 or r["changed_at"] is not None})
    limited = conn.execute("SELECT COUNT(*) FROM rate_limits WHERE ts>=?",
                           (store.iso(now - timedelta(hours=24)),)).fetchone()[0]
    disk = store.dir_bytes(settings.raw_dir) + sum(
        p.stat().st_size for p in settings.database_path.parent.glob(settings.database_path.name + "*") if p.is_file())
    started = conn.execute("SELECT MIN(started_at) FROM feed_runs WHERE status='ok'").fetchone()[0]
    return {
        "enabled": True, "started_at": started, "problem": any_problem,
        "text": "一部の一覧が読めていません。次の回にもう一度読みます。" if any_problem else "決めた時刻に保存できています。",
        "sources": rows, "new": new, "rate_limited_24h": limited, "disk_bytes": disk,
        "aero": {"start": AERO_START, "articles": articles[:5], "addresses": addresses},
    }
