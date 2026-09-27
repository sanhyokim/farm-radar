"""常時動かす収集プロセス。`python -m farm_radar.scheduler` で起動する。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import runtime
from .collectors.snapshot import collect_venue
from .db import database as db
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    config, venues = runtime.build()
    log.info("scheduler starting", extra={"data": {"mode": config.mode, "snapshot_minutes": config.snapshot_minutes}})

    def snapshot_job() -> None:
        conn = db.connect(config.database_path)
        try:
            for v in venues:
                collect_venue(conn, v.adapter, v.rpc, snapshot_minutes=config.snapshot_minutes)
        finally:
            conn.close()

    minutes = config.snapshot_minutes
    # 60を割り切れる間隔なら毎時0分・15分…に揃える（欠けチェックの枠と一致させるため）
    trigger = (CronTrigger(minute=f"*/{minutes}", timezone="UTC") if 60 % minutes == 0
               else IntervalTrigger(minutes=minutes))
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(snapshot_job, trigger, id="snapshot", max_instances=1, coalesce=True, misfire_grace_time=120)
    snapshot_job()  # 起動直後にも1回実行して動作を確認する
    sched.start()


if __name__ == "__main__":
    main()
