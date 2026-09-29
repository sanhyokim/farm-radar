"""会場が2つ以上あるときの順番と、観察だけの会場を休む条件（SPEC 5.1章。2026-09-30 オーナー条件。M6）。

- 練習と評価に使う会場（up.）を先に集める。観察だけの会場（会場ファイルの practice: false。Alandale）は後。
- 観察だけの会場は、次のどれかに当たる回は読まずに「後回し（deferred）」と記録し、次の回にまわす:
  1. 同じ回の、練習・評価の会場の収集が成功していない
  2. 直近 rate_limit_quiet_minutes 分に 429（取りすぎ）が返った
  3. 練習・評価の会場の収集率（評価中は評価を始めてから、それ以外は直近24時間）が defer_below_coverage_pct % 未満
  4. 予定時刻から max_start_delay_minutes 分を過ぎている（前の会場の収集が長引いた）
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime

from .. import ratelimit
from ..config import Config, practice_allowed
from ..db import database as db
from .completeness import check
from .snapshot import RunResult, _record_gap_if_any

log = logging.getLogger(__name__)

DEFERRED = "deferred"


def split_venues(runtimes: Iterable) -> tuple[list, list]:
    """(練習・評価に使う会場, 観察だけの会場)。それぞれ config の順番のまま。"""
    main, observe = [], []
    for v in runtimes:
        (main if practice_allowed(v.venue) else observe).append(v)
    return main, observe


def main_coverage(conn: sqlite3.Connection, config: Config, venue_ids: list[str], now: datetime
                  ) -> tuple[float | None, str]:
    """練習・評価の会場の収集率（0〜1。いちばん低い会場）と、数えた期間の説明。"""
    hours, label = 24, "直近24時間"
    ev = conn.execute("SELECT * FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()
    if ev is not None and ev["status"] == "running" and now < datetime.fromisoformat(ev["ends_at"]):
        h = math.floor((now - datetime.fromisoformat(ev["started_at"])).total_seconds() / 3600)
        if h >= 1:
            hours, label = h, "評価を始めてから"
    ratios = []
    for vid in venue_ids:
        r = check(conn, vid, hours=hours, minutes=config.snapshot_minutes, now=now)
        if r.expected:
            ratios.append(r.ok / r.expected)
    return (min(ratios) if ratios else None), label


def defer_reason(conn: sqlite3.Connection, config: Config, main_results: Mapping[str, RunResult],
                 slot: datetime, now: datetime) -> str | None:
    """観察だけの会場をこの回は休むべきなら、その理由（日本語）。読んでよければ None。"""
    o = config.observe_venues
    bad = [vid for vid, r in main_results.items() if r.status != "ok"]
    if bad:
        return f"同じ回の {', '.join(bad)} の収集が成功していないため"
    hits = ratelimit.recent(conn, now, o.rate_limit_quiet_minutes)
    if hits:
        return f"直近{o.rate_limit_quiet_minutes:g}分に回数制限（429）が {len(hits)} 回あったため（{hits[-1]['host']}）"
    cov, label = main_coverage(conn, config, list(main_results), now)
    if cov is not None and cov * 100 < o.defer_below_coverage_pct:
        return (f"練習・評価の会場の収集率（{label}）が {cov * 100:.1f}% で、"
                f"{o.defer_below_coverage_pct:g}% を下回っているため")
    late = (now - slot).total_seconds() / 60
    if late > o.max_start_delay_minutes:
        return f"予定時刻から {late:.0f} 分過ぎたため（前の会場の収集が長引いた）"
    return None


def record_deferred(conn: sqlite3.Connection, venue_id: str, slot: datetime, now: datetime, reason: str,
                    snapshot_minutes: int = 15) -> None:
    """休んだ回の記録（「欠損」とは分けて数える）。"""
    _record_gap_if_any(conn, venue_id, slot, snapshot_minutes, now)   # 止まっていた期間は「欠損」として別に残す
    run_id = db.start_run(conn, venue_id, slot, now)
    db.finish_run(conn, run_id, now=now, status=DEFERRED, block_number=None, pools_ok=0, pools_failed=0,
                  error=reason)
    conn.commit()
    log.info("observe venue deferred", extra={"data": {"venue": venue_id, "reason": reason}})
