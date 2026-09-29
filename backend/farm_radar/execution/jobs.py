"""練習（ペーパートレード）の定期ジョブ。収集のあとに呼ぶ（M5a）。

- 持っている建玉の perp の資金調達率（Lighter の1時間ごとの実績）を取っておく
- 新しい記録の分だけ、建玉の損益を計算する
- 円のレートが取れていなかった台帳の行を埋める
- 危険判定のルールで見張り、仮想的に置き直す・閉じる（M5b。risk_job.py）
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from ..config import Config
from ..fx import Frankfurter, fill_ledger_jpy
from ..tokens import TokenBook
from .paper import PaperExecutor
from .risk_job import run_risk

log = logging.getLogger(__name__)


def refresh_funding(conn: sqlite3.Connection, lighter, market_ids: set[int], now: datetime, hours: int = 6) -> int:
    """直近 hours 時間の資金調達率を funding_rates 表に入れる。失敗しても止めない（その時間は0で推定の印）。"""
    end = int(now.timestamp())
    n = 0
    for mid in sorted(market_ids):
        try:
            rows = lighter.short_funding_hourly(mid, end - hours * 3600, end)
        except Exception as exc:
            log.warning("funding fetch failed", extra={"data": {"market_id": mid, "error": str(exc)}})
            continue
        conn.executemany("INSERT OR REPLACE INTO funding_rates(market_id, ts, short_rate) VALUES (?,?,?)",
                         [(mid, t // 3600 * 3600, r) for t, r in rows])
        n += len(rows)
    conn.commit()
    return n


def run_paper(conn: sqlite3.Connection, config: Config, tokens: TokenBook, lighter=None,
              fx: Frankfurter | None = None, now: datetime | None = None) -> int:
    """練習モードのときだけ動く。計算した行の数を返す。"""
    if config.mode != "paper":
        return 0
    now = now or datetime.now(UTC)
    positions = conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open'").fetchall()
    if not positions:
        return 0
    if lighter is not None:
        markets = {int(h["market_id"]) for p in positions for h in json.loads(p["hedges_json"] or "[]")}
        if markets:
            # パソコンが止まっていた分もさかのぼって取る（最長7日）
            oldest = min(datetime.fromisoformat(p["last_ts"]) for p in positions)
            hours = min(168, max(6, int((now - oldest).total_seconds() // 3600) + 2))
            refresh_funding(conn, lighter, markets, now, hours)
    ex = PaperExecutor(conn, config, tokens, fx=fx, now=now)
    n = 0
    for p in positions:
        try:
            n += ex.update(p)
        except Exception:
            log.exception("paper update failed", extra={"data": {"position": p["id"]}})
    try:
        fill_ledger_jpy(conn, fx)
    except Exception:
        log.exception("fx fill failed")
    # 見張り（失敗したら scheduler がエラーとして通知する）
    run_risk(conn, config, tokens, ex, now)
    if n:
        log.info("paper positions updated", extra={"data": {"positions": len(positions), "rows": n}})
    return n
