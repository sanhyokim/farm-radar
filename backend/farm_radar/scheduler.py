"""常時動かす収集プロセス。`python -m farm_radar.scheduler` で起動する。"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import discovery as discovery_mod
from . import market_calendar, ratelimit, runtime
from .collectors import priority
from .config import chain_reads_enabled
from .collectors.snapshot import collect_venue, record_failed_run, slot_for
from .db import database as db
from .execution.review import make_review
from .external.geckoterminal import GeckoTerminal
from . import hedges as hedge_mod
from .logging_setup import setup_logging
from .execution.jobs import run_paper
from .notify.events import detect_signal_changes
from .notify.service import Notifier
from .scoring.run import ScoreContext, score_venue
from .tokens import load_tokens

log = logging.getLogger(__name__)


def in_fast_window(now: datetime, window: tuple[str, str], trading_days_only: bool = True) -> bool:
    """ニューヨーク時間で window（"09:00", "10:30"）の中か。日をまたぐ窓（"23:00", "01:00"）にも対応する。

    夏時間・冬時間はニューヨークの時計で数えるので自動で合う（2026-09-29 オーナー決定）。
    trading_days_only なら、米国市場が開く日（ニューヨークの日付で。土日・休日は除く。M5d）だけ True。
    """
    local = now.astimezone(market_calendar.NY)
    cur = local.hour * 60 + local.minute
    a, b = ((int(x.split(":")[0]) * 60 + int(x.split(":")[1])) for x in window)
    inside = a <= cur <= b if a <= b else (cur >= a or cur <= b)
    if not inside or not trading_days_only:
        return inside
    return market_calendar.is_trading_day(now.astimezone(market_calendar.NY).date())


def main() -> None:
    setup_logging()
    if not chain_reads_enabled():
        # 並べて動かす新しい版（SPEC 13.4）: チェーンは読まない。止まって再起動をくり返さないよう、待つだけにする
        log.warning("chain reads are off (FARM_RADAR_CHAIN_READS=off); collector is idle")
        threading.Event().wait()
        return
    config, venues = runtime.build()
    ratelimit.set_sink(config.database_path)   # 429 を受けたら記録する（SPEC 5.3章。M6）
    # 練習と評価に使う会場（up.）を先に、観察だけの会場（Alandale）を後に（SPEC 5.1章。2026-09-30 オーナー条件）
    main_venues, observe_venues = priority.split_venues(venues)
    venues = main_venues + observe_venues
    log.info("scheduler starting", extra={"data": {"mode": config.mode, "snapshot_minutes": config.snapshot_minutes}})
    notifier = Notifier.from_env(config)

    lock = threading.Lock()

    def snapshot_job(fast: bool = False) -> None:
        # 5分ごとの見張り（fast）は、ふつうの15分ごとの収集と重なったら休む
        if not lock.acquire(blocking=not fast):
            return
        conn = db.connect(config.database_path)
        try:
            slot = slot_for(datetime.now(UTC), config.snapshot_minutes)
            main_results = {}
            for v in venues:
                observe = v.venue["id"] in observe_ids
                if observe and fast:
                    continue   # 開場前後の5分ごとの記録は練習の見張り用。観察だけの会場では行わない
                if observe:
                    # 観察だけの会場は、up. の収集と評価を優先して、条件によってはこの回を休む
                    reason = priority.defer_reason(conn, config, main_results, slot, datetime.now(UTC))
                    if reason:
                        priority.record_deferred(conn, v.adapter.venue_id, slot, datetime.now(UTC), reason,
                                                 config.snapshot_minutes)
                        continue
                try:
                    v.ensure_chain()
                except Exception as exc:
                    # 失敗の記録を残して、欠けチェックに「failed」として出るようにする
                    record_failed_run(conn, v.adapter.venue_id, config.snapshot_minutes, f"チェーンの確認に失敗: {exc}")
                    notifier.error(v.adapter.venue_id, "データ収集の失敗（チェーンにつながりません）", str(exc))
                    if not observe:
                        main_results[v.adapter.venue_id] = priority.RunResult(0, "failed", None, 0, 0, str(exc))
                    continue
                res = collect_venue(conn, v.adapter, v.rpc, snapshot_minutes=config.snapshot_minutes,
                                    epoch_fresh_minutes=config.epoch_fresh_minutes,
                                    reward_drop_alert_pct=config.reward_drop_alert_pct)
                if not observe:
                    main_results[v.adapter.venue_id] = res
                if res.status == "failed":
                    notifier.error(v.adapter.venue_id, "データ収集の失敗", res.error or "全プールで読み取りに失敗")
            # 練習（M5a・M5b）: 新しい記録の分だけ損益を計算し、見張りのルールで調べる（mode が paper のときだけ）
            try:
                v0 = venues[0]
                run_paper(conn, config, paper_tokens, hedges=hedges, rpc=v0.rpc, venue=v0.venue,
                          gt=contexts[0].gt if contexts else None, fast=fast)
            except Exception as exc:
                log.exception("paper job failed")
                notifier.error("-", "練習の損益の計算・見張りに失敗", f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
            lock.release()

    def fast_job() -> None:
        """米国市場の開場前後（config の risk.fast_window_ny。ニューヨーク時間）だけ、練習の建玉があれば短い間隔で記録して見張る（SPEC 5.1章）。"""
        if config.mode != "paper" or not in_fast_window(datetime.now(UTC), config.risk.fast_window_ny):
            return
        conn = db.connect(config.database_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='open'").fetchone()[0]
        finally:
            conn.close()
        if n:
            snapshot_job(fast=True)

    # ヘッジ先（SPEC 5.2.1章。config.yaml の hedge_venues。読み取りだけ）
    hedges = hedge_mod.build([h.hedge_id for h in config.hedge_venues])
    paper_tokens = load_tokens(venues[0].venue["chain"]["id"], config.root) if venues else None
    # GeckoTerminal の読み手はチェーンごとに1つだけ作って、会場どうしで使い回す（回数制限を守るため。M6）
    gts: dict[str, GeckoTerminal] = {}
    for v in venues:
        net = v.venue["chain"].get("geckoterminal_network")
        if net and net not in gts:
            gts[net] = GeckoTerminal(net)
    contexts = [
        ScoreContext(
            venue=v.venue, tokens=load_tokens(v.venue["chain"]["id"], config.root), settings=config.scoring,
            stale_after_minutes=config.stale_after_minutes, rpc=v.rpc, hedges=hedges,
            gt=gts.get(v.venue["chain"].get("geckoterminal_network") or ""),
        )
        for v in venues
    ]
    observe_ids = {v.venue["id"] for v in observe_venues}

    def score_job() -> None:
        """スコア計算と判定（SPEC 5.1章: 1時間ごと）。外部サイトが落ちていても収集は止めない。"""
        conn = db.connect(config.database_path)
        try:
            for ctx in contexts:
                if ctx.venue["id"] in observe_ids:
                    hits = ratelimit.recent(conn, datetime.now(UTC), config.observe_venues.rate_limit_quiet_minutes)
                    if hits:
                        # 観察だけの会場の計算は外部データを読むので、429 のあとは次の回にまわす（M6）
                        log.info("observe venue scoring deferred", extra={"data": {
                            "venue": ctx.venue["id"], "rate_limits": len(hits)}})
                        continue
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

    def review_job() -> None:
        """練習の定時レビュー（M5c。SPEC 8.5章: 30分ごと）。"""
        conn = db.connect(config.database_path)
        try:
            make_review(conn, config)
        except Exception as exc:
            log.exception("review failed")
            notifier.error("-", "練習の定時レビューの作成に失敗", f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

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

    def discovery_job(trigger: str = "weekly") -> None:
        """候補の会場の一覧（週1回。SPEC 5.2.2章）。読み取りだけ。"""
        try:
            r = discovery_mod.run_now(config, trigger)
            if r and r["status"] == "error":
                notifier.error("discovery", "候補の会場の一覧の読み取りに失敗", " / ".join(r["errors"]))
        except Exception as exc:
            log.exception("discovery failed")
            notifier.error("discovery", "候補の会場の一覧の作成に失敗", f"{type(exc).__name__}: {exc}")

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
    # 練習の見張り（M5b）: 開場前後の時間だけ、ふつうの収集の間の時刻にも記録を取る
    r = config.risk
    fast_minutes = [m for m in range(0, 60, r.fast_minutes) if m % minutes]
    if config.mode == "paper" and fast_minutes and 60 % minutes == 0:
        h0, h1 = int(r.fast_window_ny[0].split(":")[0]), int(r.fast_window_ny[1].split(":")[0])
        hours = f"{h0}-{h1}" if h0 <= h1 else f"{h0}-23,0-{h1}"
        sched.add_job(fast_job, CronTrigger(minute=",".join(map(str, fast_minutes)), hour=hours,
                                            timezone="America/New_York"),
                      id="paper_fast", max_instances=1, coalesce=True, misfire_grace_time=60)
    # 定時レビュー（M5c）: 収集と練習の計算が終わったあと（毎時2分・32分など）に作る
    if config.mode == "paper":
        rm = config.review.every_minutes
        review_trigger = (CronTrigger(minute=f"2-59/{rm}", timezone="UTC") if rm < 60
                          else CronTrigger(minute="2", hour=f"*/{rm // 60}", timezone="UTC"))
        sched.add_job(review_job, review_trigger, id="paper_review", max_instances=1, coalesce=True,
                      misfire_grace_time=300)
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
    # 候補の会場の一覧（週1回。E1）。止めていて前回から7日以上たっていたら、起動の5分後にも1回
    ds = config.discovery
    sched.add_job(discovery_job, CronTrigger(day_of_week=ds.weekday, hour=ds.hour_jst, minute=ds.minute_jst,
                                             timezone="Asia/Tokyo"),
                  id="discovery", max_instances=1, coalesce=True, misfire_grace_time=None)
    conn = db.connect(config.database_path)
    try:
        due = discovery_mod.due_at_startup(conn, datetime.now(UTC))
    finally:
        conn.close()
    if due:
        sched.add_job(discovery_job, "date", run_date=datetime.now(UTC) + timedelta(minutes=5), args=["startup"],
                      id="discovery_startup")
    # /status などのコマンドを待つ（オーナーの ID だけ受け付ける）
    threading.Thread(target=notifier.run_bot, args=(threading.Event(),), daemon=True, name="telegram-bot").start()
    snapshot_job()  # 起動直後にも1回実行して動作を確認する
    sched.start()


if __name__ == "__main__":
    main()
