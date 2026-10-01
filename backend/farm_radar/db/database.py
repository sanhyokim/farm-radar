"""SQLite への接続と、M1で使う書き込み・読み取り。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 13
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


# 古いデータベースに足りない列（CREATE TABLE IF NOT EXISTS では既存の表に列が増えないため）
_ADDED_COLUMNS = {
    "pools": {"token0_symbol": "TEXT", "token1_symbol": "TEXT", "token0_decimals": "INTEGER",
              "token1_decimals": "INTEGER"},
    "pool_snapshots": {"block_time": "TEXT", "epoch_start": "TEXT", "period_finish": "TEXT",
                       "reward_rate_effective_raw": "TEXT", "gauge_alive": "INTEGER",
                       "unstaked_fee": "INTEGER", "epoch_just_flipped": "INTEGER",
                       # M6: 今のエポックのボーナスの合計と、そのうち運営が手で足した分（最小単位。Alandale）
                       "reward_epoch_total_raw": "TEXT", "reward_manual_raw": "TEXT",
                       # 2026-10-01: プールが持っているコインの量（緊急離脱の「プールのお金」）
                       "balance0_raw": "TEXT", "balance1_raw": "TEXT"},
    "scores": {"venue_id": "TEXT", "block_number": "INTEGER", "direction_risk": "REAL",
               "net_daily_pct_lp": "REAL", "mode": "TEXT", "in_range_ratio": "REAL",
               "in_range_ratio_hold": "REAL", "sigma_pair": "REAL", "sigma_token0": "REAL",
               "sigma_token1": "REAL", "vol_source": "TEXT", "has_perp": "INTEGER",
               "epoch_just_flipped": "INTEGER", "tvl_usd": "REAL", "details_json": "TEXT"},
    "learning_notes": {"topic": "TEXT"},
    # M5a: 練習の建玉。state_json は計算の途中の値（累計・報酬トークンの受け取り記録）
    "positions": {"venue_id": "TEXT", "mode": "TEXT", "liquidity": "TEXT", "amount0": "REAL", "amount1": "REAL",
                  "price_open": "REAL", "usd0_open": "REAL", "usd1_open": "REAL", "c_lp": "REAL",
                  "hedges_json": "TEXT", "started_red": "INTEGER", "signal_open": "TEXT",
                  "predicted_json": "TEXT", "last_ts": "TEXT", "state_json": "TEXT",
                  # 2026-10-01: 参考の練習（'reference'）。空なら普通の練習（評価の合否に使う）
                  "purpose": "TEXT"},
    "position_pnl": {"haircut": "REAL", "haircut_sell": "REAL", "in_range": "REAL", "reward_amount": "REAL",
                     "value_usd": "REAL", "detail_json": "TEXT"},
    "ledger": {"value_usd": "REAL", "fx_rate": "REAL", "fx_date": "TEXT"},
    # M5b: 停止の理由
    "paper_state": {"reason": "TEXT"},
    # 2026-10-01: 評価が「中断」になったときの、最後に閉じた建玉と理由（JSON）
    "evaluations": {"note": "TEXT"},
}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
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
             is_stock_pair, has_perp, gauge_address, created_block, discovered_at,
             token0_symbol, token1_symbol, token0_decimals, token1_decimals)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             gauge_address=COALESCE(excluded.gauge_address, pools.gauge_address),
             token0_symbol=COALESCE(excluded.token0_symbol, pools.token0_symbol),
             token1_symbol=COALESCE(excluded.token1_symbol, pools.token1_symbol),
             token0_decimals=COALESCE(excluded.token0_decimals, pools.token0_decimals),
             token1_decimals=COALESCE(excluded.token1_decimals, pools.token1_decimals)""",
        (
            pool.pool_id, pool.venue_id, pool.address, pool.token0, pool.token1, pool.fee_tier,
            pool.tick_spacing, pool.is_stock_pair, pool.has_perp, pool.gauge_address,
            pool.created_block, _iso(now),
            getattr(pool, "token0_symbol", None), getattr(pool, "token1_symbol", None),
            getattr(pool, "token0_decimals", None), getattr(pool, "token1_decimals", None),
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


def last_slot_before(conn: sqlite3.Connection, venue_id: str, slot: datetime) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(slot) FROM collection_runs WHERE venue_id=? AND slot<?", (venue_id, _iso(slot))
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def record_gap(conn: sqlite3.Connection, venue_id: str, start: datetime, end: datetime,
               missed: int, now: datetime) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO collection_gaps(venue_id, start_slot, end_slot, missed_slots, detected_at)
           VALUES (?,?,?,?,?)""",
        (venue_id, _iso(start), _iso(end), missed, _iso(now)),
    )
    conn.commit()


def list_gaps(conn: sqlite3.Connection, venue_id: str, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM collection_gaps WHERE venue_id=? AND end_slot>=? ORDER BY start_slot",
        (venue_id, _iso(since)),
    ).fetchall()


def in_gap(conn: sqlite3.Connection, venue_id: str, ts: datetime) -> bool:
    """その時刻が欠損期間に入っているか（M5でペーパートレードの損益を推定扱いにするのに使う）。"""
    row = conn.execute(
        "SELECT 1 FROM collection_gaps WHERE venue_id=? AND start_slot<=? AND end_slot>=? LIMIT 1",
        (venue_id, _iso(ts), _iso(ts)),
    ).fetchone()
    return row is not None


def list_runs(conn: sqlite3.Connection, venue_id: str, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM collection_runs WHERE venue_id=? AND slot>=? ORDER BY slot",
        (venue_id, _iso(since)),
    ).fetchall()


def insert_alert(conn: sqlite3.Connection, *, ts: datetime, venue_id: str, pool_id: str | None, kind: str,
                 level: str, message_ja: str, data: Any, dedupe_key: str) -> bool:
    """通知すべき出来事を記録する。同じ dedupe_key がすでにあれば何もしない（False を返す）。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO alerts(ts, venue_id, pool_id, kind, level, message_ja, data_json, dedupe_key)
           VALUES (?,?,?,?,?,?,?,?)""",
        (_iso(ts), venue_id, pool_id, kind, level, message_ja, json.dumps(data, default=str), dedupe_key),
    )
    return cur.rowcount > 0


def list_alerts(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE ts>=? ORDER BY ts DESC", (_iso(since),)).fetchall()


def insert_token_prices(conn: sqlite3.Connection, rows: list[tuple[str, str, float, str, int | None]]) -> None:
    """(トークン, 時刻, ドル価格, 出どころ, ブロック) を保存する。同じものがあれば何もしない。"""
    conn.executemany(
        "INSERT OR IGNORE INTO token_prices(token, ts, price_usd, source, block_number) VALUES (?,?,?,?,?)", rows
    )


def token_price_series(conn: sqlite3.Connection, token: str, source: str, since: datetime) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, price_usd FROM token_prices WHERE token=? AND source=? AND ts>=? ORDER BY ts",
        (token.lower(), source, _iso(since)),
    ).fetchall()
    return [(int(datetime.fromisoformat(r[0]).timestamp()), float(r[1])) for r in rows if r[1]]


def insert_score(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO scores({cols}) VALUES ({marks})", tuple(row.values()))


def latest_scores(conn: sqlite3.Connection, venue_id: str | None = None) -> list[sqlite3.Row]:
    """プールごとの最新のスコア。"""
    q = """SELECT s.*, p.token0_symbol, p.token1_symbol, p.address, v.name AS venue_name FROM scores s
           JOIN pools p ON p.id = s.pool_id LEFT JOIN venues v ON v.id = p.venue_id
           WHERE s.ts = (SELECT MAX(ts) FROM scores s2 WHERE s2.pool_id = s.pool_id)"""
    args: tuple = ()
    if venue_id:
        q += " AND s.venue_id=?"
        args = (venue_id,)
    return conn.execute(q + " ORDER BY s.net_daily_pct DESC", args).fetchall()
