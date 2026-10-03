"""N5「試す」に使える記録の一覧（N5a。/api/trial/records と、パソコンの1行の更新のまとめ）。

- 今の版（18000）のデータの写し: data/import/old-18000.sqlite3 と、写したときの確かめの記録 old-18000.json
  （scripts/pc/copy-18000.ps1 が作る。今の版のデータベースそのものは開かない。写しも読むだけで開く）
- 新しい版が集めている記録: Merkl の配った額・DefiLlama の毎日の記録・Lighter の資金調達率の過去・影の記録・Aero の住所
読むだけ。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

OLD_COPY = Path("import") / "old-18000.sqlite3"
OLD_MANIFEST = Path("import") / "old-18000.json"
# 試すのに使う今の版の表（N5b のさかのぼりの計算: 値段と流動性・ボーナスの速さ・練習の記録）
OLD_TABLES = ("pool_snapshots", "scores", "positions", "position_pnl", "collection_runs", "token_prices", "funding_rates")


def open_old_copy(data_dir: Path) -> sqlite3.Connection | None:
    """今の版のデータの写しを、読むだけで開く（immutable: 書かない・鍵をかけない。写しは誰も書き換えないので安全）。"""
    p = data_dir / OLD_COPY
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def old_copy(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / OLD_MANIFEST
    if not (data_dir / OLD_COPY).exists():
        return {"present": False, "text": "今の版のデータの写しはまだありません（パソコンの1行で作ります）。"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    conn = open_old_copy(data_dir)
    assert conn is not None
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {}
        for t in OLD_TABLES:
            if t not in names:
                continue
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
            tcol = next((c for c in ("ts", "started_at", "opened_at") if c in cols), None)
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            span = conn.execute(f'SELECT MIN("{tcol}"), MAX("{tcol}") FROM "{t}"').fetchone() if tcol else (None, None)
            tables[t] = {"rows": n, "from": span[0], "to": span[1]}
    finally:
        conn.close()
    ok = bool(manifest and manifest.get("integrity") == "ok" and manifest.get("health_match"))
    return {"present": True, "checked": ok, "copied_at": (manifest or {}).get("copied_at"),
            "bytes": (manifest or {}).get("bytes"), "tables": tables,
            "text": "今の版のデータの写しがあります（写したときに壊れていないことを確かめました）。" if ok else
            "今の版のデータの写しはありますが、写したときの確かめの記録がありません。使う前に写し直します。"}


def _count(conn: sqlite3.Connection, sql: str) -> Any:
    try:
        return conn.execute(sql).fetchone()
    except sqlite3.OperationalError:          # 表がまだない（古い保存のデータベース）
        return None


def feeds(feeds_db: Path) -> dict[str, Any]:
    if not feeds_db.exists():
        return {}
    conn = sqlite3.connect(f"file:{feeds_db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        r = _count(conn, "SELECT COUNT(DISTINCT campaign_id), COUNT(*), MIN(ts), MAX(ts) FROM merkl_reward_snaps")
        rewards = {"campaigns": r[0], "rows": r[1], "from": r[2], "to": r[3]} if r else None
        r = _count(conn, "SELECT COUNT(DISTINCT pool), COUNT(*), MIN(day), MAX(day) FROM llama_yield_history")
        llama = {"pools": r[0], "rows": r[1], "from": r[2], "to": r[3]} if r else None
        r = _count(conn, "SELECT COUNT(DISTINCT market_id), COUNT(*), MIN(ts), MAX(ts) FROM lighter_funding_history")
        lighter = {"markets": r[0], "rows": r[1], "from": r[2], "to": r[3]} if r else None
        r = _count(conn, "SELECT COUNT(DISTINCT ts), COUNT(DISTINCT opp_key), COUNT(*), MIN(ts), MAX(ts) "
                         "FROM shadow_predictions")
        shadow = {"hours": r[0], "opportunities": r[1], "rows": r[2], "from": r[3], "to": r[4]} if r else None
        files = []
        try:
            files = [{"name": x[0], "first_seen": x[1], "changed_at": x[2]} for x in conn.execute(
                "SELECT name, first_seen, changed_at FROM aero_address_files ORDER BY first_seen")]
        except sqlite3.OperationalError:
            pass
        return {"merkl_rewards": rewards, "llama_history": llama, "lighter_history": lighter, "shadow": shadow,
                "aero_addresses": files}
    finally:
        conn.close()


def summary(data_dir: Path, feeds_db: Path) -> dict[str, Any]:
    return {"old_copy": old_copy(data_dir), "feeds": feeds(feeds_db)}
