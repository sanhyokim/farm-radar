"""確認用のAPI（M3で画面をつなぐ）。`uvicorn farm_radar.api:app` で起動する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from .collectors.completeness import check
from .config import load_config, load_venue
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


@app.get("/api/venues")
def venues() -> dict:
    """会場ごとの確認状況と警告（C4 など）。M3 の会場画面はこれを表示する。"""
    config = load_config()
    out = []
    for venue_id in config.venues:
        v = load_venue(venue_id, config.root)
        contracts = {
            name: {
                "address": c.get("address"),
                "unverified": c.get("unverified", True),
                "sourcify_match": c.get("sourcify_match"),
                "checked_at": c.get("checked_at"),
            }
            for name, c in (v.get("contracts") or {}).items()
        }
        mechanics = {
            name: {k: m.get(k) for k in ("value", "unverified", "owner_acknowledged", "evidence_function")}
            for name, m in (v.get("mechanics") or {}).items()
        }
        out.append({
            "venue_id": venue_id, "name": v.get("name"), "audited": v.get("audited"),
            "launch_date": v.get("launch_date"), "warnings": v.get("warnings") or [],
            "contracts": contracts, "mechanics": mechanics,
        })
    return {"venues": out}


@app.get("/api/alerts")
def alerts(days: int = 7) -> dict:
    """記録された通知（報酬の毎秒量の急減など）。M4 で Discord / Telegram にも送る。"""
    config = load_config()
    conn = db.connect(config.database_path)
    try:
        rows = db.list_alerts(conn, datetime.now(UTC) - timedelta(days=days))
        return {"alerts": [dict(r) for r in rows]}
    finally:
        conn.close()
