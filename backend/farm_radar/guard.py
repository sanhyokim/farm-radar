"""「守る」の画面の数字（N2c。SPEC 13.1 の追加の決定 2。N4b で中身を足した）。

- 置いている額と上限: 上限は config.yaml の limits（変えるのはオーナーだけ。絶対ルール5）。1つの会場の上限 = 合計の上限 × 会場の割合。
  保険に預けるお金は、会場ではなく「Lighter」に置いているお金として数え、Lighter の行を出す（N4b）。
- チェーンごとの上限はまだ決めていない（全体の上限までの残りを出す）。
- 損の線（N4b。追加の決定 10）: 3つの期間 × 3つの段階と、今の位置・内訳（execution/loss_lines.py）。
- 早く出る4段階（N4b。追加の決定 9）: 段階ごとの決まりと、最近の合図。
- 保険の強制決済までの余裕（N4b）: 建玉ごと。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import safety
from .config import Config, ConfigError, load_venue
from .execution import hedge_guard, loss_lines, risk_job
from .execution import views as paper_views
from .execution.paper import OFFICIAL_SQL
from .risk.rules import STAGE, STAGE_JA
from .registry import load_chain



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
    margins = {p["id"]: hedge_guard.margin_of(p, config) for p in rows}
    lighter = {"venue_id": "lighter", "name": "Lighter（保険の預け金）", "chain": None, "practice": True, "safety": None,
               "placed_usd": 0.0, "positions": 0, "hedge": True}
    for p, c in zip(rows, cards):
        row = venues.setdefault(c["venue_id"], {"venue_id": c["venue_id"], "name": c["venue_id"], "chain": None,
                                                "practice": True, "safety": None, "placed_usd": 0.0, "positions": 0})
        row["placed_usd"] += float(c["capital"] or 0.0) - margins[p["id"]]
        row["positions"] += 1
        if margins[p["id"]] > 0:
            lighter["placed_usd"] += margins[p["id"]]
            lighter["positions"] += 1
    venues["lighter"] = lighter
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
    loss = loss_lines.status(conn, config.guard, now)
    day = next(p for p in loss["periods"] if p["period"] == "day")
    pct_line = -config.guard.loss_lines["day"]["stop"]
    near = config.guard.loss_lines["day"]["caution"] / config.guard.loss_lines["day"]["stop"]
    line_usd = -placed * pct_line / 100 if placed else None
    if line_usd is None:
        state = "none"
    elif day["level"] == "stop" or today <= line_usd:
        state = "hit"
    elif day["level"] is not None or today <= line_usd * near:
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
                      "near_frac": near,
                      "used_frac": (today / line_usd) if line_usd and today < 0 else 0.0},
        "loss_lines": loss,
        "stages": stage_table(config),
        "signals": recent_signals(conn),
        # 段階1の合図のあとの「出ていたら／残っていたら」（2026-10-04 オーナー決定 A の追加1）
        "stage1_watch": risk_job.stage1_watch_rows(conn),
        "margin_log": risk_job.margin_log_rows(conn),
        "hedges": [{"position_id": p["id"], "pair": c["pair"], "margin_usd": margins[p["id"]],
                    "status": hedge_guard.margin_status(conn, config, p)} for p, c in zip(rows, cards) if margins[p["id"]] > 0],
        "notes": ["チェーンごとの上限はまだ決めていません。全体の上限までの残りを出しています。",
                  "保険に預けるお金は、会場ではなく「Lighter」に置いているお金として数えます（Lighter も1つの会場と同じ上限）。",
                  "上限の数字は config.yaml の limits です。変えるのはオーナーだけです。",
                  "損の線と4段階の数字は仮です（N5 の試しで決め直します）。練習では4つの段階とも自動で出て、どの決まりで出たかを記録します。"],
    }


def stage_table(config: Config) -> list[dict[str, Any]]:
    """早く出る4段階の決まり（今の数字）。練習では4つの段階とも自動で出る（2026-10-02 23:34 JST オーナー決定 9）。"""
    r, g = config.risk, config.guard
    bonus_auto = r.bonus_drop_action == "exit"
    return [
        {"stage": 1, "label": STAGE_JA[1], "auto": True, "waits_for_gas": False,
         "rules": [f"自分の建玉の値打ちが1時間で−{r.emergency_own_value_drop_1h_pct:g}%",
                   f"プールのお金が1時間で−{r.emergency_pool_funds_drop_1h_pct:g}%"
                   + ("" if r.pool_funds_drop_action == "exit" else
                      f"（自動では出ません。知らせて、そのプールに新しく入るのを{r.pool_funds_block_hours:g}時間止めます。"
                      "出るのは手で。2026-10-04 オーナー決定）"),
                   "会場のプログラムの停止・持ち主の変更・入れ替え",
                   f"USDG の外部の価格が{r.emergency_usdg_times}回続けて ${r.emergency_usdg_below:g} 未満",
                   "保険（Lighter の売り）が強制決済の線に届いた",
                   "金庫の運用先が変わった（Merkl の金庫。一覧と詳しい画面に印。練習は N6）"]},
        {"stage": 2, "label": STAGE_JA[2], "auto": True, "waits_for_gas": True,
         "rules": [f"残る利回りが狙い利回りを{g.below_target_times}回続けて下回った（狙いより低いと分かって始めた練習は除く）",
                   f"ボーナスのコインが24時間で{r.exit_reward_token_24h_pct:g}%",
                   f"値動きするコインが1時間で{r.exit_dump_1h_pct:g}%か24時間で{r.exit_dump_24h_pct:g}%（投げ売り）",
                   "判定が🔴になった（🔴で始めた練習は除く）", "幅の外で、置き直しても利回りが低い"]},
        {"stage": 3, "label": STAGE_JA[3], "auto": bonus_auto, "waits_for_gas": True,
         "rules": [f"木曜の切り替えでボーナスが前の週の{r.bonus_drop_ratio * 100:g}%以下になり、抜けるのがいちばん損が少ない"
                   + ("" if bonus_auto else "（今は記録と知らせだけ。config.yaml の risk.bonus_drop_action が record）"),
                   # 2026-10-03 オーナー決定②A: 「終わりが近い」= 終わりまでに稼げる見込みが、移る費用より小さくなったとき
                   "キャンペーンの終わりまでに稼げる見込みが、移る費用より小さくなった（終わりの時刻がある Merkl の場所。練習は N6）"]},
        {"stage": 4, "label": STAGE_JA[4], "auto": True, "waits_for_gas": True,
         "rules": [f"（利回りの差 × 次の切り替えまでの日数）が、移る費用の{g.better_cost_multiple:g}倍より大きい"
                   "（比べる先は練習ができる会場のほかのプールで、🔴でなく狙い利回り以上。移った先の練習は自分で始める）"]},
    ]


def recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """最近の合図（4段階の決まり・損の線・保険）。新しい順。"""
    kinds = [*STAGE, "loss_line_caution", "loss_line_no_new", "loss_line_stop", "hedge_margin_low", "hedge_adjust"]
    return paper_views.events(conn, None, limit=limit, kinds=kinds)
