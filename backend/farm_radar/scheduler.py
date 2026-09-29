"""常時動かす収集プロセス。`python -m farm_radar.scheduler` で起動する。"""

from __future__ import annotations

import logging
import threading
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
from .notify.events import detect_signal_changes
from .notify.service import Notifier
from .scoring.run import ScoreContext, score_venue
from .tokens import load_tokens

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    config, venues = runtime.build()
    log.info("scheduler starting", extra={"data": {"mode": config.mode, "snapshot_minutes": config.snapshot_minutes}})
    notifier = Notifier.from_env(config)

    def snapshot_job() -> None:
        conn = db.connect(config.database_path)
        try:
            for v in venues:
                try:
                    v.ensure_chain()
                except Exception as exc:
                    # 失敗の記録を残して、欠けチェックに「failed」として出るようにする
                    record_failed_run(conn, v.adapter.venue_id, config.snapshot_minutes, f"チェーンの確認に失敗: {exc}")
                    notifier.error(v.adapter.venue_id, "データ収集の失敗（チェーンにつながりません）", str(exc))
                    continue
                res = collect_venue(conn, v.adapter, v.rpc, snapshot_minutes=config.snapshot_minutes,
                                    epoch_fresh_minutes=config.epoch_fresh_minutes,
                                    reward_drop_alert_pct=config.reward_drop_alert_pct)
                if res.status == "failed":
                    notifier.error(v.adapter.venue_id, "データ収集の失敗", res.error or "全プールで読み取りに失敗")
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
                    rows = score_venue(conn, ctx)
                    if rows:
                        # 判定の変化（新しく🟢 / 🟢→🔴）を通知の箱に入れる（SPEC 8章）
                        detect_signal_changes(conn, ctx.venue["id"], ctx.venue.get("name") or ctx.venue["id"],
                                              rows[0]["ts"])
                except Exception as exc:
                    log.exception("scoring failed", extra={"data": {"venue": ctx.venue["id"]}})
                    notifier.error(ctx.venue["id"], "スコア計算の失敗", f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        notifier.tick()

    def notify_job() -> None:
        try:
            notifier.tick()
        except Exception:
            log.exception("notify failed")

    def report_job() -> None:
        try:
            notifier.daily_report()
        except Exception as exc:
            log.exception("daily report failed")
            notifier.error("-", "毎朝のレポートの作成に失敗", f"{type(exc).__name__}: {exc}")

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
    # 通知（M4）: 送っていないお知らせを1分ごとに送る。毎朝のレポートは日本時間 8:00
    n = config.notify
    sched.add_job(notify_job, IntervalTrigger(minutes=n.send_every_minutes), id="notify", max_instances=1,
                  coalesce=True, misfire_grace_time=None)
    sched.add_job(report_job, CronTrigger(hour=n.daily_report_hour_jst, minute=n.daily_report_minute,
                                          timezone="Asia/Tokyo"),
                  id="daily_report", max_instances=1, coalesce=True, misfire_grace_time=None)
    if notifier.report_due():
        # 8時より後に起動した日（再起動など）は、起動の少し後に今日のレポートを作る
        sched.add_job(report_job, "date", run_date=datetime.now(UTC) + timedelta(minutes=10), id="daily_report_late")
    # /status などのコマンドを待つ（オーナーの ID だけ受け付ける）
    threading.Thread(target=notifier.run_bot, args=(threading.Event(),), daemon=True, name="telegram-bot").start()
    snapshot_job()  # 起動直後にも1回実行して動作を確認する
    sched.start()


if __name__ == "__main__":
    main()
