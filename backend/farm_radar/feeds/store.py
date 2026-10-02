"""一覧の保存先（data/feeds.sqlite3 と data/feeds/ の圧縮ファイル）。N2a。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .sources import Item

JST = ZoneInfo("Asia/Tokyo")
SCHEMA_VERSION = 1
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 保存とAPIが同時に読み書きできるように
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM feeds_schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO feeds_schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def save_raw(raw_dir: Path, source_id: str, now: datetime, body: bytes, ext: str) -> str:
    """元の応答を圧縮して残す。data/feeds/<一覧>/<日付 UTC>/<時刻>Z.<ext>.gz。戻り値は raw_dir からの場所。"""
    t = now.astimezone(UTC)
    rel = Path(source_id) / t.strftime("%Y-%m-%d") / f"{t.strftime('%H%M%S')}Z.{ext}.gz"
    path = raw_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb", compresslevel=6) as f:
        f.write(body)
    return rel.as_posix()


def last_raw_at(conn: sqlite3.Connection, source_id: str) -> datetime | None:
    row = conn.execute("SELECT MAX(started_at) FROM feed_runs WHERE source=? AND raw_path IS NOT NULL",
                       (source_id,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def start_run(conn: sqlite3.Connection, source_id: str, now: datetime) -> int:
    cur = conn.execute("INSERT INTO feed_runs(source, started_at, status) VALUES (?,?, 'running')",
                       (source_id, iso(now)))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, now: datetime, status: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE feed_runs SET finished_at=?, status=?{', ' + sets if sets else ''} WHERE id=?",
                 (iso(now), status, *cols.values(), run_id))
    conn.commit()


def last_ok(conn: sqlite3.Connection, source_id: str, before_id: int | None = None) -> sqlite3.Row | None:
    q = "SELECT * FROM feed_runs WHERE source=? AND status='ok'"
    args: tuple = (source_id,)
    if before_id is not None:
        q += " AND id<?"
        args += (before_id,)
    return conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()


def upsert_items(conn: sqlite3.Connection, source_id: str, items: Iterable[Item], seen_at: str,
                 baseline: bool) -> int:
    """一覧のものを書く。初めて見たものの数を返す（最初の回は baseline=1 にして「新しい」に数えない）。"""
    before = conn.execute("SELECT COUNT(*) FROM feed_items WHERE source=?", (source_id,)).fetchone()[0]
    rows = {}
    for it in items:
        rows[it.key] = (source_id, it.key, seen_at, seen_at, int(baseline), it.name, it.chain,
                        json.dumps(it.info, ensure_ascii=False, default=str))
    conn.executemany(
        """INSERT INTO feed_items(source, key, first_seen, last_seen, baseline, name, chain, info_json)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source, key) DO UPDATE SET last_seen=excluded.last_seen, name=excluded.name,
             chain=excluded.chain, info_json=excluded.info_json""", list(rows.values()))
    after = conn.execute("SELECT COUNT(*) FROM feed_items WHERE source=?", (source_id,)).fetchone()[0]
    return after - before


def gone_count(conn: sqlite3.Connection, source_id: str, prev_seen_at: str | None) -> int:
    """前の回にあって、この回になかったもの（終わった機会など）の数。"""
    if not prev_seen_at:
        return 0
    return conn.execute("SELECT COUNT(*) FROM feed_items WHERE source=? AND last_seen=?",
                        (source_id, prev_seen_at)).fetchone()[0]


def write_merkl(conn: sqlite3.Connection, ts: str, opportunities: list[dict[str, Any]]) -> int:
    """Merkl の機会の数字（15分ごと）とキャンペーン（最新の値で上書き）を書く。キャンペーンの数を返す。"""
    from .sources import merkl_campaigns, merkl_opportunity_snap
    snaps, camps = [], {}
    for o in opportunities:
        if not isinstance(o, dict) or not o.get("id"):
            continue
        snaps.append((ts, *merkl_opportunity_snap(o)))
        for c in merkl_campaigns(o):
            camps[c["campaign_id"]] = c
    conn.executemany("INSERT OR REPLACE INTO merkl_opportunity_snaps(ts, opportunity_id, status, apr, max_apr, "
                     "native_apr, tvl, daily_rewards, live_campaigns) VALUES (?,?,?,?,?,?,?,?,?)", snaps)
    if camps:
        cols = list(next(iter(camps.values())))
        marks = ",".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "campaign_id")
        conn.executemany(
            f"INSERT INTO merkl_campaigns({', '.join(cols)}, first_seen, last_seen) VALUES ({marks}, ?, ?) "
            f"ON CONFLICT(campaign_id) DO UPDATE SET {updates}, last_seen=excluded.last_seen",
            [(*(c[k] for k in cols), ts, ts) for c in camps.values()])
    return len(camps)


def write_yields(conn: sqlite3.Connection, now: datetime, items: list[Item]) -> int:
    """DefiLlama の利回りのうち、ボーナスのあるものを日ごとに残す。"""
    day = now.astimezone(JST).strftime("%Y-%m-%d")
    rows = [(day, it.key, it.chain, it.info.get("project"), it.name, it.info.get("tvl"), it.info.get("apy_base"),
             it.info.get("apy_reward"), it.info.get("apy"))
            for it in items if (it.info.get("apy_reward") or 0) > 0]
    conn.executemany("INSERT OR REPLACE INTO yield_snaps(day, pool, chain, project, symbol, tvl_usd, apy_base, "
                     "apy_reward, apy) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
