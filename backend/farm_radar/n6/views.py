"""N6 の画面に出す形（練習の画面・建玉の詳しい画面）。読むだけ。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..config import Config
from . import report, sim
from .engine import LEVEL_JA, PICKER_JA, REASONS_JA

RULE_JA = {
    "liquidation": "段階1: 保険の強制決済", "own_value_drop": "段階1: 建玉の値打ちが1時間で大きく減った",
    "pool_funds_drop": "段階1: プールのお金が1時間で大きく減った", "vault_changed": "段階1: 金庫の運用先が変わった",
    "reward_drop": "段階2: ボーナスのコインが24時間で下がった", "dump": "段階2: 投げ売り",
    "below_target": "段階2: 狙い利回りを続けて下回った", "campaign_end": "段階3: キャンペーンが終わった",
    "gone": "段階3: 入れる先が一覧から消えた", "end_near": "段階3: 終わりが近い（残りより出る費用が大きい）",
    "bonus_drop": "段階3: ボーナスが半分以下になった", "better_place": "段階4: もっと良い場所",
    "manual": "手動で出た", "main_closed": "本体と一緒に閉じた", "loss_day": "損の線（今日）",
    "loss_week": "損の線（7日）", "loss_since_start": "損の線（始めてから）", "loss_day_old": "（影）前の1日の線",
    "edge": "置き直し（境目から幅の15%外に30分）", "hedge_above": "保険: 幅の上に出たので閉じた",
    "hedge_below": "保険: 幅の下に出たので増やした", "hedge_inside": "保険: 幅に戻ったので合わせた",
    "hedge_match": "保険: 中身に合わせた", "margin_half": "保険の余裕が半分を切った（足したとしたら）",
    "pick": "アプリが選んで入った", "move": "移ってきた",
}
ACTION_JA = {"caution": "注意", "no_new": "新しく入らない", "stop": "すべて止める", "rebalance": "置き直した",
             "hedge": "保険を直した", "move": "移った", "exit": "出た", "enter": "入った", "liquidation": "強制決済",
             "topup": "足したとしたら", "start": "始めた", "resume": "再開した", "info": "お知らせ"}


def _j(s: str | None) -> Any:
    try:
        return json.loads(s or "null")
    except (TypeError, ValueError):
        return None


def _days(a: str, b: str | None) -> float:
    t0 = datetime.fromisoformat(a)
    t1 = datetime.fromisoformat(b) if b else datetime.now(UTC)
    return max((t1 - t0).total_seconds() / 86400, 1e-9)


def position_row(r: sqlite3.Row, with_state: bool = False) -> dict[str, Any]:
    st = _j(r["state_json"]) or {}
    amount = float(r["amount_usd"])
    pnl = r["pnl_usd"] or 0.0
    days = _days(r["opened_at"], r["closed_at"] or st.get("last_ts"))
    h = st.get("hedge") or {}
    out = {k: r[k] for k in ("id", "portfolio_id", "twin_of", "opp_key", "name", "pair", "chain", "venue", "kind",
                             "amount_usd", "opened_at", "closed_at", "status", "est_apr_pct", "est_old_apr_pct",
                             "pick_reason", "exit_reason", "exit_rule", "exit_stage", "move_from", "move_to", "move_reason",
                             "value_usd", "pnl_usd", "bonus_usd", "fees_usd", "price_move_usd", "rebalance_cost_usd",
                             "hedge_usd", "hedge_cost_usd", "funding_usd", "gas_usd", "entry_exit_cost_usd",
                             "max_drawdown_usd", "rebalances")}
    out.update(hedge=bool(r["hedge"]), exit_rule_ja=RULE_JA.get(r["exit_rule"] or "", r["exit_rule"]),
               days=days, pnl_pct=pnl / amount * 100 if amount else None,
               actual_apr_pct=pnl / amount / days * 365 * 100 if amount and days >= 1 / 24 else None,
               in_range_pct=(st["in_range_s"] / st["total_s"] * 100) if st.get("total_s") else None,
               range_pct=(st.get("r") or 0) * 100 if st.get("r") else None, geo=st.get("geo"),
               price_src=st.get("price_src"), valued_at=st.get("last_ts"),
               margin={"start": h.get("margin0"), "equity": sim.hedge_equity(st) if h else None,
                       "maintenance": sim.maintenance(st) if h else None, "liquidated": h.get("liquidated"),
                       "topups": len(h.get("topups") or []),
                       "topup_usd": sum(t.get("add_usd") or 0 for t in h.get("topups") or [])} if h.get("legs") or h.get("margin0") else None)
    if st.get("bonus"):
        out["bonus_hold"] = sim.hold_difference(st)
    if with_state:
        out["state"] = {"tokens": st.get("tokens"), "u": st.get("u"), "P": st.get("P"), "lower": st.get("lower"),
                        "upper": st.get("upper"), "legs": [{"sym": lg["sym"], "size": lg["size"],
                                                            "market": lg["market"].get("symbol"),
                                                            "book": lg["market"].get("book")} for lg in h.get("legs") or []],
                        "topups": h.get("topups"), "est": st.get("est"), "old": st.get("old"), "costs": st.get("costs"),
                        "gap_hours": (st.get("gap_s") or 0) / 3600, "denoms": st.get("denoms")}
    return out


def _twin_compare(conn: sqlite3.Connection, main: dict[str, Any]) -> dict[str, Any] | None:
    tw = conn.execute("SELECT * FROM n6_positions WHERE twin_of=?", (main["id"],)).fetchone()
    if tw is None:
        return None
    t = position_row(tw)
    keys = ("pnl_usd", "pnl_pct", "price_move_usd", "hedge_usd", "hedge_cost_usd", "funding_usd", "bonus_usd",
            "max_drawdown_usd", "rebalances", "status", "closed_at", "exit_reason", "exit_rule_ja")
    with_h, without = (main, t) if main["hedge"] else (t, main)
    return {"main_id": main["id"], "name": main["name"], "amount_usd": main["amount_usd"], "status": main["status"],
            "opened_at": main["opened_at"], "closed_at": main["closed_at"], "exit_reason": main["exit_reason"],
            "hedged": {k: with_h.get(k) for k in keys} | {"margin": with_h.get("margin"), "id": with_h["id"]},
            "unhedged": {k: without.get(k) for k in keys} | {"id": without["id"]}}


def _shadow(conn: sqlite3.Connection, pid: str | None = None) -> dict[str, Any]:
    """新しい決まりと前の決まり（影）の比べ（指示書 23。最初の日から記録）。"""
    q = "SELECT * FROM n6_positions WHERE twin_of IS NULL AND kind='pool_range'" + (" AND portfolio_id=?" if pid else "")
    rows = conn.execute(q, (pid,) if pid else ()).fetchall()
    new_n = old_n = 0
    new_cost = old_cost = 0.0
    est_new = est_old = 0.0
    days = 0.0
    in_new = in_old = tot = 0.0
    bonus_split = bonus_a = 0.0
    for r in rows:
        st = _j(r["state_json"]) or {}
        old = st.get("old") or {}
        d = (st.get("total_s") or 0) / 86400
        days += d
        new_n += (st.get("reb") or {}).get("count") or 0
        old_n += old.get("count") or 0
        c = st.get("costs") or {}
        new_cost += (c.get("reb_swap") or 0) + (c.get("reb_gas") or 0)
        old_cost += old.get("cost") or 0
        est_new += ((st.get("reb") or {}).get("est_per_day") or 0) * d
        est_old += (old.get("est_per_day") or 0) * d
        tot += st.get("total_s") or 0
        in_new += st.get("in_range_s") or 0
        in_old += old.get("in_range_s") or 0
        b = st.get("bonus") or {}
        bonus_split += b.get("acc_usd") or 0
        bonus_a += b.get("acc_usd_a") or 0
    ev = conn.execute("SELECT shadow, rule, COUNT(*) FROM n6_events WHERE rule LIKE 'loss_day%'" +
                      (" AND portfolio_id=?" if pid else "") + " GROUP BY shadow, rule", (pid,) if pid else ()).fetchall()
    return {"positions": len(rows), "position_days": days,
            "rebalances": {"new": new_n, "old": old_n, "est_new": est_new, "est_old": est_old},
            "rebalance_cost": {"new": new_cost, "old": old_cost},
            "in_range_pct": {"new": in_new / tot * 100 if tot else None, "old": in_old / tot * 100 if tot else None},
            "bonus": {"split": bonus_split, "a": bonus_a},
            "loss_day_events": {"new": sum(x[2] for x in ev if not x[0]), "old": sum(x[2] for x in ev if x[0])}}


def overview(conn: sqlite3.Connection, config: Config) -> dict[str, Any]:
    tick = conn.execute("SELECT ts, ok, detail_json FROM n6_ticks ORDER BY ts DESC LIMIT 1").fetchone()
    pfs = []
    for pf in conn.execute("SELECT * FROM n6_portfolios ORDER BY picker, total_usd").fetchall():
        pid = pf["id"]
        rows = [position_row(r) for r in conn.execute(
            "SELECT * FROM n6_positions WHERE portfolio_id=? AND twin_of IS NULL ORDER BY status='closed', opened_at DESC",
            (pid,))]
        mark = conn.execute("SELECT * FROM n6_portfolio_marks WHERE portfolio_id=? ORDER BY ts DESC LIMIT 1", (pid,)).fetchone()
        md = _j(mark["detail_json"]) if mark else {}
        eqs = [x[0] for x in conn.execute("SELECT equity_usd FROM n6_portfolio_marks WHERE portfolio_id=? ORDER BY ts", (pid,))]
        peak, dd = float(pf["total_usd"]), 0.0
        for e in eqs:
            peak = max(peak, e)
            dd = max(dd, peak - e)
        rules = conn.execute("SELECT action, rule, COUNT(*) n FROM n6_events WHERE portfolio_id=? AND shadow=0 AND "
                             "action IN ('caution','no_new','stop','rebalance','hedge','exit','liquidation','topup') "
                             "GROUP BY action, rule ORDER BY n DESC", (pid,)).fetchall()
        open_rows = [x for x in rows if x["status"] == "open"]
        equity = float(pf["cash_usd"]) + sum(x["value_usd"] or x["amount_usd"] for x in open_rows)
        pfs.append({
            "id": pid, "picker": pf["picker"], "picker_ja": PICKER_JA[pf["picker"]], "total_usd": pf["total_usd"],
            "status": pf["status"], "stopped_reason": pf["stopped_reason"], "started_at": pf["started_at"],
            "cash_usd": pf["cash_usd"], "placed_usd": sum(x["amount_usd"] for x in open_rows), "equity_usd": equity,
            "pnl_usd": equity - float(pf["total_usd"]), "pnl_pct": (equity / float(pf["total_usd"]) - 1) * 100,
            "max_drawdown_usd": dd, "loss": (md or {}).get("loss"), "waiting": (md or {}).get("waiting"),
            "open": open_rows, "closed": [x for x in rows if x["status"] == "closed"][:30],
            "rules_fired": [{"action": r["action"], "action_ja": ACTION_JA.get(r["action"], r["action"]),
                             "rule": r["rule"], "rule_ja": RULE_JA.get(r["rule"] or "", r["rule"]), "count": r["n"]}
                            for r in rules],
            "twins": [c for c in (_twin_compare(conn, x) for x in rows) if c],
            "funnel": report.funnel_view(md), "last_mark_at": mark["ts"] if mark else None,
        })
    by = {p["id"]: p for p in pfs}
    app_vs_own = []
    for size in config.n6.owner_portfolios:
        own, app = by.get(f"own_{size:g}"), by.get(f"app_{size:g}")
        if own is None or app is None:
            continue
        r = conn.execute("SELECT equity_usd FROM n6_portfolio_marks WHERE portfolio_id=? AND ts >= ? ORDER BY ts LIMIT 1",
                         (app["id"], own["started_at"])).fetchone()
        app0 = float(r[0]) if r else app["equity_usd"]
        app_vs_own.append({"size": size, "since": own["started_at"], "own_pnl_usd": own["pnl_usd"],
                           "app_pnl_usd": app["equity_usd"] - app0,
                           "own_pnl_pct": own["pnl_usd"] / size * 100, "app_pnl_pct": (app["equity_usd"] - app0) / size * 100})
    events = [dict(r) | {"action_ja": ACTION_JA.get(r["action"], r["action"]), "rule_ja": RULE_JA.get(r["rule"] or "", r["rule"])}
              for r in conn.execute("SELECT id, ts, portfolio_id, position_id, action, rule, stage, shadow, message_ja "
                                    "FROM n6_events ORDER BY id DESC LIMIT 60")]
    reqs = [dict(r) for r in conn.execute("SELECT * FROM n6_requests ORDER BY id DESC LIMIT 20")]
    sales = conn.execute("SELECT COUNT(*), COALESCE(SUM(usd),0), COALESCE(SUM(cost_usd),0) FROM n6_bonus_sales").fetchone()
    n6 = config.n6
    return {
        "enabled": n6.enabled, "virtual": True,
        "virtual_ja": "100% 仮想のお金です。本物の送金・両替・LP・ボーナスの受け取り・Lighter の建玉・署名・秘密鍵は使いません。",
        "last_tick": {"ts": tick["ts"], "ok": bool(tick["ok"]),
                      "error": (_j(tick["detail_json"]) or {}).get("error")} if tick else None,
        "rules": {"new": {"rebalance_factor": n6.new.rebalance_factor, "edge_buffer_pct": n6.new.edge_buffer_frac * 100,
                          "edge_wait_minutes": n6.new.edge_wait_minutes, "loss_day": n6.new.loss_day,
                          "merkl_denominator": n6.new.merkl_denominator},
                  "old": {"rebalance_factor": n6.old.rebalance_factor, "edge_buffer_pct": n6.old.edge_buffer_frac * 100,
                          "edge_wait_minutes": n6.old.edge_wait_minutes, "loss_day": n6.old.loss_day,
                          "merkl_denominator": n6.old.merkl_denominator},
                  "loss_week": config.guard.loss_lines["week"], "loss_since_start": config.guard.loss_lines["since_start"],
                  "amounts_usd": list(n6.amounts_usd), "per_venue_share": n6.per_venue_share,
                  "max_danger": n6.max_danger, "bonus_sale_hour_jst": n6.bonus_sale_hour_jst},
        "level_ja": LEVEL_JA, "reasons_ja": REASONS_JA,
        "portfolios": pfs, "app_vs_own": app_vs_own, "shadow": _shadow(conn), "events": events, "requests": reqs,
        "bonus_sales": {"count": sales[0], "usd": sales[1], "cost_usd": sales[2]},
        "owner_sizes": list(n6.owner_portfolios),
        "ticks": conn.execute("SELECT COUNT(*) FROM n6_ticks").fetchone()[0],
        "first_entries": report.first_entries(conn, config),
        "cap_diff": report.cap_diff(conn, limit_ticks=96 * 7),
        "health": report.health(conn, config),
    }


def position_detail(conn: sqlite3.Connection, pos_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM n6_positions WHERE id=?", (pos_id,)).fetchone()
    if r is None:
        return None
    out = position_row(r, with_state=True)
    out["entry"] = _j(r["entry_json"])
    marks = conn.execute("SELECT ts, value_usd, pnl_usd, in_range, price FROM n6_marks WHERE position_id=? ORDER BY ts",
                         (pos_id,)).fetchall()
    step = max(1, len(marks) // 200)
    out["marks"] = [dict(m) for m in marks[::step]]
    if marks and (len(marks) - 1) % step:
        out["marks"].append(dict(marks[-1]))
    out["events"] = [dict(e) | {"action_ja": ACTION_JA.get(e["action"], e["action"]),
                                "rule_ja": RULE_JA.get(e["rule"] or "", e["rule"])}
                     for e in conn.execute("SELECT id, ts, action, rule, stage, shadow, message_ja FROM n6_events "
                                           "WHERE position_id=? ORDER BY id DESC LIMIT 100", (pos_id,))]
    out["sales"] = [dict(s) for s in conn.execute("SELECT * FROM n6_bonus_sales WHERE position_id=? ORDER BY day", (pos_id,))]
    ex = conn.execute("SELECT * FROM n6_exits WHERE position_id=?", (pos_id,)).fetchone()
    out["manual_exit"] = ({"ts": ex["ts"], "reason": ex["reason"], "reason_ja": REASONS_JA.get(ex["reason"]),
                           "note": ex["note"], "pnl_usd": ex["pnl_usd"], "estimate": _j(ex["estimate_json"])} if ex else None)
    out["twin"] = _twin_compare(conn, out) if r["twin_of"] is None else None
    out["reasons_ja"] = REASONS_JA
    return out
