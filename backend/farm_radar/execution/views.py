"""練習（ペーパートレード）の画面用のまとめ（SPEC 7.4章・7.6章・7.7章。M5a）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .. import views
from .paper import CATS, lp_amounts

RED_START_LABEL = "🔴で開始した練習"
RED_START_NOTE = ("判定が🔴のプールで始めた練習です。「判定が🔴になったら離脱」のルールは当てはめません"
                  "（ほかの離脱・緊急離脱のルールは当てはめます。M5b）。")
ESTIMATE_NOTES = [
    "報酬は15分ごとの記録（実際の報酬の量・ステーク流動性・価格）から計算しています。",
    "perp の値段は、プールから出したドル価格で代用しています。",
    "ステークしない建玉の手数料は、1日の取引量からの推定です。",
    "収集が止まっていた時間の損益には「推定」の印を付け、評価から外せるようにしています。",
]


def _pnl_rows(conn: sqlite3.Connection, pid: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts", (pid,))]


def _sum(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {c: sum(float(r[c] or 0.0) for r in rows) for c in CATS}


def _pair(conn: sqlite3.Connection, pool_id: str) -> tuple[str, sqlite3.Row]:
    p = conn.execute("SELECT * FROM pools WHERE id=?", (pool_id,)).fetchone()
    return f"{p['token0_symbol']}/{p['token1_symbol']}", p


def card(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime) -> dict[str, Any]:
    """建玉カード（SPEC 7.4章）: レンジ、端までの距離、評価額、入れた額との差、1日の報酬、予測と実績の日利。"""
    pair, pool = _pair(conn, pos["pool_id"])
    rows = _pnl_rows(conn, pos["id"])
    tot = views.signed_breakdown(_sum(rows))
    st = json.loads(pos["state_json"] or "{}")
    price = (st.get("prev") or {}).get("price") or pos["price_open"]
    in_range = pos["lower"] <= price <= pos["upper"]
    last = rows[-1]["ts"] if rows else pos["opened_at"]
    end = datetime.fromisoformat(pos["closed_at"]) if pos["closed_at"] else datetime.fromisoformat(last)
    days = max((end - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 86400, 1e-9)
    pred = json.loads(pos["predicted_json"] or "{}")
    since24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    reward_24h = sum(float(r["income"] or 0) for r in rows if r["ts"] >= since24)
    x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"],
                      int(pool["token0_decimals"]), int(pool["token1_decimals"]))
    open_cost = -float(rows[0]["other"] or 0.0) if rows else 0.0
    running = sum(tot[k] for k in CATS) + open_cost          # 1日あたりの比較は、開く時の費用を除いて行う
    return {
        "id": pos["id"], "pool_id": pos["pool_id"], "pair": pair, "venue_id": pos["venue_id"],
        "status": pos["status"], "opened_at": pos["opened_at"], "closed_at": pos["closed_at"],
        "close_reason": pos["close_reason"], "last_ts": last,
        "capital": pos["capital"], "c_lp": pos["c_lp"], "mode": pos["mode"],
        "r_pct": pos["r"] * 100, "lower": pos["lower"], "upper": pos["upper"], "price": price,
        "price_open": pos["price_open"], "in_range": in_range,
        "to_lower_pct": (price / pos["lower"] - 1) * 100, "to_upper_pct": (pos["upper"] / price - 1) * 100,
        "amounts": {pool["token0_symbol"]: x, pool["token1_symbol"]: y},
        "value": pos["capital"] + tot["net"], "change_usd": tot["net"],
        "change_pct": tot["net"] / pos["capital"] * 100 if pos["capital"] else 0.0,
        "reward_24h_usd": reward_24h,
        "predicted_daily_pct": (pred["net"] / pred["c_total"] * 100
                                if pred.get("net") is not None and pred.get("c_total") else None),
        "actual_daily_pct": running / days / pos["capital"] * 100 if pos["capital"] else None,
        "days": days, "open_cost_usd": open_cost,
        "started_red": bool(pos["started_red"]), "signal_open": pos["signal_open"],
        "red_label": RED_START_LABEL if pos["started_red"] else None,
        "hedges": json.loads(pos["hedges_json"] or "[]"),
        "estimated_rows": sum(1 for r in rows if r["is_estimated"]),
    }


def detail(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime, limit_ledger: int = 60) -> dict[str, Any]:
    """建玉の詳細: 6区分（累計・今日・1日平均）、実現/未実現、着地見込み、1時間ごとの棒グラフ、予測との差、台帳。"""
    c = card(conn, pos, now)
    rows = _pnl_rows(conn, pos["id"])
    tot = views.signed_breakdown(_sum(rows))
    closed = pos["status"] != "open"
    realized = tot["income"] + tot["other"] if not closed else tot["net"]
    unrealized = 0.0 if closed else tot["direction"] + tot["gamma"] + tot["hedge"] + tot["haircut"]

    today = now.astimezone(views.JST).strftime("%Y-%m-%d")
    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(views.jst_day(r["ts"]), []).append(r)
    days = {d: views.signed_breakdown(_sum(v)) for d, v in by_day.items()}
    today_b = days.get(today)
    landing = None
    if today_b and not closed:
        day_start = now.astimezone(views.JST).replace(hour=0, minute=0, second=0, microsecond=0)
        start = max(day_start, datetime.fromisoformat(pos["opened_at"]).astimezone(views.JST))
        elapsed_h = (now - start).total_seconds() / 3600
        remaining_h = (day_start + timedelta(days=1) - now).total_seconds() / 3600
        if elapsed_h > 0.5:
            landing = today_b["net"] + today_b["net"] / elapsed_h * remaining_h
    avg = views.average_breakdown(list(days.values()))
    if avg:
        avg["days"] = len(days)

    pred = json.loads(pos["predicted_json"] or "{}")
    d = c["days"]
    run = {k: tot[k] for k in CATS}
    run["other"] += c["open_cost_usd"]
    compare = None
    if pred.get("net") is not None:
        pred_parts = {"income": pred.get("income") or 0.0, "direction": -(pred.get("direction_risk") or 0.0),
                      "gamma": -(pred.get("gamma") or 0.0), "hedge": -(pred.get("hedge") or 0.0),
                      "haircut": -(pred.get("haircut") or 0.0), "other": -(pred.get("rebalance") or 0.0)}
        compare = {
            "rows": [{"key": k, "label": views.CATEGORY_JA[k], "predicted": pred_parts[k], "actual": run[k] / d}
                     for k in CATS],
            "predicted_net": pred["net"], "actual_net": sum(run.values()) / d,
            "score_ts": pred.get("score_ts"), "short": d < 1,
            "note": "予測はスコア（始めた時の最適レンジの1日の見込み）。実績は始めてからの1日あたり（開く時の費用を除く）。",
        }
    sell = {
        "hours": None, "hold_haircut": tot["haircut"],
        "sell_haircut": sum(float(r["haircut_sell"] or 0.0) for r in rows),
        "predicted_hold_pct": c["predicted_daily_pct"],
        "predicted_sell_pct": (pred["net_sell_now"] / pred["c_total"] * 100
                               if pred.get("net_sell_now") is not None and pred.get("c_total") else None),
    }
    sell["hold_net"] = tot["net"]
    sell["sell_net"] = tot["net"] - tot["haircut"] + sell["sell_haircut"]
    ledger = [dict(r) for r in conn.execute(
        "SELECT ts, kind, token, amount, price_usd, value_usd, price_jpy, fx_rate, fx_date, note FROM ledger "
        "WHERE position_id=? ORDER BY ts DESC, id DESC LIMIT ?", (pos["id"], limit_ledger))]
    return {
        **c, "total": tot, "realized": realized, "unrealized": unrealized,
        "today": today_b, "landing": landing, "since_start": avg,
        "hourly": views.hourly_sum_bars(rows, now),
        "series": [{"ts": r["ts"], "jst": datetime.fromisoformat(r["ts"]).astimezone(views.JST).strftime("%m/%d %H時"),
                    "value": r["value_usd"]} for r in rows if r["value_usd"] is not None],
        "compare": compare, "sell_now": sell, "ledger": ledger,
        "labels": views.CATEGORY_JA, "apy_note": views.APY_NOTE,
        "apy_display": views.apy(tot["income"] / d, pos["capital"]),
        "apy_net": views.apy(sum(run.values()) / d, pos["capital"]),
        "red_note": RED_START_NOTE if pos["started_red"] else None,
        "notes": ESTIMATE_NOTES,
    }
