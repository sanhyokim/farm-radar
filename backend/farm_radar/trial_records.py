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


def feeds(feeds_db: Path, coins_keys: dict[int, str] | None = None) -> dict[str, Any]:
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
        r = _count(conn, "SELECT COUNT(DISTINCT market_id), COUNT(*), MIN(ts), MAX(ts) FROM lighter_price_history")
        lighter_prices = {"markets": r[0], "rows": r[1], "from": r[2], "to": r[3]} if r else None
        r = _count(conn, "SELECT COUNT(DISTINCT ts), COUNT(DISTINCT opp_key), COUNT(*), MIN(ts), MAX(ts) "
                         "FROM shadow_predictions")
        shadow = {"hours": r[0], "opportunities": r[1], "rows": r[2], "from": r[3], "to": r[4]} if r else None
        files = []
        try:
            files = [{"name": x[0], "first_seen": x[1], "changed_at": x[2]} for x in conn.execute(
                "SELECT name, first_seen, changed_at FROM aero_address_files ORDER BY first_seen")]
        except sqlite3.OperationalError:
            pass
        r = _count(conn, "SELECT COUNT(DISTINCT token_id), SUM(error IS NULL), SUM(error IS NOT NULL), MAX(ts) "
                         "FROM merkl_position_snaps")
        positions = {"positions": r[0], "ok": r[1] or 0, "errors": r[2] or 0, "last": r[3]} if r else None
        return {"merkl_rewards": rewards, "llama_history": llama, "lighter_history": lighter, "lighter_prices": lighter_prices,
                "shadow": shadow,
                "aero_addresses": files, "lighter_rh": lighter_rh(conn),
                "merkl_positions": positions, "merkl_check": _merkl_check(feeds_db, coins_keys or {})}
    finally:
        conn.close()


def _merkl_check(feeds_db: Path, coins_keys: dict[int, str]) -> dict[str, Any] | None:
    """N5c: Merkl の実際に配った額と、探すの見込み（全員を分母）の答え合わせ（merkl_check）。"""
    from . import merkl_check
    conn = sqlite3.connect(f"file:{feeds_db.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return merkl_check.check(conn, coins_keys)
    except Exception as exc:  # noqa: BLE001  答え合わせが落ちても、ほかの記録の数（/api/trial/records）は返す
        return {"campaigns": [], "errors": [{"campaign_id": None, "error": f"{type(exc).__name__}: {exc}"[:200]}],
                "ratio_median": None, "denominator_share_median": None, "pairs_in_range": 0}
    finally:
        conn.close()


def _funding_daily(conn: sqlite3.Connection, table: str) -> dict[str, float]:
    """銘柄ごとの、7日の売り（保険）の1日の支払い（割合。プラス = 払う）。opportunities.FeedData と同じ読み方
    （funding-rates の exchange=lighter の rate は8時間あたり。プラス = 買いが払う → 売りの1日 = −rate × 3）。"""
    rows = _rows(conn, f"SELECT symbol, AVG(rate_8h) FROM {table} WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') "
                       "GROUP BY symbol")
    return {str(r[0]).upper(): -float(r[1]) * 3 for r in rows if r[0] and r[1] is not None}


def _rows(conn: sqlite3.Connection, sql: str) -> list[Any]:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def lighter_rh(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Lighter の Robinhood Chain 版で読めたもの（2026-10-04 オーナー決定 ②A）と、本体との比べ（保険に使う市場だけ）。
    保険に使う市場 = 値段の過去を読んだ市場（feeds/trial.rh_targets: up. の保険の市場のうち、その版にあるもの）。"""
    r = _count(conn, "SELECT COUNT(*), MAX(updated_at) FROM lighter_rh_markets WHERE status='active'")
    if r is None:
        return None
    hist = {int(x[0]): x for x in _rows(conn, "SELECT market_id, symbol, COUNT(*), MIN(ts), MAX(ts) "
                                              "FROM lighter_rh_price_history GROUP BY market_id")}
    fh = {int(x[0]): x[1] for x in _rows(conn, "SELECT market_id, COUNT(*) FROM lighter_rh_funding_history GROUP BY market_id")}
    rh = {int(x[0]): x for x in _rows(conn, "SELECT market_id, symbol, mark_price, maintenance_margin_fraction "
                                            "FROM lighter_rh_markets WHERE status='active'")}
    main = {str(x[0]).upper(): x for x in _rows(conn, "SELECT symbol, mark_price, maintenance_margin_fraction, market_id "
                                                      "FROM lighter_markets WHERE status='active'")}
    f_rh, f_main = _funding_daily(conn, "lighter_rh_funding_snaps"), _funding_daily(conn, "lighter_funding_snaps")
    rows = []
    for mid in sorted(hist, key=lambda m: str(hist[m][1])):
        sym = str(hist[mid][1]).upper()
        a, b = rh.get(mid), main.get(sym)
        rows.append({"symbol": sym, "rh_market_id": mid, "main_market_id": b[3] if b else None,
                     "price_rh": a[2] if a else None, "price_main": b[1] if b else None,
                     "mmf_rh_pct": a[3] / 100 if a and a[3] is not None else None,
                     "mmf_main_pct": b[2] / 100 if b and b[2] is not None else None,
                     "funding_daily_rh": f_rh.get(sym), "funding_daily_main": f_main.get(sym),
                     "price_points": hist[mid][2], "funding_points": fh.get(mid, 0)})
    return {"markets": r[0], "updated_at": r[1], "hedge_markets": len(rows), "rows": rows}


def summary(data_dir: Path, feeds_db: Path, coins_keys: dict[int, str] | None = None) -> dict[str, Any]:
    return {"old_copy": old_copy(data_dir), "feeds": feeds(feeds_db, coins_keys)}
