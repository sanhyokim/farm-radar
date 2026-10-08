"""N6 の評価の準備（2026-10-08 指示書「N6正式開始後の監視・評価準備」）。読むだけ。入る・出るの判断は変えない。

- 初めて入った瞬間（練習のまとまりごと）
- 15分ごとの回の「なぜ入らなかったか」の内訳と、いちばん惜しかった候補（engine.funnel が記録したもの）
- $1,000 と $10,000 で、同じ入れる先の扱いが分かれたとき（どの上限で差が出たか）
- 1日ごとのまとめ（日本時間の日付）
- 異常のチェック
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .. import opportunities as opps
from .. import realmoney_scan, riskscore
from ..config import Config
from .engine import DROP_JA, PICKER_JA, REASONS_JA

JST = timezone(timedelta(hours=9))
STALE_MINUTES = 30            # これより長く N6 の回がなければ異常
EPS = 0.01


def _j(s: str | None) -> Any:
    try:
        return json.loads(s or "null")
    except (TypeError, ValueError):
        return None


def _t(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _day(s: str) -> str:
    return _t(s).astimezone(JST).date().isoformat()


def size_ja(total: float) -> str:
    return f"${total:,.0f}"


# --- 内訳 -------------------------------------------------------------------------------------------

def wait_reason(md: dict[str, Any] | None) -> str | None:
    """1回の「入らなかった理由」を一言で（その回に入らなかったときだけ。入った回・建玉を持っているだけの回は None）。"""
    md = md or {}
    f = md.get("funnel") or {}
    state = f.get("state")
    if state == "stopped" or md.get("status") == "stopped":
        return "損の線「すべて止める」で止まっている"
    if state == "no_new":
        return "損の線「新しく入らない」に届いている"
    waiting = md.get("waiting")
    if not waiting:
        return None
    if f and "error" not in f and not waiting.startswith("置いていないお金"):
        # もう入っている先は「入れなかった理由」に数えない
        dropped = {c: n for c, n in (f.get("reach_dropped") or {}).items() if c != "held"}
        if not dropped:
            return f"狙い利回り（年{f.get('target', 30):g}%）に届く入れる先がない"
        code = max(dropped, key=lambda c: dropped[c])
        return f"年{f.get('target', 30):g}%以上はあるが「{DROP_JA.get(code, code)}」で入れない"
    return waiting


def funnel_view(md: dict[str, Any] | None) -> dict[str, Any] | None:
    """画面に出す内訳（数と日本語）。"""
    f = (md or {}).get("funnel")
    if not f:
        return None
    if "error" in f:
        return {"error": f["error"]}
    counts = f.get("counts") or {}
    caps = ("cash", "danger_cap", "venue_cap", "high_total", "pool_5pct", "lighter", "no_variant", "below_target_at_amount")
    rd = f.get("reach_dropped") or {}
    return {
        "total": f.get("total"), "usable": f.get("usable"), "reach": f.get("reach"), "final": f.get("final"),
        "target": f.get("target"), "entered": f.get("entered"), "state": f.get("state"),
        "reach_cap_dropped": sum(rd.get(c, 0) for c in caps),
        "reach_danger_dropped": rd.get("danger", 0),
        "reach_dropped": [{"code": c, "ja": DROP_JA.get(c, c), "n": n} for c, n in sorted(rd.items(), key=lambda x: -x[1])],
        "counts": [{"code": c, "ja": DROP_JA.get(c, c), "n": n} for c, n in sorted(counts.items(), key=lambda x: -x[1])],
        "best_miss": f.get("best_miss"), "near": f.get("near") or [],
        "reason": wait_reason(md),
    }


# --- 初めて入った瞬間 ----------------------------------------------------------------------------------

def first_entries(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    out = []
    for pf in conn.execute("SELECT * FROM n6_portfolios ORDER BY picker, total_usd"):
        r = conn.execute("SELECT * FROM n6_positions WHERE portfolio_id=? AND twin_of IS NULL ORDER BY opened_at, id LIMIT 1",
                         (pf["id"],)).fetchone()
        row: dict[str, Any] = {"portfolio_id": pf["id"], "size_ja": size_ja(float(pf["total_usd"])),
                               "picker_ja": PICKER_JA.get(pf["picker"], pf["picker"]), "started_at": pf["started_at"]}
        if r is None:
            out.append(row | {"entered": False})
            continue
        entry = _j(r["entry_json"]) or {}
        lv = entry.get("danger")
        hours = (_t(r["opened_at"]) - _t(pf["started_at"])).total_seconds() / 3600
        out.append(row | {
            "entered": True, "position_id": r["id"], "opened_at": r["opened_at"], "waited_hours": round(hours, 2),
            "name": r["name"], "pair": r["pair"], "venue": r["venue"], "chain": r["chain"],
            "chain_name": opps.chain_name(config, r["chain"]) or r["chain"], "amount_usd": r["amount_usd"],
            "est_apr_pct": r["est_apr_pct"], "danger": lv, "danger_label": riskscore.LABEL.get(lv or "", lv),
            "hedge": bool(r["hedge"]), "pick_reason": r["pick_reason"], "status": r["status"]})
    return out


# --- $1,000 と $10,000 の差 --------------------------------------------------------------------------

def _pair_ids(conn: sqlite3.Connection) -> tuple[str, str] | None:
    ids = sorted((r["id"], r["total_usd"]) for r in conn.execute(
        "SELECT id, total_usd FROM n6_portfolios WHERE picker='app'"))
    ids.sort(key=lambda x: x[1])
    return (ids[0][0], ids[-1][0]) if len(ids) >= 2 else None


def cap_diff(conn: sqlite3.Connection, limit_ticks: int | None = None) -> dict[str, Any] | None:
    """同じ回で、狙いに届く入れる先の扱いが $1,000 と $10,000 で分かれたもの（どの上限で差が出たか）。"""
    ids = _pair_ids(conn)
    if ids is None:
        return None
    small, big = ids
    q = ("SELECT a.ts, a.detail_json AS da, b.detail_json AS db FROM n6_portfolio_marks a JOIN n6_portfolio_marks b "
         "ON a.ts = b.ts AND b.portfolio_id=? WHERE a.portfolio_id=? ORDER BY a.ts DESC")
    rows = conn.execute(q + (f" LIMIT {int(limit_ticks)}" if limit_ticks else ""), (big, small)).fetchall()
    ticks = 0
    tally: dict[tuple[str, str], int] = {}
    latest: list[dict[str, Any]] | None = None
    latest_ts = None
    for r in rows:
        fa, fb = (_j(r["da"]) or {}).get("funnel"), (_j(r["db"]) or {}).get("funnel")
        if not fa or not fb or "error" in fa or "error" in fb:
            continue
        ticks += 1
        ra = {x["key"]: x for x in fa.get("reach_rows") or []}
        rb = {x["key"]: x for x in fb.get("reach_rows") or []}
        diffs = []
        for k in sorted(set(ra) | set(rb)):
            ca, cb = (ra.get(k) or {}).get("code"), (rb.get(k) or {}).get("code")
            if ca == cb:
                continue
            tally[(ca or "-", cb or "-")] = tally.get((ca or "-", cb or "-"), 0) + 1
            x = ra.get(k) or rb.get(k) or {}
            diffs.append({"key": k, "name": x.get("name"), "venue": x.get("venue"), "apr_pct": x.get("apr_pct"),
                          "small": ca, "small_ja": DROP_JA.get(ca or "", ca), "big": cb, "big_ja": DROP_JA.get(cb or "", cb)})
        if latest is None:
            latest, latest_ts = diffs, r["ts"]
    return {"small": small, "big": big, "ticks": ticks, "latest_ts": latest_ts, "latest": latest or [],
            "tally": [{"small": a, "small_ja": DROP_JA.get(a, a), "big": b, "big_ja": DROP_JA.get(b, b), "n": n}
                      for (a, b), n in sorted(tally.items(), key=lambda x: -x[1])]}


# --- 1日ごとのまとめ ---------------------------------------------------------------------------------

def _top(d: dict[str, int], n: int = 3) -> list[dict[str, Any]]:
    return [{"text": k, "n": v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]


def _bump(d: dict[str, int], k: str | None) -> None:
    if k:
        d[k] = d.get(k, 0) + 1


def daily(conn: sqlite3.Connection, config: Config, days: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
    """日本時間の1日ごとの N6 のまとめ（新しい日が先）。建玉がない日も、待った理由を残す。"""
    from .views import RULE_JA

    now = now or datetime.now(UTC)
    first = conn.execute("SELECT MIN(ts) FROM n6_ticks").fetchone()[0]
    if not first:
        return []
    d0 = max(_t(first).astimezone(JST).date(), (now.astimezone(JST) - timedelta(days=days - 1)).date())
    d1 = now.astimezone(JST).date()
    pfs = conn.execute("SELECT * FROM n6_portfolios ORDER BY picker, total_usd").fetchall()
    positions = conn.execute("SELECT id, portfolio_id, twin_of, hedge, opened_at, closed_at, kind, pnl_usd, status "
                             "FROM n6_positions").fetchall()
    mains = [p for p in positions if p["twin_of"] is None]
    twins = {p["twin_of"]: p for p in positions if p["twin_of"] is not None}
    out = []
    day = d1
    while day >= d0:
        start = datetime(day.year, day.month, day.day, tzinfo=JST).astimezone(UTC)
        end = start + timedelta(days=1)
        s_iso, e_iso = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
        ticks = conn.execute("SELECT ts, ok FROM n6_ticks WHERE ts >= ? AND ts < ? ORDER BY ts", (s_iso, e_iso)).fetchall()
        tick_ts = [t["ts"] for t in ticks]
        row: dict[str, Any] = {"date": day.isoformat(), "ticks": len(ticks), "ticks_ok": sum(1 for t in ticks if t["ok"]),
                               "ticks_failed": sum(1 for t in ticks if not t["ok"]), "portfolios": []}
        wait_all: dict[str, int] = {}
        exit_all: dict[str, int] = {}
        best_day: dict[str, Any] | None = None
        open_total_max = 0
        for ts in tick_ts:
            n_open = sum(1 for p in mains if p["opened_at"] <= ts and (p["closed_at"] is None or p["closed_at"] > ts))
            open_total_max = max(open_total_max, n_open)
        for pf in pfs:
            pid, total = pf["id"], float(pf["total_usd"])
            marks = conn.execute("SELECT ts, equity_usd, detail_json FROM n6_portfolio_marks WHERE portfolio_id=? "
                                 "AND ts >= ? AND ts < ? ORDER BY ts", (pid, s_iso, e_iso)).fetchall()
            if not marks and pf["started_at"] >= e_iso:
                continue
            prev = conn.execute("SELECT equity_usd FROM n6_portfolio_marks WHERE portfolio_id=? AND ts < ? ORDER BY ts DESC "
                                "LIMIT 1", (pid, s_iso)).fetchone()
            eq0 = float(prev[0]) if prev else total
            eq1 = float(marks[-1]["equity_usd"]) if marks else eq0
            peak, dd = eq0, 0.0
            waits: dict[str, int] = {}
            wait_n = 0
            for m in marks:
                peak = max(peak, float(m["equity_usd"]))
                dd = max(dd, peak - float(m["equity_usd"]))
                md = _j(m["detail_json"]) or {}
                why = wait_reason(md)
                if why:
                    wait_n += 1
                    _bump(waits, why)
                    _bump(wait_all, why)
                bm = (md.get("funnel") or {}).get("best_miss")
                if bm and (best_day is None or bm["apr_pct"] > best_day["apr_pct"]):
                    best_day = bm | {"portfolio_id": pid, "ts": m["ts"]}

            def n_ev(sql: str, *args: Any, _pid: str = pid) -> int:
                return int(conn.execute(sql, (_pid, s_iso, e_iso, *args)).fetchone()[0])
            entered = n_ev("SELECT COUNT(*) FROM n6_events WHERE portfolio_id=? AND ts>=? AND ts<? AND action='enter' "
                           "AND shadow=0 AND COALESCE(rule,'') != 'move'")
            moved = n_ev("SELECT COUNT(*) FROM n6_events WHERE portfolio_id=? AND ts>=? AND ts<? AND action='enter' "
                         "AND shadow=0 AND rule='move'")
            exits: dict[str, int] = {}
            for e in conn.execute("SELECT e.rule, e.position_id FROM n6_events e JOIN n6_positions p ON p.id = e.position_id "
                                  "WHERE e.portfolio_id=? AND e.ts>=? AND e.ts<? AND e.action='exit' AND p.twin_of IS NULL",
                                  (pid, s_iso, e_iso)):
                txt = RULE_JA.get(e["rule"] or "", e["rule"]) or "?"
                if e["rule"] == "manual":
                    x = conn.execute("SELECT reason FROM n6_exits WHERE position_id=? ORDER BY id DESC LIMIT 1",
                                     (e["position_id"],)).fetchone()
                    if x:
                        txt = f"手動（{REASONS_JA.get(x['reason'], x['reason'])}）"
                _bump(exits, txt)
                _bump(exit_all, txt)
            pmax = 0
            for ts in tick_ts:
                pmax = max(pmax, sum(1 for p in mains if p["portfolio_id"] == pid and p["opened_at"] <= ts
                                     and (p["closed_at"] is None or p["closed_at"] > ts)))
            row["portfolios"].append({
                "id": pid, "size_ja": size_ja(total), "picker_ja": PICKER_JA.get(pf["picker"], pf["picker"]),
                "marks": len(marks), "waiting_ticks": wait_n, "entered": entered, "exited": sum(exits.values()),
                "moved": moved, "max_positions": pmax, "equity_start": eq0, "equity_end": eq1,
                "pnl_usd": eq1 - eq0, "pnl_pct": (eq1 - eq0) / total * 100 if total else None,
                "max_drawdown_usd": dd, "wait_reasons": _top(waits), "exit_reasons": _top(exits)})
        row.update(max_positions=open_total_max, wait_reasons=_top(wait_all), exit_reasons=_top(exit_all), best_miss=best_day,
                   hedge_vs=_hedge_day(conn, mains, twins, s_iso, e_iso), new_vs_old=_new_old_day(conn, mains, s_iso, e_iso))
        out.append(row)
        day -= timedelta(days=1)
    return out


def _pnl_at(conn: sqlite3.Connection, p: sqlite3.Row, at: str) -> float | None:
    """at（含まない）までの最後の評価の損益。始める前なら None、閉じたあとは閉じたときの損益。"""
    if p["opened_at"] >= at:
        return None
    if p["closed_at"] is not None and p["closed_at"] < at:
        return float(p["pnl_usd"] or 0.0)
    r = conn.execute("SELECT pnl_usd FROM n6_marks WHERE position_id=? AND ts < ? ORDER BY ts DESC LIMIT 1",
                     (p["id"], at)).fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def _hedge_day(conn: sqlite3.Connection, mains: list[sqlite3.Row], twins: dict[int, sqlite3.Row],
               s_iso: str, e_iso: str) -> dict[str, Any]:
    """保険あり／なしの対の、この日の損益の動き（対がそろっている建玉だけ）。"""
    pairs, hedged, unhedged = 0, 0.0, 0.0
    for m in mains:
        tw = twins.get(m["id"])
        if tw is None:
            continue
        a1, a0 = _pnl_at(conn, m, e_iso), _pnl_at(conn, m, s_iso)
        b1, b0 = _pnl_at(conn, tw, e_iso), _pnl_at(conn, tw, s_iso)
        if a1 is None or b1 is None or (m["closed_at"] is not None and m["closed_at"] < s_iso):
            continue
        pairs += 1
        dm, dt = a1 - (a0 or 0.0), b1 - (b0 or 0.0)
        if m["hedge"]:
            hedged, unhedged = hedged + dm, unhedged + dt
        else:
            hedged, unhedged = hedged + dt, unhedged + dm
    return {"pairs": pairs, "hedged_pnl_usd": hedged, "unhedged_pnl_usd": unhedged}


def _detail_at(conn: sqlite3.Connection, pos_id: int, at: str) -> dict[str, Any]:
    r = conn.execute("SELECT detail_json FROM n6_marks WHERE position_id=? AND ts < ? ORDER BY ts DESC LIMIT 1",
                     (pos_id, at)).fetchone()
    return (_j(r[0]) or {}) if r else {}


def _new_old_day(conn: sqlite3.Connection, mains: list[sqlite3.Row], s_iso: str, e_iso: str) -> dict[str, Any]:
    """新しい設定と前の設定（影）の、この日の置き直しの回数と1日の損の線の合図。"""
    new_n = old_n = 0
    for m in mains:
        if m["kind"] != "pool_range" or m["opened_at"] >= e_iso or (m["closed_at"] is not None and m["closed_at"] < s_iso):
            continue
        d1, d0 = _detail_at(conn, m["id"], e_iso), _detail_at(conn, m["id"], s_iso)
        new_n += int(d1.get("rebalances") or 0) - int(d0.get("rebalances") or 0)
        old_n += int((d1.get("old") or {}).get("rebalances") or 0) - int((d0.get("old") or {}).get("rebalances") or 0)
    ev = conn.execute("SELECT rule, COUNT(*) FROM n6_events WHERE ts>=? AND ts<? AND rule IN ('loss_day','loss_day_old') "
                      "GROUP BY rule", (s_iso, e_iso)).fetchall()
    evd = {r[0]: r[1] for r in ev}
    return {"rebalances_new": new_n, "rebalances_old": old_n,
            "loss_day_new": evd.get("loss_day", 0), "loss_day_old": evd.get("loss_day_old", 0)}


# --- 異常のチェック ----------------------------------------------------------------------------------

def health(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> dict[str, Any]:
    """N6 の異常（指示書 13）。problems が空なら「異常なし」。notes は異常ではないお知らせ。"""
    now = now or datetime.now(UTC)
    problems: list[str] = []
    notes: list[str] = []
    tick = conn.execute("SELECT ts, ok, detail_json FROM n6_ticks ORDER BY ts DESC LIMIT 1").fetchone()
    if tick is None:
        problems.append("N6 の見回りがまだ1回も記録されていません")
    else:
        if not tick["ok"]:
            problems.append(f"最後の見回りが失敗しました（ok=False）: {(_j(tick['detail_json']) or {}).get('error')}")
        age = (now - _t(tick["ts"])).total_seconds() / 60
        if age > STALE_MINUTES:
            problems.append(f"N6 の見回りが {age:.0f} 分ありません（{STALE_MINUTES} 分より長い）")
    n_tick = len(problems)                  # ここまでは見回りの回の問題。ここから下は記録の食い違い（重大）
    pfs = {r["id"]: r for r in conn.execute("SELECT * FROM n6_portfolios")}
    rows = conn.execute("SELECT * FROM n6_positions").fetchall()
    by_id = {r["id"]: r for r in rows}
    for pid, pf in pfs.items():
        total = float(pf["total_usd"])
        mains = [r for r in rows if r["portfolio_id"] == pid and r["twin_of"] is None and r["status"] == "open"]
        placed = sum(float(r["amount_usd"]) for r in mains)
        if placed > total + EPS:
            problems.append(f"{pid}: 置いた額 ${placed:,.2f} が総額 ${total:,.0f} をこえている")
        if float(pf["cash_usd"]) < -EPS:
            problems.append(f"{pid}: 置いていないお金がマイナス（${float(pf['cash_usd']):,.2f}）")
        seen: dict[str, int] = {}
        for r in mains:
            seen[r["opp_key"]] = seen.get(r["opp_key"], 0) + 1
        for k, n in seen.items():
            if n > 1:
                problems.append(f"{pid}: 同じ入れる先の建玉が {n} つ開いている（{k}）")
        if pf["status"] == "stopped" and mains:
            problems.append(f"{pid}: 止まった練習に開いた建玉が {len(mains)} つ残っている")
    for r in rows:
        if r["status"] == "open" and r["closed_at"]:
            problems.append(f"建玉 {r['id']}: 閉じた時刻があるのに開いたまま")
        pf = pfs.get(r["portfolio_id"])
        if pf is None:
            problems.append(f"建玉 {r['id']}: 練習のまとまり {r['portfolio_id']} がない")
            continue
        if float(r["amount_usd"]) > float(pf["total_usd"]) + EPS:
            problems.append(f"建玉 {r['id']}: 額 ${float(r['amount_usd']):,.0f} が練習の総額 ${float(pf['total_usd']):,.0f} をこえている")
        if r["twin_of"] is not None:
            m = by_id.get(r["twin_of"])
            if m is None:
                problems.append(f"対 {r['id']}: 本体の建玉がない")
            elif m["portfolio_id"] != r["portfolio_id"]:
                problems.append(f"対 {r['id']}: 本体と練習のまとまりが違う（{r['portfolio_id']} と {m['portfolio_id']}）")
            elif r["status"] == "open" and m["status"] != "open":
                problems.append(f"対 {r['id']}: 本体は閉じたのに、対が開いたまま")
            elif m["hedge"] == r["hedge"]:
                problems.append(f"対 {r['id']}: 本体と保険のあり・なしが同じ")
            continue
        if r["move_from"] is not None and (by_id.get(r["move_from"]) or {"portfolio_id": r["portfolio_id"]})["portfolio_id"] \
                != r["portfolio_id"]:
            problems.append(f"建玉 {r['id']}: 別の練習のまとまりから移ってきている")
        tws = [t for t in rows if t["twin_of"] == r["id"]]
        if len(tws) > 1:
            problems.append(f"建玉 {r['id']}: 対が {len(tws)} つある")
        if not tws and r["status"] == "open":
            why = conn.execute("SELECT message_ja FROM n6_events WHERE position_id=? AND action='info' AND message_ja LIKE '対%' "
                               "LIMIT 1", (r["id"],)).fetchone()
            if why:
                notes.append(f"建玉 {r['id']}: 対は作れなかった（理由つき: {why[0]}）")
            else:
                problems.append(f"建玉 {r['id']}: 保険あり／なしの対が欠けている")
        est = (_j(r["state_json"]) or {}).get("est") or {}
        pool = float((est.get("split") or {}).get("pool") or 0.0)
        cap = est.get("cap_usd")
        if cap is None and est.get("tvl_usd"):
            cap = float(est["tvl_usd"]) * config.opportunities.max_pool_share
        if r["kind"] in ("pool_range", "pool_full") and cap and pool > float(cap) * (1 + 1e-6) + EPS:
            problems.append(f"建玉 {r['id']}: プールに置く分 ${pool:,.2f} が入ったときの 5% 上限 ${float(cap):,.2f} をこえている")
    found = realmoney_scan.scan()
    if found:
        problems.append("本物のお金につながる言葉が N6 のコードにある: " + "、".join(found))
    # serious: 記録の食い違いと本物のお金の言葉（更新の1行はこれがあれば止める。2026-10-08 指示書）。
    # 見回りの回の問題（まだない・失敗・30分の空き）は、更新の1行が last_tick で別に確かめる
    return {"ok": not problems, "checked_at": now.isoformat(timespec="seconds"), "problems": problems, "notes": notes,
            "serious": problems[n_tick:], "realmoney": found,
            "last_tick": tick["ts"] if tick else None, "ticks": conn.execute("SELECT COUNT(*) FROM n6_ticks").fetchone()[0]}
