"""定時レビュー・タイムライン・損益カレンダー・資産の見通し・月次CSV（M5c。SPEC 7.4章・8.5章・12.4章）。

- 定時レビュー: 30分ごと（config の review.every_minutes）に、持っている練習の建玉の様子を、ルールで文にして reviews 表に残す
  （AI は使わない。2026-09-29 オーナー決定）
- タイムライン: 定時レビュー・見張りの記録・建玉の開始と終了を、時刻の順に並べる（種類ごとに色分けは画面で）
- 損益カレンダー: 練習の建玉の純損益を、日本時間の1日ごとに合計する
- 資産の見通し: 始めてからの1日平均の純損益（始めた費用込み）× 日数を、今の評価額に足す（複利にしない。1か月後まで）。
  下限は、プラスの項目を控えめ・マイナスの項目を厳しめに。始めて24時間未満は出さない（2026-09-29 オーナー指示）
- 月次CSV: 台帳（練習の取引）を月ごとに書き出す（税務の形式の確認用。Excel で開けるように BOM つき UTF-8）
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import views
from ..config import Config
from . import views as pviews
from .paper import CATS

OUTLOOK_DAYS = (("1週間後", 7), ("1か月後", 30))
LEDGER_KIND_JA = {"deposit": "入れる", "withdraw": "引き出す", "claim": "報酬の受け取り", "cost": "費用",
                  "hedge_open": "ヘッジを持つ", "hedge_close": "ヘッジを閉じる", "sell_reward": "報酬を売る",
                  "hedge_adjust": "ヘッジの量を合わせる"}


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open' ORDER BY id").fetchall()


# --- 定時レビュー ----------------------------------------------------------------------------

def make_review(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> dict[str, Any] | None:
    """定時レビューを1つ作って保存する。建玉がなく、この間に何も起きていなければ作らない（None）。"""
    if config.mode != "paper":
        return None
    now = now or datetime.now(UTC)
    minutes = config.review.every_minutes
    since = _iso(now - timedelta(minutes=minutes))
    positions = _open_positions(conn)
    ev = conn.execute("SELECT level, kind FROM risk_events WHERE ts>? ORDER BY ts", (since,)).fetchall()
    closed = conn.execute("SELECT COUNT(*) FROM positions WHERE is_paper=1 AND closed_at>?", (since,)).fetchone()[0]
    if not positions and not ev and not closed:
        return None
    lines: list[str] = []
    items: list[dict[str, Any]] = []
    out_n = 0
    for pos in positions:
        c = pviews.card(conn, pos, now, config.risk)
        rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=? AND ts>?", (pos["id"], since)).fetchall()
        win = {k: sum(float(r[k] or 0.0) for r in rows) for k in CATS}
        win_net = sum(win.values())
        gap = any(r["is_estimated"] for r in rows)
        if not c["in_range"]:
            out_n += 1
        edge = min(c["to_lower_pct"], c["to_upper_pct"]) if c["in_range"] else None
        parts = [f"直近{minutes}分 {_su(win_net)}（報酬 {_su(win['income'])}）",
                 f"評価額 ${c['value']:,.2f}（始めてから {_su(c['change_usd'])}）"]
        parts.append(f"レンジの端まで {edge:.1f}%" if edge is not None else "レンジの外")
        pred = json.loads(pos["predicted_json"] or "{}").get("income")
        ratio = None
        if pred and c["hours"] >= config.risk.caution_min_hours:
            since24 = _iso(now - timedelta(hours=min(24.0, c["hours"])))
            inc = conn.execute("SELECT SUM(income) FROM position_pnl WHERE position_id=? AND ts>?",
                               (pos["id"], since24)).fetchone()[0] or 0.0
            ratio = inc / min(24.0, c["hours"]) * 24 / pred - 1
            parts.append(f"報酬の実績は予測比 {ratio * 100:+.0f}%")
        if not rows:
            parts.append("この間の記録なし")
        elif gap:
            parts.append("記録の欠けあり（推定）")
        lines.append(f"{c['pair']}: " + "。".join(parts) + "。")
        items.append({"id": pos["id"], "pair": c["pair"], "in_range": c["in_range"], "window_net": win_net,
                      "window_income": win["income"], "value": c["value"], "change_usd": c["change_usd"],
                      "edge_pct": edge, "reward_vs_pred": ratio, "estimated": gap})
    n = len(positions)
    if n == 0:
        head = "持っている建玉はありません。"
    elif out_n == 0:
        head = f"{n}建玉すべてレンジ内。"
    else:
        head = f"{n}建玉のうち{out_n}つがレンジの外。"
    counts: dict[str, int] = {}
    for e in ev:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
    if counts:
        from ..risk.rules import LEVEL_JA
        moves = "・".join(f"{LEVEL_JA.get(k, k)}{v}件" for k, v in counts.items())
        tail = f"この{minutes}分の見張り: {moves}。"
    else:
        tail = "移動なし。"
    if closed:
        tail += f"閉じた建玉 {closed}件。"
    body = "\n".join([head, *lines, tail])
    title = f"定時レビュー {now.astimezone(views.JST).strftime('%H:%M')}"
    ts = _iso(now)
    conn.execute("INSERT INTO reviews(ts, kind, title, body_ja, positions_json) VALUES (?,?,?,?,?)",
                 (ts, "review", title, body, json.dumps(items)))
    conn.commit()
    return {"ts": ts, "title": title, "body": body, "positions": items}


def _su(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


# --- タイムライン ---------------------------------------------------------------------------

def timeline(conn: sqlite3.Connection, limit: int = 100, kinds: set[str] | None = None,
             position_id: int | None = None) -> list[dict[str, Any]]:
    """定時レビュー（review）・見張りの記録（event）・開始（open）・終了（close）を新しい順に。"""
    items: list[dict[str, Any]] = []
    want = kinds or {"review", "event", "open", "close"}
    if "review" in want and position_id is None:
        for r in conn.execute("SELECT rowid, * FROM reviews WHERE kind='review' ORDER BY ts DESC LIMIT ?", (limit,)):
            items.append({"key": f"r{r['rowid']}", "ts": r["ts"], "type": "review", "level": "review",
                          "title": r["title"], "body": r["body_ja"]})
    if "event" in want:
        for e in pviews.events(conn, position_id, limit):
            items.append({"key": f"e{e['id']}", "ts": e["ts"], "type": "event", "level": e["level"],
                          "title": e["level_ja"] + ("" if e["action"] == "none" else f" → {e['action_ja']}"),
                          "body": e["message"], "position_id": e["position_id"]})
    pos_sql = "SELECT p.*, l.token0_symbol, l.token1_symbol FROM positions p JOIN pools l ON l.id=p.pool_id " \
              "WHERE p.is_paper=1" + (" AND p.id=?" if position_id is not None else "")
    for p in conn.execute(pos_sql, (position_id,) if position_id is not None else ()):
        pair = f"{p['token0_symbol']}/{p['token1_symbol']}"
        if "open" in want:
            items.append({"key": f"o{p['id']}", "ts": p["opened_at"], "type": "open", "level": "open",
                          "title": "練習を開始", "body": f"{pair} に ${p['capital']:,.0f}（±{p['r'] * 100:g}%）"
                          + ("。判定は🔴で開始" if p["started_red"] else ""), "position_id": p["id"]})
        if "close" in want and p["closed_at"]:
            net = conn.execute("SELECT SUM(net) FROM position_pnl WHERE position_id=?", (p["id"],)).fetchone()[0] or 0.0
            items.append({"key": f"c{p['id']}", "ts": p["closed_at"], "type": "close", "level": "close",
                          "title": "練習を終了", "body": f"{pair}（{pviews.close_reason_ja(p['close_reason'])}）"
                          f"。純損益 {_su(net)}", "position_id": p["id"]})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# --- 損益カレンダー --------------------------------------------------------------------------

def calendar(conn: sqlite3.Connection, month: str | None, now: datetime) -> dict[str, Any]:
    """日本時間の1日ごとの、練習の純損益の合計（month は "2026-09"。省略で今月）。"""
    month = month or now.astimezone(views.JST).strftime("%Y-%m")
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=views.JST)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=views.JST)
    rows = conn.execute("""SELECT n.ts, n.net, n.income, n.is_estimated FROM position_pnl n
                           JOIN positions p ON p.id=n.position_id
                           WHERE p.is_paper=1 AND n.ts>=? AND n.ts<?""", (_iso(start), _iso(end))).fetchall()
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = views.jst_day(r["ts"])
        x = days.setdefault(d, {"day": d, "net": 0.0, "income": 0.0, "estimated": False})
        x["net"] += float(r["net"] or 0.0)
        x["income"] += float(r["income"] or 0.0)
        x["estimated"] = x["estimated"] or bool(r["is_estimated"])
    first = conn.execute("SELECT MIN(opened_at) FROM positions WHERE is_paper=1").fetchone()[0]
    first_month = datetime.fromisoformat(first).astimezone(views.JST).strftime("%Y-%m") if first else month
    prev = (start - timedelta(days=1)).strftime("%Y-%m")
    this_month = now.astimezone(views.JST).strftime("%Y-%m")
    return {
        "month": month, "first_weekday": start.weekday(),       # 0 = 月曜
        "days_in_month": (end - start).days, "today": now.astimezone(views.JST).strftime("%Y-%m-%d"),
        "days": sorted(days.values(), key=lambda x: x["day"]),
        "total": sum(x["net"] for x in days.values()),
        "prev": prev if prev >= first_month else None,
        "next": end.strftime("%Y-%m") if end.strftime("%Y-%m") <= this_month else None,
    }


# --- 資産の見通し ----------------------------------------------------------------------------

def outlook(conn: sqlite3.Connection, config: Config, now: datetime,
            positions: list[sqlite3.Row] | None = None) -> dict[str, Any] | None:
    """持っている建玉の実績から、1週間後・1か月後の資産を見積もる（必ず「推定」）。

    1日の純損益 = 始めてからの純損益の合計（始めた費用込み）÷ たった日数。見通し = 今の評価額 + 1日の純損益 × 日数
    （複利にしない。2026-09-29 オーナー指示）。始めて review.outlook_min_hours 時間未満なら数字を出さない（short）。
    """
    positions = _open_positions(conn) if positions is None else positions
    if not positions:
        return None
    rc = config.review
    k = rc.outlook_conservative_pct / 100
    cap = value = daily = low = 0.0
    hours = []
    for pos in positions:
        c = pviews.card(conn, pos, now, config.risk)
        rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=?", (pos["id"],)).fetchall()
        per = {cat: sum(float(r[cat] or 0.0) for r in rows) for cat in CATS}   # 始めた費用も「その他」に入ったまま
        d = max(c["days"], 1e-9)
        per_day = {cat: v / d for cat, v in per.items()}
        cap += pos["capital"] or 0.0
        value += c["value"]
        daily += sum(per_day.values())
        low += sum(v * (1 - k) if v > 0 else v * (1 + k) for v in per_day.values())
        hours.append(c["hours"])
    short = min(hours) < rc.outlook_min_hours
    base = {
        "value_now": value, "capital": cap, "hours": min(hours), "short": short, "min_hours": rc.outlook_min_hours,
        "conservative_pct": rc.outlook_conservative_pct,
        "note": ("始めてからの1日平均の純損益（始めた費用込み）が同じように続くとして、今の評価額に日数分を足した推定です"
                 "（増えた分がさらに増える計算＝複利にはしていません）。"
                 f"下限は、プラスの項目を{rc.outlook_conservative_pct:g}%控えめ、マイナスの項目を"
                 f"{rc.outlook_conservative_pct:g}%厳しめにしています。報酬の量や値段は毎日変わるので、当たるとは限りません。"),
    }
    if short:
        return {**base, "daily_usd": None, "daily_pct": None, "daily_low_usd": None, "daily_low_pct": None, "rows": []}
    return {**base, "daily_usd": daily, "daily_pct": daily / cap * 100, "daily_low_usd": low,
            "daily_low_pct": low / cap * 100,
            "rows": [{"label": label, "days": n, "value": value + daily * n, "low": value + low * n}
                     for label, n in OUTLOOK_DAYS]}


# --- 月次CSV ------------------------------------------------------------------------------

CSV_HEADER = ["日時（日本時間）", "種類", "トークン", "数量", "単価（ドル）", "金額（ドル）", "円のレート（1ドル）",
              "レートの日付", "金額（円）", "建玉", "練習", "メモ"]


def ledger_csv(conn: sqlite3.Connection, month: str) -> str:
    """台帳の1か月分（日本時間）を CSV にする。先頭に BOM（Excel で文字化けしないように）。"""
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=views.JST)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=views.JST)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for r in conn.execute("SELECT * FROM ledger WHERE ts>=? AND ts<? ORDER BY ts, id", (_iso(start), _iso(end))):
        value = r["value_usd"] if r["value_usd"] is not None else (
            (r["amount"] or 0.0) * r["price_usd"] if r["price_usd"] is not None else None)
        jpy = value * r["fx_rate"] if value is not None and r["fx_rate"] else None
        w.writerow([
            datetime.fromisoformat(r["ts"]).astimezone(views.JST).strftime("%Y-%m-%d %H:%M:%S"),
            LEDGER_KIND_JA.get(r["kind"], r["kind"]), r["token"] or "",
            _num(r["amount"], 8), _num(r["price_usd"], 8), _num(value, 4), _num(r["fx_rate"], 4),
            r["fx_date"] or "", _num(jpy, 0), r["position_id"] or "", "はい" if r["is_paper"] else "いいえ",
            r["note"] or "",
        ])
    return "﻿" + buf.getvalue()


def _num(v: float | None, digits: int) -> str:
    if v is None:
        return ""
    return f"{v:.{digits}f}" if digits else f"{round(v):d}"


def ledger_months(conn: sqlite3.Connection) -> list[str]:
    months = set()
    for (ts,) in conn.execute("SELECT ts FROM ledger WHERE is_paper=1"):
        months.add(datetime.fromisoformat(ts).astimezone(views.JST).strftime("%Y-%m"))
    return sorted(months, reverse=True)
