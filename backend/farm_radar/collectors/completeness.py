"""M1の完了条件の確認:「全プールのスナップショットが15分ごとに24時間欠けずに保存される」。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..db import database as db
from .snapshot import slot_for


@dataclass
class CompletenessReport:
    venue_id: str
    since: datetime
    until: datetime
    expected: int
    ok: int
    missing: list[datetime] = field(default_factory=list)      # 収集の記録がない時刻
    not_ok: list[tuple[datetime, str]] = field(default_factory=list)  # 記録はあるが ok でない時刻

    @property
    def passed(self) -> bool:
        return self.expected > 0 and self.ok == self.expected


def check(conn: sqlite3.Connection, venue_id: str, *, hours: int = 24, minutes: int = 15,
          now: datetime | None = None) -> CompletenessReport:
    now = now or datetime.now(UTC)
    until = slot_for(now, minutes)          # 今の枠はまだ途中かもしれないので含めない
    since = until - timedelta(hours=hours)
    expected_slots = [since + timedelta(minutes=minutes * i) for i in range(hours * 60 // minutes)]

    best: dict[str, str] = {}
    for row in db.list_runs(conn, venue_id, since):
        if row["slot"] >= until.isoformat(timespec="seconds"):
            continue
        # 同じ枠に複数の記録があれば、ok を優先する
        if best.get(row["slot"]) != "ok":
            best[row["slot"]] = row["status"]

    report = CompletenessReport(venue_id, since, until, len(expected_slots), 0)
    for s in expected_slots:
        status = best.get(s.isoformat(timespec="seconds"))
        if status is None:
            report.missing.append(s)
        elif status == "ok":
            report.ok += 1
        else:
            report.not_ok.append((s, status))
    return report
