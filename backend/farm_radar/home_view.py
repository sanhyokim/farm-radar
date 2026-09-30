"""ホーム画面の「今日やること」「練習のまとめ」「評価の進み具合」（2026-09-30 オーナー依頼 17・18・32）。

表示のためのまとめだけ。判定・練習・評価の計算には触らない。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import views
from .config import Config

# 並べる順（危険 → 注意 → お知らせ）。オーナー依頼 15: 危険を上に
LEVEL_ORDER = {"danger": 0, "attention": 1, "info": 2}
SIGNAL_JA = {"green": "良い", "yellow": "様子見", "red": "危険"}


def _hm(ts: str | None) -> str:
    if not ts:
        return "—"
    t = datetime.fromisoformat(ts).astimezone(views.JST)
    return f"{t.month}/{t.day} {t:%H:%M}"


def _hours_text(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))}分"
    if h < 48:
        return f"{h:.0f}時間"
    return f"{h / 24:.0f}日"


def paper_summary(cards: list[dict[str, Any]], state: dict[str, Any], mode: str) -> dict[str, Any]:
    """練習のまとめ（評価額・今日の損益・状態。オーナー依頼 18）。cards は持っている建玉のカード。"""
    rows = []
    for c in cards:
        if c["cautions"]:
            status, tone = c["cautions"][0], "attention"
        elif not c["in_range"]:
            status, tone = "レンジの外（手数料もボーナスも入りません）", "attention"
        else:
            status, tone = "レンジの中", "ok"
        rows.append({"id": c["id"], "pair": c["pair"], "venue_id": c["venue_id"], "value": c["value"],
                     "change_usd": c["change_usd"], "today_usd": c["today_usd"],
                     "reward_24h_usd": c["reward_24h_usd"], "status": status, "tone": tone,
                     "hours": c["hours"], "spark": c["spark"], "started_red": c["started_red"]})
    capital = sum(float(c["capital"] or 0.0) for c in cards)
    if mode != "paper":
        text = "練習は使っていません（config.yaml の mode が paper ではありません）。"
    elif state["stopped"]:
        text = "停止中です。持っている建玉の計算と見張りは続けています。"
    elif rows:
        text = f"{len(rows)}つの建玉を自動で見張っています。"
    else:
        text = "持っている建玉はありません。"
    return {"enabled": mode == "paper", "stopped": state["stopped"], "text": text, "positions": rows,
            "total": {"capital": capital, "value": sum(r["value"] for r in rows),
                      "change_usd": sum(r["change_usd"] for r in rows),
                      "today_usd": sum(r["today_usd"] for r in rows)}}


def evaluation_light(ev: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """評価の進み具合（何日目か、データの集まり具合、1日ごとの印）。評価をしていなければ None。"""
    if ev.get("state") == "not_started":
        return None
    crit = ev["criteria"]
    day = min(crit["days"], int(ev["elapsed_hours"] // 24) + 1)
    return {
        "state": ev["state"], "started_at": ev["started_at"], "ends_at": ev["ends_at"],
        "day": day, "days": crit["days"], "left_hours": ev["left_hours"],
        "coverage_pct": crit["coverage_pct"], "min_coverage_pct": crit["min_coverage_pct"],
        "coverage_ok": crit["coverage_ok"],
        "ok_days": crit["hold"]["ok_days"], "need_days": crit["hold"]["need_days"],
        # 1日ごとの印: ok（予測に近かった）/ ng（外れた）/ running（今日。まだ24時間たっていない）/ none（まだ先）
        "marks": [_mark(ev, k, day) for k in range(crit["days"])],
    }


def _mark(ev: dict[str, Any], k: int, day: int) -> dict[str, Any]:
    rows = ev["days"]
    if k < len(rows):
        r = rows[k]
        return {"day": k + 1, "state": ("ok" if r["hold_ok"] else "ng") if r["done"] else "running", "flip": r["flip"]}
    return {"day": k + 1, "state": "running" if ev["state"] == "running" and k == day - 1 else "none", "flip": False}


def _signal_changes(conn: sqlite3.Connection, pool_ids: list[str], now: datetime) -> list[tuple[str, str, str]]:
    """練習中のプールで、24時間前から判定が変わったもの（プール, 前, 今）。"""
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    out = []
    for pid in pool_ids:
        cur = conn.execute("SELECT signal FROM scores WHERE pool_id=? ORDER BY ts DESC LIMIT 1", (pid,)).fetchone()
        old = conn.execute("SELECT signal FROM scores WHERE pool_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                           (pid, since)).fetchone()
        if cur and old and cur[0] != old[0]:
            out.append((pid, old[0], cur[0]))
    return out


def todo(conn: sqlite3.Connection, config: Config, now: datetime, *, collection: list[dict[str, Any]],
         paper: dict[str, Any], cards: list[dict[str, Any]], evaluation: dict[str, Any] | None,
         plans_soon: list[dict[str, Any]], flip_at: str | None) -> list[dict[str, Any]]:
    """「今日やること」（オーナー依頼 17）。level は danger / attention / info。

    title は何が起きたか、action はオーナーがすること（何もしなくてよいときはそう書く）。to は開く画面。
    """
    items: list[dict[str, Any]] = []

    def add(level: str, title: str, action: str, to: str | None = None) -> None:
        items.append({"level": level, "title": title, "action": action, "to": to})

    # 1. 収集が止まっている（練習する会場は危険、観察だけの会場は注意）
    for h in collection:
        if not h["stale"]:
            continue
        when = f"最後に集めたのは {_hm(h['last_ok_at'])}" if h["last_ok_at"] else "まだ一度も集められていません"
        if h["observe"]:
            add("attention", f"{h['name']} の読み取りが遅れています（観察だけの会場）",
                f"{when}。評価の会場を優先して休むことがあるので、半日ほど続くときだけ確かめてください。", "/venues")
        else:
            add("danger", f"{h['name']} のデータの収集が止まっています",
                f"{when}。パソコンがスリープしていないか、Docker Desktop が動いているかを確かめてください。", "/venues")
    # 2. 評価のデータの集まり具合
    if evaluation and evaluation["state"] == "running" and evaluation["coverage_pct"] is not None \
            and not evaluation["coverage_ok"]:
        add("danger", f"評価のデータの集まり具合が {evaluation['coverage_pct']:.1f}% です"
                      f"（{evaluation['min_coverage_pct']:.0f}% 以上が必要）",
            "パソコンを止めないようにしてください。記録のない時間は評価で「満たさない」に数えます。", "/practice")
    # 3. 直近24時間に練習の建玉が閉じられた（離脱・緊急離脱）
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    for e in conn.execute("SELECT * FROM risk_events WHERE ts>? AND level IN ('exit', 'emergency') ORDER BY ts DESC",
                          (since,)):
        add("danger", "練習の建玉を自動で閉じました（" + ("緊急離脱" if e["level"] == "emergency" else "離脱") + "）",
            f"{e['message_ja']}（{_hm(e['ts'])}）。練習タブで中身を確かめてください。", "/practice")
    # 4. 練習が止まっている
    if paper["enabled"] and paper["stopped"]:
        add("attention", "練習は停止中です",
            "新しい建玉は作れません。持っている建玉の見張りは続いています。練習タブで理由を見て、よければ再開してください。",
            "/practice")
    # 5. 持っている建玉の注意と、ボーナスが減ったときの比べ方（記録だけ）
    for c in cards:
        for text in c["cautions"]:
            add("attention", f"{c['pair']}: {text}", "見るだけで大丈夫です。ルールに当たれば自動で置き直し・離脱します。",
                f"/practice/{c['id']}")
        bd = c.get("bonus_drop")
        if bd and bd.get("at", "") >= since:
            add("attention", f"{c['pair']}: 切り替えでボーナスが減りました",
                f"いちばん損が少ないのは「{bd.get('best_ja')}」でした。今は記録だけで、建玉はそのままです。", f"/practice/{c['id']}")
    # 6. 練習中のプールの判定が変わった
    names = {c["pool_id"]: c["pair"] for c in cards}
    for pid, old, cur in _signal_changes(conn, list(names), now):
        add("info", f"{names[pid]} の判定が「{SIGNAL_JA.get(old, old)}」から「{SIGNAL_JA.get(cur, cur)}」に変わりました",
            "練習中の建玉はそのままです。理由はプールの画面で見られます。", f"/pools/{pid}")
    # 7. 期限の近い予定
    for p in plans_soon:
        days = p.get("days_left")
        when = "今日まで" if days is not None and days < 1 else (f"あと{days:.0f}日" if days is not None else "")
        add("attention", f"予定: {p['title']}（{when}）" if when else f"予定: {p['title']}",
            p.get("text") or "学ぶタブの「予定とメモ」で中身を見てください。", "/learn#plans")
    # 8. 評価がもうすぐ終わる
    if evaluation and evaluation["state"] == "running" and evaluation["left_hours"] <= 24:
        add("info", f"評価はあと{_hours_text(evaluation['left_hours'])}で終わります",
            "終わったら、練習タブで結果を見てください。", "/practice")
    # 9. 木曜の切り替えが近い（2日以内。切り替えの知らせは、ホームではここに1回だけ出す）
    if flip_at:
        left_h = (datetime.fromisoformat(flip_at) - now).total_seconds() / 3600
        if 0 < left_h <= 48:
            t = datetime.fromisoformat(flip_at).astimezone(views.JST)
            extra = f"練習中の{len(cards)}つにも関係します。" if cards else ""
            add("info", f"木曜 {t:%H:%M} にボーナスの切り替え（あと{_hours_text(left_h)}）",
                f"来週のボーナスが減ったり、なくなったりすることがあります。{extra}何もしなくて大丈夫です（自動で記録します）。",
                "/pools")
    items.sort(key=lambda x: LEVEL_ORDER[x["level"]])
    return items

