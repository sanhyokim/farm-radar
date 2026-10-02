"""練習（ペーパートレード）の画面用のまとめ（SPEC 7.4章・7.6章・7.7章。M5a）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .. import views
from ..config import RiskSettings
from ..risk.rules import LEVEL_JA
from .paper import CATS, latest_score, lp_amounts

OPTION_JA = {"stay": "そのまま", "fees": "ステークをやめて手数料", "exit": "抜ける"}
RED_START_LABEL = "🔴で開始した練習"
RED_START_NOTE = ("判定が🔴のプールで始めた練習です。「判定が🔴になったら離脱」のルールは当てはめません"
                  "（ほかの離脱・緊急離脱のルールは当てはめます）。")
ACTION_JA = {"none": "記録のみ", "rebalanced": "置き直した", "closed": "閉じた", "closed_all": "全部閉じた",
             "closed_pool": "この建玉だけ閉じた",
             "skipped_gas": "ガス代が高く見送り", "stopped": "停止", "resumed": "再開"}
CAUTION_JA = {"edge_near": "レンジの端が近い", "reward_shortfall": "報酬が予測より少ない",
              "hedge_cost": "ヘッジの費用が大きい", "bonus_drop": "切り替えでボーナスが減った（記録だけ）",
              "active_liquidity_drop": "今の値段のところの流動性が急に減った（表示だけ）"}
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


def card(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime, risk: RiskSettings | None = None
         ) -> dict[str, Any]:
    """建玉カード（SPEC 7.4章）: レンジ、端までの距離、評価額、入れた額との差、報酬、予測と実績の日利。"""
    risk = risk or RiskSettings()
    pair, pool = _pair(conn, pos["pool_id"])
    rows = _pnl_rows(conn, pos["id"])
    tot = views.signed_breakdown(_sum(rows))
    st = json.loads(pos["state_json"] or "{}")
    price = (st.get("prev") or {}).get("price") or pos["price_open"]
    in_range = pos["lower"] <= price <= pos["upper"]
    last = rows[-1]["ts"] if rows else pos["opened_at"]
    end = datetime.fromisoformat(pos["closed_at"]) if pos["closed_at"] else datetime.fromisoformat(last)
    days = max((end - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 86400, 1e-9)
    hours = days * 24
    pred = json.loads(pos["predicted_json"] or "{}")
    since24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    reward_24h = sum(float(r["income"] or 0) for r in rows if r["ts"] >= since24)
    x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"],
                      int(pool["token0_decimals"]), int(pool["token1_decimals"]))
    open_cost = -float(rows[0]["other"] or 0.0) if rows else 0.0
    net = float(tot["net"])
    running = net + open_cost          # 1日あたりの比較は、開く時の費用を除いて行う
    cap = pos["capital"] or 0.0
    # 始めた費用を取り返すまでの見込み（費用を除いた稼ぎのペースで、費用込みの損がゼロに戻るまで）
    per_hour = running / hours if hours > 0 else 0.0
    payback_total = open_cost / per_hour if per_hour > 0 and open_cost > 0 else None
    payback_left = (max(0.0, -net) / per_hour if per_hour > 0 else None) if open_cost > 0 else None
    actual_state = "short" if hours < risk.actual_min_hours else "ok"
    # 今日（日本時間の0時から）の損益と、評価額の小さな線グラフ（2026-09-30 オーナー依頼 18・27）
    today = now.astimezone(views.JST).strftime("%Y-%m-%d")
    today_usd = sum(float(r[c] or 0.0) for r in rows if views.jst_day(r["ts"]) == today for c in CATS)
    return {
        "id": pos["id"], "pool_id": pos["pool_id"], "pair": pair, "venue_id": pos["venue_id"],
        # 参考の練習（合否に使わない。2026-10-01 案B）
        "reference": (pos["purpose"] if "purpose" in pos.keys() else None) == "reference",
        "status": pos["status"], "opened_at": pos["opened_at"], "closed_at": pos["closed_at"],
        "close_reason": pos["close_reason"], "close_reason_ja": close_reason_ja(pos["close_reason"]),
        "last_ts": last,
        "capital": pos["capital"], "c_lp": pos["c_lp"], "mode": pos["mode"],
        "r_pct": pos["r"] * 100, "lower": pos["lower"], "upper": pos["upper"], "price": price,
        "price_open": pos["price_open"], "in_range": in_range,
        "to_lower_pct": (price / pos["lower"] - 1) * 100, "to_upper_pct": (pos["upper"] / price - 1) * 100,
        "amounts": {pool["token0_symbol"]: x, pool["token1_symbol"]: y},
        "value": cap + net, "change_usd": net, "today_usd": today_usd,
        "spark": views.thin([float(r["value_usd"]) for r in rows if r["value_usd"] is not None]),
        "change_pct": net / cap * 100 if cap else 0.0,
        "reward_24h_usd": reward_24h, "reward_hours": min(24.0, (now - datetime.fromisoformat(pos["opened_at"])
                                                                 ).total_seconds() / 3600),
        "predicted_daily_pct": (pred["net"] / pred["c_total"] * 100
                                if pred.get("net") is not None and pred.get("c_total") else None),
        "actual_daily_pct": running / days / cap * 100 if cap else None,
        "actual_daily_pct_with_cost": net / days / cap * 100 if cap else None,
        "actual_state": actual_state, "actual_min_hours": risk.actual_min_hours,
        "compare_enabled": hours >= risk.compare_min_hours, "compare_min_hours": risk.compare_min_hours,
        "payback_total_hours": payback_total, "payback_left_hours": payback_left,
        "days": days, "hours": hours, "open_cost_usd": open_cost,
        "close_cost_usd": st.get("close_cost"),
        "started_red": bool(pos["started_red"]), "signal_open": pos["signal_open"],
        "red_label": RED_START_LABEL if pos["started_red"] else None,
        "hedges": json.loads(pos["hedges_json"] or "[]"),
        "estimated_rows": sum(1 for r in rows if r["is_estimated"]),
        "rebalances": int(st.get("rebalances", 0)),
        "rebalance_cost": round(float(st.get("rebalance_cost", 0.0)), 2),
        "cautions": [CAUTION_JA.get(k, k) for k in st.get("risk_active") or [] if not k.startswith("skip:")],
        "skipped": [k[5:] for k in st.get("risk_active") or [] if k.startswith("skip:")],
        "swap": _swap_info(conn, pos, st, x, y),
        # ボーナスが減ったときの比べ方の最新の結果（2026-09-30 オーナー決定③。記録だけ）
        "bonus_drop": ({**st["bonus_drop"], "best_ja": OPTION_JA.get(st["bonus_drop"].get("best"))}
                       if st.get("bonus_drop") else None),
    }


def _swap_info(conn: sqlite3.Connection, pos: sqlite3.Row, st: dict[str, Any], x: float, y: float) -> dict[str, Any]:
    """両替のずれ（%）と、始めた費用・置き直しの費用に含まれた額（2026-09-29 オーナー追加）。

    始めた時の内訳は開いた時に記録したもの（M5d より前に始めた建玉は記録がないので、今のプールで見積もる）。
    置き直し1回の見込みは、今のプールのずれで、今の LP の額を両替した場合。
    """
    sc = latest_score(conn, pos["pool_id"])
    inp = (json.loads(sc["details_json"] or "{}").get("inputs") or {}) if sc else {}
    slip_now = inp.get("slippage")
    ob = st.get("open_breakdown")
    c_lp = float(pos["c_lp"] or 0.0)
    ratio = 0.5
    if ob and ob.get("swap_usd") and c_lp:
        ratio = ob["swap_usd"] / c_lp
    est_open = None
    if not ob and slip_now is not None:
        est_open = {"swap_usd": c_lp * ratio, "slippage_pct": float(slip_now) * 100,
                    "slippage": c_lp * ratio * float(slip_now), "estimated": True}
    prev = st.get("prev") or {}
    return {
        "slippage_pct_now": float(slip_now) * 100 if slip_now is not None else None,
        "open": ob or est_open,
        "rebalance_slippage_total": round(float(st.get("rebalance_slippage", 0.0)), 4),
        "rebalance_slippage_next": (c_lp * ratio * float(slip_now) if slip_now is not None else None),
        "price": prev.get("price"),
    }


def close_reason_ja(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason == "manual":
        return "オーナーが閉じた"
    if reason == "owner_exit_all":
        return "オーナーが全部閉じた"
    if reason.startswith("emergency:"):
        detail = {"pool_funds_drop": "プールのお金が減った", "liquidity_drop": "前の決まり: レンジ内の流動性",
                  "contract_change": "会場プログラムの変化", "usdg_depeg": "USDG の値段", "daily_loss": "今日の損"
                  }.get(reason.partition(":")[2])
        return f"緊急離脱・{detail}" if detail else "緊急離脱"
    if reason.startswith("risk:"):
        return "離脱のルール"
    return reason


def events(conn: sqlite3.Connection, pid: int | None, limit: int = 50) -> list[dict[str, Any]]:
    """見張りの記録（新しい順）。pid を指定すればその建玉の分と全体の分、None なら全部。"""
    q = "SELECT * FROM risk_events"
    args: list[Any] = []
    if pid is not None:
        q += " WHERE position_id=? OR position_id IS NULL"
        args.append(pid)
    rows = conn.execute(q + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "position_id": r["position_id"], "level": r["level"],
             "level_ja": LEVEL_JA.get(r["level"], r["level"]), "kind": r["kind"], "message": r["message_ja"],
             "action": r["action"], "action_ja": ACTION_JA.get(r["action"] or "none", r["action"])} for r in rows]


def risk_rules(r: RiskSettings) -> list[dict[str, str]]:
    """画面の「見張りのルール」の一覧（config.yaml の risk の今の値）。"""
    return [
        {"level": "caution", "level_ja": "注意", "rule": f"レンジの端まで{r.caution_edge_pct:g}%未満 / "
                                                       f"報酬の実績が予測より{r.caution_reward_shortfall_pct:g}%以上少ない / "
                                                       f"ヘッジの1日の費用が報酬の{r.caution_hedge_cost_pct:g}%超"
                                                       f"（報酬の比べっこは{r.caution_min_hours:g}時間たってから） / "
                                                       f"今の値段のところの流動性が1時間で−{r.caution_active_liquidity_drop_1h_pct:g}%"
                                                       "（ボーナスの取り分の見込みが変わる）",
         "action": "記録する"},
        {"level": "caution", "level_ja": "注意（記録だけ）",
         "rule": f"木曜の切り替えでボーナスが前の週の{r.bonus_drop_ratio * 100:g}%以下になった"
                 f"（0のままなら切り替えから{r.bonus_drop_wait_hours:g}時間待ってから調べる）",
         "action": "次の切り替えまでの見込みで「そのまま／ステークをやめて手数料／抜ける」を比べて、"
                   "いちばん損が少ないものを記録して知らせる。建玉はそのまま"},
        {"level": "rebalance", "level_ja": "置き直し", "rule": f"レンジの外に{r.rebalance_after_minutes:g}分いた",
         "action": f"今の価格を中心に置き直す（置き直し先の純日利が{r.rebalance_min_net_pct:+g}%以下なら閉じる）"},
        {"level": "exit", "level_ja": "離脱", "rule": f"報酬トークンが24時間で{r.exit_reward_token_24h_pct:g}% / "
                                                    f"値動きする側のトークンが1時間で{r.exit_dump_1h_pct:g}%か"
                                                    f"24時間で{r.exit_dump_24h_pct:g}%（投げ売り）/ "
                                                    "プールの判定が🔴になった（🔴で始めた練習は除く）", "action": "その建玉を閉じる"},
        {"level": "emergency", "level_ja": "緊急離脱（そのプール）",
         "rule": f"プールのお金（持っているコインの量。1時間前も今の値段で数える）が1時間で−{r.emergency_pool_funds_drop_1h_pct:g}%",
         "action": "その建玉だけ閉じる。ほかの建玉と評価は続く（評価の建玉が全部閉じたら評価は中断）"},
        {"level": "emergency", "level_ja": "緊急離脱（全体）",
         "rule": "会場プログラムの停止・持ち主の変更・入れ替え / "
                 f"USDG の外部の価格が{r.emergency_usdg_times}回続けて ${r.emergency_usdg_below:g} 未満 / "
                 f"今日の損が総資産の{r.emergency_daily_loss_pct:g}%",
         "action": "全部閉じて、新しく始めるのを止める"},
    ]


def watch(conn: sqlite3.Connection) -> dict[str, Any]:
    """画面の「会場プログラムと USDG の見張り」: 読めた項目・未確認の項目と、USDG の外部の価格。"""
    from . import contract_watch
    row = conn.execute("SELECT ts, price, source FROM stable_prices ORDER BY ts DESC LIMIT 1").fetchone()
    return {"contracts": contract_watch.status(conn),
            "usdg": dict(row) if row else None}


def detail(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime, risk: RiskSettings | None = None,
           limit_ledger: int = 60) -> dict[str, Any]:
    """建玉の詳細: 6区分（累計・今日・1日平均）、実現/未実現、着地見込み、1時間ごとの棒グラフ、予測との差、台帳。"""
    c = card(conn, pos, now, risk)
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
            "score_ts": pred.get("score_ts"), "short": not c["compare_enabled"],
            "enabled": c["compare_enabled"], "min_hours": c["compare_min_hours"],
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
        "events": events(conn, pos["id"]),
        "notes": ESTIMATE_NOTES,
    }
