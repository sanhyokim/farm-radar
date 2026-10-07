"""一覧の保存を常に動かす（N2a）。`python -m farm_radar.feeds.scheduler` で起動する（docker compose の feeds）。

時刻（UTC の分）は、今の版（ポート 18000）の仕事（毎時 0・5・15・30・45 分）を避ける（SPEC 13.4）。
- Merkl の機会: 毎時 7・22・37・52 分
- Aero のお知らせ・Aero の公式の住所・影の記録（N5a）: 毎時 47 分
- N6 の仮想のお金の練習: Merkl の機会の回（毎時 7・22・37・52 分）のいちばん最後
- 1日1回の一覧: 毎時 23 分に「今日まだ読めていないか」を確かめ、日本時間 4 時を過ぎていれば読む
起動したときにも1回ずつ読む（パソコンの再起動のあと、すぐ保存を続けるため）。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import ratelimit
from ..config import load_config
from ..logging_setup import setup_logging
from . import store
from .run import FeedRunner

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    config = load_config()
    fs = config.feeds
    store.connect(fs.database_path).close()            # 表を作っておく
    ratelimit.set_sink(fs.database_path)               # 429 を受けたら feeds.sqlite3 に記録する
    from ..registry import coin_chains
    coins = coin_chains(config.chains, config.root)
    if not coins:                                      # chains フォルダーが見えないと、コインの値段を読めない
        log.warning("coin prices off: no chain with defillama_coins_key", extra={"data": {"chains": config.chains}})
    from ..registry import receipt_chains, stable_addresses
    from .run import ReceiptContext
    rchains = receipt_chains(config.chains, config.root)
    from ..venue_match import load_known
    ctx = ReceiptContext(chains=rchains, stables={cid: stable_addresses(c, config.root) for cid, c in rchains.items()},
                         known=load_known(config.root))
    # N5a: 試すための記録（Merkl の配った額・DefiLlama と Lighter の過去・Aero の住所・影の記録）
    from ..registry import llama_chain_names
    from .shadow import make_recorder
    from .trial import TrialContext
    from ..tokens import load_tokens
    book = load_tokens(root=config.root)
    lighter = tuple(sorted({(r.market_id, r.symbol) for t in book.perp_alts for r in book.perp_candidates(t)
                            if r.venue == "lighter"} | {(r.market_id, r.symbol) for r in book.perps.values()
                                                       if r.venue == "lighter"}))
    from ..n6.engine import make_runner
    tctx = TrialContext(chain_ids=tuple(coins), llama_chains=llama_chain_names(config.chains, config.root),
                        perp_alias=dict(config.opportunities.perp_alias), shadow=make_recorder(config),
                        lighter_markets=lighter, n6=make_runner(config))
    runner = FeedRunner(fs, coin_chains=coins, receipt_ctx=ctx, trial_ctx=tctx)
    lock = threading.Lock()                            # 同時に2つ読まない（回数制限とパソコンの負荷のため）

    def job(cadence: str) -> None:
        with lock:
            try:
                runner.run(cadence)
            except Exception:
                log.exception("feed job failed", extra={"data": {"cadence": cadence}})

    sched = BlockingScheduler(timezone=UTC)
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 600}
    sched.add_job(job, CronTrigger(minute=",".join(map(str, fs.merkl_minutes)), timezone="UTC"),
                  args=["15min"], id="feeds_15min", **common)
    sched.add_job(job, CronTrigger(minute=fs.hourly_minute, timezone="UTC"), args=["hourly"], id="feeds_hourly",
                  **common)
    sched.add_job(job, CronTrigger(minute=fs.daily_minute, timezone="UTC"), args=["daily"], id="feeds_daily",
                  **common)
    # 起動したときにも1回ずつ（少しずらして、今の版の仕事と重ならないように）
    start = datetime.now(UTC) + timedelta(seconds=20)
    for i, cadence in enumerate(("15min", "hourly", "daily")):
        sched.add_job(job, "date", run_date=start + timedelta(seconds=5 * i), args=[cadence],
                      id=f"feeds_startup_{cadence}")
    log.info("feeds scheduler starting", extra={"data": {"database": str(fs.database_path),
                                                         "raw_dir": str(fs.raw_dir)}})
    sched.start()


if __name__ == "__main__":
    main()
