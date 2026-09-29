"""ヘッジ先の担保の状態（SPEC 5.2.1章。2026-09-29 オーナー追加）。読み取った記録から画面用にまとめるだけ。

- 担保なし: アドレスが未設定、見つからない、または残高が0
- 担保不足: 残高が、ヘッジに使う予定の証拠金（総資産 × scoring.allocation.hedge_margin。初期設定 $400）より少ない
- ヘッジ可能: 残高がそれ以上
練習モードでは、仮の担保額（総資産の40%）で扱う（本物の残高は参考に並べる）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..config import Config
from . import REGISTRY
from .base import mask

STATE_JA = {"ok": "ヘッジ可能", "short": "担保不足", "none": "担保なし", "error": "読み取れません"}


def _judge(collateral: float | None, need: float) -> str:
    if collateral is None:
        return "error"
    if collateral <= 0:
        return "none"
    return "ok" if collateral >= need else "short"


def summary(conn: sqlite3.Connection, config: Config) -> dict[str, Any]:
    s = config.scoring
    need = s.total_capital_usd * s.allocation_hedge_margin
    out = []
    for hv in config.hedge_venues:
        cls = REGISTRY.get(hv.hedge_id)
        name = getattr(cls, "name", hv.hedge_id)
        fees = conn.execute("SELECT COUNT(*), MAX(ts), MAX(taker_pct) FROM hedge_fees WHERE hedge_id=?",
                            (hv.hedge_id,)).fetchone()
        real = None
        if hv.account_address:
            row = conn.execute("SELECT * FROM hedge_accounts WHERE hedge_id=?", (hv.hedge_id,)).fetchone()
            if row is None:
                real = {"state": "waiting", "state_ja": "まだ読んでいません（15分ごとに読みます）"}
            else:
                st = "none" if row["error"] == "not_found" else ("error" if row["error"] else _judge(row["collateral_usd"], need))
                real = {"state": st, "state_ja": STATE_JA[st], "collateral_usd": row["collateral_usd"],
                        "available_usd": row["available_usd"], "positions": json.loads(row["positions_json"] or "[]"),
                        "read_at": row["ts"]}
        if config.mode == "paper":
            eff = {"state": "ok", "state_ja": STATE_JA["ok"], "collateral_usd": need, "paper": True,
                   "note": f"練習なので、仮の担保 ${need:,.0f}（総資産の{s.allocation_hedge_margin * 100:.0f}%）で扱っています。"}
        elif real is not None and real["state"] != "waiting":
            eff = {**real, "paper": False}
        else:
            eff = {"state": "none", "state_ja": STATE_JA["none"], "paper": False,
                   "note": "担保を確かめるアドレスが設定されていません（config.yaml の hedge_venues か .env）。"}
        out.append({"hedge_id": hv.hedge_id, "name": name, "address": mask(hv.account_address),
                    "markets": fees[0], "fees_at": fees[1], "max_taker_pct": fees[2],
                    "need_usd": need, "status": eff, "real": real})
    return {"mode": config.mode, "need_usd": need, "venues": out}
