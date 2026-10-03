"""影の記録（N5a。docs/n5-plan-2026-10-03.md の 2-2。判断③A 2026-10-03 オーナー）。

「探す」の一覧に出る行のうち、狙い利回り以上の行と、$1,000 の並びで上位30行について、毎時間の見込みをメモする。
建玉は作らない（ボタンなし。SPEC 13.2「評価のボタンなしで自動で記録し、あとから期間を切り出して判定する」）。
1日後・7日後に、その時間に実際に起きたこと（チェーンの記録・Merkl の配った額）と比べる（N5b・N5c）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

TOP_N = 30
AMOUNTS = (1000.0, 10000.0)        # 練習の金額（SPEC 13.1 の7）
RANK_AMOUNT = 1000.0


def select(ops: list[Any], target_apr_pct: float, top_n: int = TOP_N) -> list[tuple[int, Any]]:
    """メモする行: 一覧に出るもの（計算できて、外していない）の、狙い利回り以上と上位 top_n 行。(並び順, 機会)。"""
    from ..opportunities import rank
    ranked = rank([o for o in ops if o.computable], RANK_AMOUNT, target_apr_pct)
    out = []
    for i, o in enumerate(ranked):
        b = o.best(RANK_AMOUNT)
        if i < top_n or (b is not None and b.apr_pct >= target_apr_pct):
            out.append((i, o))
    return out


def _variant(o: Any, amount: float, case: str) -> Any:
    return o.best(amount, case)


def rows(ops: list[Any], target_apr_pct: float, ts: str) -> list[tuple]:
    out = []
    for i, o in select(ops, target_apr_pct):
        for amount in AMOUNTS:
            c, n = _variant(o, amount, "cautious"), _variant(o, amount, "normal")
            if c is None and n is None:
                continue
            detail = {"cautious": c.to_dict() if c else None, "normal": n.to_dict() if n else None,
                      "name": o.base.name, "venue": o.base.venue, "tvl_usd": o.base.tvl_usd,
                      "bonus_usd_per_day": o.base.bonus_usd_per_day, "bonus_token": o.base.bonus_token,
                      "days_left": o.base.days_left, "cap_usd": o.cap_usd,
                      "campaigns": [x.get("campaign_id") for x in o.campaigns if isinstance(x, dict)][:10],
                      "flags": [f.code for f in o.flags]}
            out.append((ts, o.base.key, amount, o.base.chain, o.kind, i, target_apr_pct,
                        c.apr_pct if c else None, n.apr_pct if n else None,
                        c.net_after_move if c else None, n.net_after_move if n else None,
                        int(bool(c and c.hedge)), int(o.recommended(amount, target_apr_pct)),
                        json.dumps(detail, ensure_ascii=False, default=str)))
    return out


def write(fconn: sqlite3.Connection, data: list[tuple]) -> int:
    fconn.executemany("INSERT OR REPLACE INTO shadow_predictions(ts, opp_key, amount, chain, kind, rank, target_apr_pct, "
                      "apr_cautious, apr_normal, net_cautious, net_normal, hedge, recommended, detail_json) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    return len(data)


def make_recorder(config: Any):
    """scheduler から呼ぶ: 新しい版のデータベース（練習・設定）を開いて、一覧を計算してメモの行を作る。"""
    from .. import app_settings
    from .. import opportunities as opps
    from ..db import database as db

    def record(fconn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        conn = db.connect(config.database_path)
        try:
            target = app_settings.target_apr_pct(conn, config)
            ops = opps.collect(conn, config, now)
        finally:
            conn.close()
        ts = now.isoformat(timespec="seconds")
        return {"ts": ts, "target": target, "rows": rows(ops, target, ts), "total": len(ops)}

    return record
