"""2週間の評価（M5d。SPEC 11章「M5の後、2週間のペーパートレードで予測と実績の乖離を評価する」）。

オーナーがボタンを押すと評価の期間（既定14日）が始まる。期間の中の練習の記録から、次をまとめる:
- データの集まり具合（15分ごとの収集が何回成功したか。パソコンが止まっていた時間）
- 予測と実績の差（6区分・1日あたり）。推定の行（収集が止まっていた時間）と、開く・閉じる時の1回きりの費用は除く
- 報酬を持ち続けた場合と、すぐ売った場合のどちらが予測に近いか
- 見張りの記録の件数（置き直し・離脱など）
合格の基準は SPEC に書かれていないので、ここでは数字を並べるだけにする（基準はオーナーが決める）。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import views
from ..collectors.completeness import check
from ..config import Config
from ..risk.rules import LEVEL_JA
from .paper import CATS

PRED_KEYS = {"income": ("income", 1), "direction": ("direction_risk", -1), "gamma": ("gamma", -1),
             "hedge": ("hedge", -1), "haircut": ("haircut", -1), "other": ("rebalance", -1)}


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def current(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()


def start(conn: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    """評価の期間を始める（練習モードのときだけ。進行中なら何もしない）。"""
    if config.mode != "paper":
        raise ValueError("評価は練習モード（config.yaml の mode: paper）のときだけ始められます。")
    cur = current(conn)
    if cur is not None and cur["ends_at"] > _iso(now) and cur["status"] == "running":
        raise ValueError("評価はもう始まっています。")
    ends = now + timedelta(days=config.review.evaluation_days)
    conn.execute("INSERT INTO evaluations(started_at, ends_at, status) VALUES (?,?, 'running')",
                 (_iso(now), _iso(ends)))
    conn.commit()
    return {"started_at": _iso(now), "ends_at": _iso(ends)}


def stop(conn: sqlite3.Connection, now: datetime) -> None:
    """進行中の評価を途中でやめる（記録は残す）。"""
    cur = current(conn)
    if cur is not None and cur["status"] == "running":
        conn.execute("UPDATE evaluations SET status='stopped', ends_at=? WHERE id=?", (_iso(now), cur["id"]))
        conn.commit()


def summary(conn: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    cur = current(conn)
    base = {"evaluation_days": config.review.evaluation_days, "mode": config.mode}
    if cur is None:
        return {**base, "state": "not_started"}
    start_t = datetime.fromisoformat(cur["started_at"])
    end_t = datetime.fromisoformat(cur["ends_at"])
    until = min(now, end_t)
    state = "running" if cur["status"] == "running" and now < end_t else (
        "stopped" if cur["status"] == "stopped" else "finished")
    hours = max(0.0, (until - start_t).total_seconds() / 3600)

    # データの集まり具合（会場ごと）
    coverage = []
    for (vid,) in conn.execute("SELECT id FROM venues ORDER BY id"):
        h = int(math.floor(hours))
        if h <= 0:
            continue
        r = check(conn, vid, hours=h, minutes=config.snapshot_minutes, now=until)
        coverage.append({"venue_id": vid, "expected": r.expected, "ok": r.ok,
                         "ratio": r.ok / r.expected if r.expected else None,
                         "missing_hours": len(r.missing) * config.snapshot_minutes / 60})

    # 予測と実績（期間の中の、推定でない行。開く・閉じる時の行は1回きりの費用なので除く）
    rows = conn.execute("""SELECT n.*, p.predicted_json FROM position_pnl n JOIN positions p ON p.id=n.position_id
                           WHERE p.is_paper=1 AND n.ts>? AND n.ts<=?""", (_iso(start_t), _iso(until))).fetchall()
    actual = {c: 0.0 for c in CATS}
    predicted = {c: 0.0 for c in CATS}
    sell_haircut = 0.0
    obs_hours = est_hours = 0.0
    per_pos: set[int] = set()
    for r in rows:
        d = json.loads(r["detail_json"] or "{}")
        if d.get("event") in ("open", "close"):
            continue
        dt_h = float(d.get("dt_s") or 0.0) / 3600
        if r["is_estimated"]:
            est_hours += dt_h
            continue
        per_pos.add(r["position_id"])
        for c in CATS:
            actual[c] += float(r[c] or 0.0)
        sell_haircut += float(r["haircut_sell"] or 0.0)
        obs_hours += dt_h
        pred = json.loads(r["predicted_json"] or "{}")
        for c, (key, sign) in PRED_KEYS.items():
            predicted[c] += sign * float(pred.get(key) or 0.0) * dt_h / 24
    days = obs_hours / 24
    per_day = (lambda v: v / days) if days > 0 else (lambda v: None)
    compare = [{"key": c, "label": views.CATEGORY_JA[c], "predicted": per_day(predicted[c]), "actual": per_day(actual[c])}
               for c in CATS]
    pred_net, act_net = sum(predicted.values()), sum(actual.values())
    sell_net = act_net - actual["haircut"] + sell_haircut
    events = [{"level": r["level"], "level_ja": LEVEL_JA.get(r["level"], r["level"]), "n": r["n"]}
              for r in conn.execute("SELECT level, COUNT(*) AS n FROM risk_events WHERE ts>? AND ts<=? "
                                    "GROUP BY level ORDER BY n DESC", (_iso(start_t), _iso(until)))]
    return {
        **base, "state": state, "started_at": cur["started_at"], "ends_at": cur["ends_at"],
        "elapsed_hours": hours, "left_hours": max(0.0, (end_t - now).total_seconds() / 3600) if state == "running" else 0,
        "coverage": coverage, "positions": len(per_pos),
        "observed_hours": obs_hours, "estimated_hours": est_hours,
        "compare": compare,
        "predicted_net_day": per_day(pred_net), "actual_net_day": per_day(act_net),
        "gap_pct": (act_net / pred_net - 1) * 100 if pred_net else None,
        "hold_net_day": per_day(act_net), "sell_net_day": per_day(sell_net),
        "closer": (None if not days or not pred_net else
                   ("hold" if abs(act_net - pred_net) <= abs(sell_net - pred_net) else "sell")),
        "events": events,
        "note": ("予測はスコア（始めた時の1日の見込み）を、実際に記録した時間の分だけ足したもの。実績は同じ時間の6区分の合計。"
                 "パソコンが止まっていた時間（推定）と、開く・閉じる時の1回きりの費用は比べる対象から外しています。"
                 "合格の基準はまだ決まっていません。"),
    }
