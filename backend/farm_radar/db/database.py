"""SQLite への接続と、M1で使う書き込み・読み取り。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 収集とAPIが同時に読み書きできるように
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def upsert_venue(conn: sqlite3.Connection, venue: dict[str, Any]) -> None:
    chain = venue.get("chain") or {}
    conn.execute(
        """INSERT INTO venues(id, name, chain, type, audited, launch_date, verified, source_url, checked_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, chain=excluded.chain, type=excluded.type,
             audited=excluded.audited, launch_date=excluded.launch_date, verified=excluded.verified,
             source_url=excluded.source_url, checked_at=excluded.checked_at""",
        (
            venue["id"], venue.get("name", venue["id"]), chain.get("id", ""), venue.get("type"),
            venue.get("audited"), venue.get("launch_date"), int(venue_is_verified(venue)),
            chain.get("source_url"), chain.get("checked_at"),
        ),
    )


def venue_is_verified(venue: dict[str, Any]) -> bool:
    """チェーン情報・全コントラクト・全仕組みが確認済みなら True。"""
    items = [venue.get("chain") or {}]
    items += list((venue.get("contracts") or {}).values())
    items += list((venue.get("mechanics") or {}).values())
    return all(not (i or {}).get("unverified", True) for i in items)


def upsert_pool(conn: sqlite3.Connection, pool: Any, now: datetime) -> None:
    conn.execute(
        """INSERT INTO pools(id, venue_id, address, token0, token1, fee_tier, tick_spacing,
             is_stock_pair, has_perp, gauge_address, created_block, discovered_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET gauge_address=COALESCE(excluded.gauge_address, pools.gauge_address)""",
        (
            pool.pool_id, pool.venue_id, pool.address, pool.token0, pool.token1, pool.fee_tier,
            pool.tick_spacing, pool.is_stock_pair, pool.has_perp, pool.gauge_address,
            pool.created_block, _iso(now),
        ),
    )


def start_run(conn: sqlite3.Connection, venue_id: str, slot: datetime, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO collection_runs(venue_id, slot, started_at, status) VALUES (?,?,?, 'running')",
        (venue_id, _iso(slot), _iso(now)),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, now: datetime, status: str, block_number: int | None,
    pools_ok: int, pools_failed: int, error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE collection_runs SET finished_at=?, status=?, block_number=?, pools_ok=?,
             pools_failed=?, error=? WHERE id=?""",
        (_iso(now), status, block_number, pools_ok, pools_failed, error, run_id),
    )
    conn.commit()


def insert_raw(
    conn: sqlite3.Connection, *, run_id: int, ts: datetime, block_number: int | None,
    pool_id: str | None, kind: str, request: Any, response: Any,
) -> None:
    conn.execute(
        """INSERT INTO raw_rpc(run_id, ts, block_number, pool_id, kind, request_json, response_json)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, _iso(ts), block_number, pool_id, kind,
         json.dumps(request, default=str), json.dumps(response, default=str)),
    )


def insert_snapshot(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO pool_snapshots({cols}) VALUES ({marks})", tuple(row.values()))


def list_runs(conn: sqlite3.Connection, venue_id: str, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM collection_runs WHERE venue_id=? AND slot>=? ORDER BY slot",
        (venue_id, _iso(since)),
    ).fetchall()
