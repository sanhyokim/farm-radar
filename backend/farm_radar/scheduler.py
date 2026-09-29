"""常時動かす収集プロセス。`python -m farm_radar.scheduler` で起動する。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import runtime
from .collectors.snapshot import collect_venue, record_failed_run
from .db import database as db
from .external.geckoterminal import GeckoTerminal
from .external.lighter import Lighter
from .logging_setup import setup_logging
from .scoring.run import ScoreContext, score_venue
from .tokens import load_tokens

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    config, venues = runtime.build()
    log.info("scheduler starting", extra={"data": {"mode": config.mode, "snapshot_minutes": config.snapshot_minutes}})

    def snapshot_job() -> None:
        conn = db.connect(config.database_path)
        try:
            for v in venues:
                try:
                    v.ensure_chain()
                except Exception as exc:
                    # 失敗の記録を残して、欠けチェックに「failed」として出るようにする
                    record_failed_run(conn, v.adapter.venue_id, config.snapshot_minutes, f"チェーンの確認に失敗: {exc}")
                    continue
                collect_venue(conn, v.adapter, v.rpc, snapshot_minutes=config.snapshot_minutes,
                              epoch_fresh_minutes=config.epoch_fresh_minutes,
                              reward_drop_alert_pct=config.reward_drop_alert_pct)
        finally:
            conn.close()

    lighter = Lighter()
    contexts = [
        ScoreContext(
            venue=v.venue, tokens=load_tokens(v.venue["chain"]["id"], config.root), settings=config.scoring,
            stale_after_minutes=config.stale_after_minutes, rpc=v.rpc, lighter=lighter,
            gt=GeckoTerminal(v.venue["chain"]["geckoterminal_network"])
            if v.venue["chain"].get("geckoterminal_network") else None,
        )
        for v in venues
    ]

    def score_job() -> None:
        """スコア計算と判定（SPEC 5.1章: 1時間ごと）。外部サイトが落ちていても収集は止めない。"""
        conn = db.connect(config.database_path)
        try:
            for ctx in contexts:
                try:
                    score_venue(conn, ctx)
                except Exception:
                    log.exception("scoring failed", extra={"data": {"venue": ctx.venue["id"]}})
        finally:
            conn.close()

    minutes = config.snapshot_minutes
    # 60を割り切れる間隔なら毎時0分・15分…に揃える（欠けチェックの枠と一致させるため）
    trigger = (CronTrigger(minute=f"*/{minutes}", timezone="UTC") if 60 % minutes == 0
               else IntervalTrigger(minutes=minutes))
    sched = BlockingScheduler(timezone="UTC")
    # スリープ復帰時: 寝ている間の予定は1回にまとめて（coalesce）、起きた直後にすぐ実行する
    # （misfire_grace_time=None は「どれだけ遅れても実行する」）。止まっていた期間は欠損として記録される。
    sched.add_job(snapshot_job, trigger, id="snapshot", max_instances=1, coalesce=True, misfire_grace_time=None)
    # スコアは収集の少し後（毎時5分など）に計算する。収集と同じ時刻だと、その回の記録がまだ途中のため
    sm = config.scoring.every_minutes
    if sm == 60:
        score_trigger = CronTrigger(minute="5", timezone="UTC")
    elif 60 % sm == 0:
        score_trigger = CronTrigger(minute=f"{5 % sm}-59/{sm}", timezone="UTC")
    else:
        score_trigger = IntervalTrigger(minutes=sm)
    # 起動直後のスコア計算も、ここで直接呼ばずにスケジューラーに任せる。
    # 最初の計算は外部サイトから7日分の足を取り寄せるので数分かかり、直接呼ぶと15分ごとの収集が待たされるため
    sched.add_job(score_job, score_trigger, id="score", max_instances=1, coalesce=True, misfire_grace_time=None,
                  next_run_time=datetime.now(UTC) + timedelta(seconds=30))
    snapshot_job()  # 起動直後にも1回実行して動作を確認する
    sched.start()


if __name__ == "__main__":
    main()
