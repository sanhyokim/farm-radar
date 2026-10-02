"""一覧の保存先（data/feeds.sqlite3 と data/feeds/ の圧縮ファイル）。N2a。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .sources import Item

JST = ZoneInfo("Asia/Tokyo")
SCHEMA_VERSION = 1
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# N2b で古い表に足した列（2026-10-02）。パソコンで動いている保存のデータベースにも、起動したときに足す
_ADD_COLUMNS = {
    "merkl_opportunity_snaps": {"max_daily_rewards": "REAL"},
    "merkl_campaigns": {"distribution_method": "TEXT", "settings_json": "TEXT", "restricted": "INTEGER",
                        "hidden": "INTEGER", "reward_type": "TEXT", "reward_verified": "INTEGER"},
    "pool_state_snaps": {"hooks": "TEXT", "tick_spacing": "INTEGER"},   # N3（フックと tick の間隔）
}


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADD_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 保存とAPIが同時に読み書きできるように
    conn.executescript(_SCHEMA)
    _add_columns(conn)
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
                     "native_apr, tvl, daily_rewards, live_campaigns, max_daily_rewards) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     snaps)
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


def write_lighter_markets(conn: sqlite3.Connection, ts: str, items: list[Item]) -> int:
    rows = [(int(it.key), it.name, it.info.get("status"), it.info.get("taker_pct"), it.info.get("maker_pct"),
             it.info.get("imf"), it.info.get("mmf"), it.info.get("open_interest"), it.info.get("daily_quote_volume"),
             it.info.get("mark_price"), ts) for it in items if it.name]
    conn.executemany("INSERT OR REPLACE INTO lighter_markets(market_id, symbol, status, taker_pct, maker_pct, "
                     "initial_margin_fraction, maintenance_margin_fraction, open_interest, daily_quote_volume, "
                     "mark_price, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def write_lighter_funding(conn: sqlite3.Connection, ts: str, items: list[Item]) -> int:
    rows = [(ts, int(it.key), it.name, it.info.get("rate_8h")) for it in items if it.info.get("rate_8h") is not None]
    conn.executemany("INSERT OR REPLACE INTO lighter_funding_snaps(ts, market_id, symbol, rate_8h) VALUES (?,?,?,?)",
                     rows)
    return len(rows)


def write_token_prices(conn: sqlite3.Connection, ts: str, coins: dict[str, Any], keep_days: int = 30) -> int:
    """DefiLlama の chart の応答（{"<チェーン>:<住所>": {symbol, confidence, prices: [{timestamp, price}]}}）を書く。"""
    n = 0
    for coin, c in coins.items():
        if not isinstance(c, dict):
            continue
        pts = [(coin, int(p["timestamp"]), float(p["price"])) for p in c.get("prices") or []
               if isinstance(p, dict) and p.get("timestamp") and p.get("price")]
        conn.executemany("INSERT OR REPLACE INTO token_prices(coin, ts, price) VALUES (?,?,?)", pts)
        chain, _, addr = coin.partition(":")
        conn.execute("INSERT OR REPLACE INTO token_meta(coin, symbol, chain_id, address, confidence, updated_at) "
                     "VALUES (?,?,(SELECT chain_id FROM token_meta WHERE coin=?),?,?,?)",
                     (coin, c.get("symbol"), coin, addr.lower(), c.get("confidence"), ts))
        n += len(pts)
    cutoff = int(datetime.fromisoformat(ts).timestamp()) - keep_days * 86400
    conn.execute("DELETE FROM token_prices WHERE ts < ?", (cutoff,))
    return n


def write_receipts(conn: sqlite3.Connection, ts: str, rows: dict[str, Any]) -> int:
    """預かり証の中身（feeds/receipts.py の結果）を書く。読み取り口の失敗（failed）は前の結果を残す。"""
    n = 0
    for key, r in rows.items():
        if not isinstance(r, dict) or r.get("failed"):
            continue
        cid, _, addr = key.partition(":")
        conn.execute("""INSERT OR REPLACE INTO receipt_checks(chain_id, address, checked_at, is_vault, name, symbol, asset,
                        asset_symbol, assets_per_share, verified, verified_by, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (int(cid), addr.lower(), ts, int(bool(r.get("is_vault"))), r.get("name"), r.get("symbol"),
                      r.get("asset"), r.get("asset_symbol"), r.get("assets_per_share"), r.get("verified"),
                      r.get("verified_by"), r.get("error")))
        n += 1
    return n


def write_pool_states(conn: sqlite3.Connection, ts: str, rows: dict[str, Any], keep_days: int = 60) -> int:
    """幅に配るプールの状態（feeds/pools.py の結果）を書く。読み取り口の失敗（failed）は書かない。"""
    n = 0
    for key, r in rows.items():
        if not isinstance(r, dict) or r.get("failed"):
            continue
        cid, _, pid = key.partition(":")
        sp, liq = r.get("sqrt_price_x96"), r.get("liquidity")
        conn.execute("""INSERT OR REPLACE INTO pool_state_snaps(chain_id, pool_id, checked_at, kind, block, sqrt_price_x96,
                        tick, liquidity, lp_fee, price, official, error, hooks, tick_spacing)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (int(cid), pid.lower(), ts, r.get("kind") or "?", r.get("block"),
                      None if sp is None else str(sp), r.get("tick"), None if liq is None else str(liq),
                      r.get("lp_fee"), r.get("price"), r.get("official"), r.get("error"), r.get("hooks"),
                      r.get("tick_spacing")))
        n += 1
    cutoff = (datetime.fromisoformat(ts) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM pool_state_snaps WHERE checked_at < ?", (cutoff,))
    return n


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
