"""確認用のAPI（M3で画面をつなぐ）。`uvicorn farm_radar.api:app` で起動する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from .collectors.completeness import check
from .config import load_config
from .db import database as db

app = FastAPI(title="Farm Radar")


@app.get("/api/health")
def health() -> dict:
    config = load_config()
    conn = db.connect(config.database_path)
    try:
        venues = []
        for venue_id in config.venues:
            last = conn.execute(
                "SELECT * FROM collection_runs WHERE venue_id=? ORDER BY id DESC LIMIT 1", (venue_id,)
            ).fetchone()
            last_ok = conn.execute(
                "SELECT finished_at FROM collection_runs WHERE venue_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                (venue_id,),
            ).fetchone()
            stale = True
            if last_ok and last_ok["finished_at"]:
                age = datetime.now(UTC) - datetime.fromisoformat(last_ok["finished_at"])
                stale = age > timedelta(minutes=config.stale_after_minutes)
            r = check(conn, venue_id, minutes=config.snapshot_minutes)
            gaps = [dict(g) for g in db.list_gaps(conn, venue_id, datetime.now(UTC) - timedelta(days=7))]
            venues.append({
                "venue_id": venue_id,
                "last_run": dict(last) if last else None,
                "last_ok_at": last_ok["finished_at"] if last_ok else None,
                "stale": stale,
                "last_24h": {"expected": r.expected, "ok": r.ok, "missing": len(r.missing), "not_ok": len(r.not_ok)},
                "gaps_7d": gaps,   # 収集が止まっていた期間（M3の画面で「欠損」として表示する）
            })
        return {"mode": config.mode, "venues": venues}
    finally:
        conn.close()
