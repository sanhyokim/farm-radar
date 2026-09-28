"""24時間の欠けチェック。`python -m farm_radar.check` で実行する。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .collectors.completeness import check
from .config import load_config
from .db import database as db

JST = timezone(timedelta(hours=9))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="スナップショットが欠けずに保存されているか確認します。")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args(argv)

    config = load_config()
    conn = db.connect(config.database_path)
    all_passed = True
    for venue_id in config.venues:
        r = check(conn, venue_id, hours=args.hours, minutes=config.snapshot_minutes)
        fmt = lambda d: d.astimezone(JST).strftime("%m/%d %H:%M")
        print(f"■ {venue_id}: {fmt(r.since)} 〜 {fmt(r.until)}（日本時間）")
        print(f"  予定 {r.expected} 回 / 成功 {r.ok} 回 / 記録なし {len(r.missing)} 回 / 失敗・一部失敗・スキップ {len(r.not_ok)} 回")
        for s in r.missing[:20]:
            print(f"  - 記録なし: {fmt(s)}")
        for s, status in r.not_ok[:20]:
            print(f"  - {status}: {fmt(s)}")
        gaps = db.list_gaps(conn, venue_id, r.since)
        for g in gaps:
            print(f"  - 欠損（収集が止まっていた）: {fmt(datetime.fromisoformat(g['start_slot']))} 〜 "
                  f"{fmt(datetime.fromisoformat(g['end_slot']))}（{g['missed_slots']} 回分）")
        snaps = conn.execute(
            "SELECT COUNT(*) FROM pool_snapshots s JOIN pools p ON p.id = s.pool_id "
            "WHERE p.venue_id = ? AND s.ts >= ?", (venue_id, r.since.isoformat(timespec="seconds"))).fetchone()[0]
        print(f"  保存されたスナップショット: {snaps} 件")
        print("  結果: " + ("合格（欠けなし）" if r.passed else "不合格"))
        all_passed &= r.passed
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
