"""「守る」の画面の数字（N2c。SPEC 13.1 の追加の決定 2）: 置いている額と上限、損失ライン。

今ある数字だけで作る（N4 で中身を足す）。
- 上限は config.yaml の limits（変えるのはオーナーだけ。絶対ルール5）。1つの会場の上限 = 合計の上限 × 会場の割合。
- チェーンごとの上限はまだ決めていない（全体の上限までの残りを出す）。
- 損失ライン: 今日（日本時間）の損が、置いている額の risk.emergency_daily_loss_pct（%）に達したら全部閉じる（今の版の緊急離脱）。
- 保険の預け金（Lighter）を会場として数えるのは N4（今は建玉の中に入っている）。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import safety
from .config import Config, ConfigError, load_venue
from .execution import views as paper_views
from .execution.paper import OFFICIAL_SQL
from .registry import load_chain

NEAR_LINE = 0.7              # 損失ラインの7割に来たら「近い」（仮）


def summary(conn: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    lim = config.limits
    total_cap = float(lim["total_usd"]) if "total_usd" in lim else None
    share = float(lim["per_venue_share"]) if "per_venue_share" in lim else None
    venue_cap = total_cap * share if total_cap is not None and share is not None else None
    outside = config.evaluation.reference_outside_limits
    rows = conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open'"
                        + (f" AND {OFFICIAL_SQL}" if outside else "")).fetchall()
    cards = [paper_views.card(conn, p, now, config.risk) for p in rows]

    venues: dict[str, dict[str, Any]] = {}
    for vid in config.venues:
        try:
            v = load_venue(vid, config.root)
        except (OSError, ConfigError):
            continue
        venues[vid] = {"venue_id": vid, "name": v.get("name", vid), "chain": (v.get("chain") or {}).get("id"),
                       "practice": v.get("practice", True) is not False, "safety": safety.venue_from_registry(v),
                       "placed_usd": 0.0, "positions": 0}
    for c in cards:
        row = venues.setdefault(c["venue_id"], {"venue_id": c["venue_id"], "name": c["venue_id"], "chain": None,
                                                "practice": True, "safety": None, "placed_usd": 0.0, "positions": 0})
        row["placed_usd"] += float(c["capital"] or 0.0)
        row["positions"] += 1
    for row in venues.values():
        row["cap_usd"] = venue_cap
        row["left_usd"] = max(0.0, venue_cap - row["placed_usd"]) if venue_cap is not None else None
        row["used_frac"] = row["placed_usd"] / venue_cap if venue_cap else None

    placed = sum(float(c["capital"] or 0.0) for c in cards)
    chains: dict[str, dict[str, Any]] = {}
    for cid in config.chains:
        try:
            ch = load_chain(cid, config.root)
        except ConfigError:
            continue
        chains[cid] = {"chain": cid, "name": ch.get("name", cid), "placed_usd": 0.0, "venues": 0}
    for row in venues.values():
        row["chain_name"] = (chains.get(row["chain"] or "") or {}).get("name") or row["chain"]
        ch = chains.get(row["chain"] or "")
        if ch is not None:
            ch["placed_usd"] += row["placed_usd"]
            ch["venues"] += 1
    total_left = max(0.0, total_cap - placed) if total_cap is not None else None
    for ch in chains.values():
        ch["cap_usd"] = None                    # チェーンごとの上限はまだ決めていない
        ch["left_usd"] = total_left             # 全体の上限までの残り

    today = sum(float(c["today_usd"] or 0.0) for c in cards)
    pct_line = config.risk.emergency_daily_loss_pct
    line_usd = -placed * pct_line / 100 if placed else None
    if line_usd is None:
        state = "none"
    elif today <= line_usd:
        state = "hit"
    elif today <= line_usd * NEAR_LINE:
        state = "near"
    else:
        state = "ok"
    return {
        "limits": {"position_usd": lim.get("position_usd"), "total_usd": total_cap, "per_venue_share": share,
                   "venue_cap_usd": venue_cap, "trades_per_day": lim.get("trades_per_day")},
        "placed_usd": placed, "total_left_usd": total_left, "positions": len(cards),
        "venues": sorted(venues.values(), key=lambda r: (-r["placed_usd"], r["name"])),
        "chains": list(chains.values()),
        "pnl": {"open_change_usd": sum(float(c["change_usd"] or 0.0) for c in cards), "today_usd": today},
        "loss_line": {"pct": pct_line, "line_usd": line_usd, "today_usd": today, "state": state,
                      "near_frac": NEAR_LINE,
                      "used_frac": (today / line_usd) if line_usd and today < 0 else 0.0},
        "notes": ["チェーンごとの上限はまだ決めていません。全体の上限までの残りを出しています。",
                  "保険の預け金（Lighter）を会場として数えるのは N4 からです（今は建玉の額に入っています）。",
                  "上限の数字は config.yaml の limits です。変えるのはオーナーだけです。"],
    }
