"""2週間の評価（M5d。SPEC 11章「M5の後、2週間のペーパートレードで予測と実績の乖離を評価する」）。

オーナーがボタンを押すと評価の期間（既定14日）が始まる。期間の中の練習の記録から、次をまとめる:
- データの集まり具合（15分ごとの収集が何回成功したか。パソコンが止まっていた時間）
- 予測と実績の差（6区分・1日あたり）。推定の行（収集が止まっていた時間）と、開く・閉じる時の1回きりの費用は除く
- 報酬を持ち続けた場合と、すぐ売った場合のどちらが予測に近いか
- 見張りの記録の件数（置き直し・離脱など）
合格の基準（2026-09-29 オーナー決定。config.yaml の evaluation）:
1. データの集まり具合が95%以上
2. 1日ごとの純損益で、予測と実績の差が「±30%以内」または「総資産の0.1%以内」の日が、評価日数の70%以上
3. 報酬を「持ち続ける前提」と「すぐ売る前提」の両方で判定して並べる
「1日」は評価を始めた時刻から24時間ずつ区切る。記録のない日は「満たさない日」に数える。
2026-10-01 オーナー決定 C: 1つの建玉が閉じても、評価は残りの建玉で続ける。評価の建玉が全部閉じたら「中断」（interrupted）にして
合否は出さない（paper.interrupt_evaluation_if_empty）。練習の建玉が1つもないときは始められない。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import views
from ..collectors.completeness import check
from ..config import Config, ConfigError, load_venue, practice_allowed
from ..risk.rules import LEVEL_JA
from .flip import live_per_day
from .paper import CATS
from .views import close_reason_ja

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
    if conn.execute("SELECT 1 FROM positions WHERE is_paper=1 AND status='open' LIMIT 1").fetchone() is None:
        raise ValueError("練習の建玉が1つもないので、評価を始められません。先に練習を開いてください（2026-10-01 の決まり）。")
    ends = now + timedelta(days=config.evaluation.days)
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


def evaluation_venues(conn: sqlite3.Connection, config: Config, start: datetime, until: datetime) -> list[str]:
    """評価の期間に練習の建玉があった会場。建玉がまだなければ、練習のできる会場（practice: false でないもの）。"""
    rows = conn.execute(
        """SELECT DISTINCT venue_id FROM positions WHERE is_paper=1 AND opened_at<=?
           AND (closed_at IS NULL OR closed_at>=?) ORDER BY venue_id""", (_iso(until), _iso(start))).fetchall()
    if rows:
        return [r[0] for r in rows if r[0]]
    out = []
    for vid in config.venues:
        try:
            if practice_allowed(load_venue(vid, config.root)):
                out.append(vid)
        except (OSError, ConfigError):
            continue
    return out


def summary(conn: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    cur = current(conn)
    base = {"evaluation_days": config.evaluation.days, "mode": config.mode}
    if cur is None:
        return {**base, "state": "not_started"}
    start_t = datetime.fromisoformat(cur["started_at"])
    end_t = datetime.fromisoformat(cur["ends_at"])
    until = min(now, end_t)
    state = "running" if cur["status"] == "running" and now < end_t else (
        cur["status"] if cur["status"] in ("stopped", "interrupted") else "finished")
    hours = max(0.0, (until - start_t).total_seconds() / 3600)

    # データの集まり具合（会場ごと）。数えるのは評価の建玉がある会場だけ（2026-09-30 オーナー条件:
    # 観察だけの会場 Alandale は評価に入れない。途中から増えた会場の収集は、評価の合否に関係しない）
    coverage = []
    for vid in evaluation_venues(conn, config, start_t, until):
        h = int(math.floor(hours))
        if h <= 0:
            continue
        r = check(conn, vid, hours=h, minutes=config.snapshot_minutes, now=until)
        coverage.append({"venue_id": vid, "expected": r.expected, "ok": r.ok,
                         "ratio": r.ok / r.expected if r.expected else None,
                         "missing_hours": len(r.missing) * config.snapshot_minutes / 60})

    # 予測と実績（期間の中の、推定でない行。開く・閉じる時の行は1回きりの費用なので除く）
    rows = conn.execute("""SELECT n.*, p.predicted_json, p.capital, p.pool_id, p.mode FROM position_pnl n
                           JOIN positions p ON p.id=n.position_id
                           WHERE p.is_paper=1 AND n.ts>? AND n.ts<=? ORDER BY n.position_id, n.ts""",
                        (_iso(start_t), _iso(until))).fetchall()
    actual = {c: 0.0 for c in CATS}
    predicted = {c: 0.0 for c in CATS}
    sell_haircut = 0.0
    pred_sell_net = 0.0
    obs_hours = est_hours = 0.0
    per_pos: set[int] = set()
    ev = config.evaluation
    n_days = ev.days
    daily = [{"pred": 0.0, "pred_sell": 0.0, "hold": 0.0, "sell": 0.0, "hours": 0.0, "capital": {},
              "ref": 0.0, "ref_hours": 0.0} for _ in range(n_days)]
    # 参考: その時点の最新のスコアで作った見込み（2026-09-30 オーナー決定②。合否には使わない）。
    # 建玉のレンジ幅は置き直しで変わるので、置き直しの行を見ながら追いかける
    ref_net = ref_hours = 0.0
    ref_cache: dict = {}
    r_now: dict[int, float] = {}
    for r in rows:
        d = json.loads(r["detail_json"] or "{}")
        if r["position_id"] not in r_now:
            r_now[r["position_id"]] = float(json.loads(r["predicted_json"] or "{}").get("r_pct") or 0.0)
        if d.get("event") == "rebalance" and d.get("r_pct") is not None:
            r_now[r["position_id"]] = float(d["r_pct"])
        if d.get("event") in ("open", "close"):
            continue
        dt_h = float(d.get("dt_s") or 0.0) / 3600
        if r["is_estimated"]:
            est_hours += dt_h
            continue
        per_pos.add(r["position_id"])
        net_row = sum(float(r[c] or 0.0) for c in CATS)
        for c in CATS:
            actual[c] += float(r[c] or 0.0)
        sell_row = net_row - float(r["haircut"] or 0.0) + float(r["haircut_sell"] or 0.0)
        sell_haircut += float(r["haircut_sell"] or 0.0)
        obs_hours += dt_h
        pred = json.loads(r["predicted_json"] or "{}")
        pred_row = 0.0
        for c, (key, sign) in PRED_KEYS.items():
            v = sign * float(pred.get(key) or 0.0) * dt_h / 24
            predicted[c] += v
            pred_row += v
        # すぐ売る前提の予測（スコアの参考値。なければ持ち続ける前提と同じ）
        ns = pred.get("net_sell_now")
        pred_sell_row = float(ns) * dt_h / 24 if ns is not None else pred_row
        pred_sell_net += pred_sell_row
        live = live_per_day(conn, r["pool_id"], r["ts"], r_now[r["position_id"]], r["mode"], ref_cache) if dt_h else None
        ref_row = live["net"] * dt_h / 24 if live is not None else None
        if ref_row is not None:
            ref_net += ref_row
            ref_hours += dt_h
        k = int((datetime.fromisoformat(r["ts"]) - start_t).total_seconds() // 86400)
        if 0 <= k < n_days:
            day = daily[k]
            if ref_row is not None:
                day["ref"] += ref_row
                day["ref_hours"] += dt_h
            day["pred"] += pred_row
            day["pred_sell"] += pred_sell_row
            day["hold"] += net_row
            day["sell"] += sell_row
            day["hours"] += dt_h
            day["capital"][r["position_id"]] = float(r["capital"] or 0.0)
    days = obs_hours / 24
    per_day = (lambda v: v / days) if days > 0 else (lambda v: None)
    compare = [{"key": c, "label": views.CATEGORY_JA[c], "predicted": per_day(predicted[c]), "actual": per_day(actual[c])}
               for c in CATS]
    pred_net, act_net = sum(predicted.values()), sum(actual.values())
    sell_net = act_net - actual["haircut"] + sell_haircut
    events = [{"level": r["level"], "level_ja": LEVEL_JA.get(r["level"], r["level"]), "n": r["n"]}
              for r in conn.execute("SELECT level, COUNT(*) AS n FROM risk_events WHERE ts>? AND ts<=? "
                                    "GROUP BY level ORDER BY n DESC", (_iso(start_t), _iso(until)))]

    # 1日ごとの判定（2026-09-29 オーナー決定）
    done_days = min(n_days, int(hours // 24))           # 24時間が終わった日だけ判定する

    def day_ok(pred: float, act: float, capital: float) -> bool:
        gap = abs(act - pred)
        return gap <= abs(pred) * ev.day_gap_pct / 100 or gap <= capital * ev.day_gap_capital_pct / 100

    flips = _flip_times(conn, config, start_t, end_t)
    day_rows = []
    for k, day in enumerate(daily[:max(done_days, min(n_days, int(math.ceil(hours / 24))))]):
        cap = sum(day["capital"].values())
        has = day["hours"] > 0
        has_ref = has and day["ref_hours"] >= day["hours"] - 1e-9
        d0, d1 = start_t + timedelta(days=k), start_t + timedelta(days=k + 1)
        day_rows.append({
            "day": k + 1, "start": _iso(start_t + timedelta(days=k)), "done": k < done_days,
            "hours": day["hours"], "capital": cap, "predicted": day["pred"] if has else None,
            "predicted_sell": day["pred_sell"] if has else None,
            "hold": day["hold"] if has else None, "sell": day["sell"] if has else None,
            "hold_ok": has and day_ok(day["pred"], day["hold"], cap),
            "sell_ok": has and day_ok(day["pred_sell"], day["sell"], cap),
            # 参考（合否には使わない）: その時点の見込みと比べた結果。切り替えのあった日に印
            "reference": day["ref"] if has_ref else None,
            "reference_ok": (has_ref and day_ok(day["ref"], day["hold"], cap)) if has_ref else None,
            "flip": any(d0 <= f < d1 for f in flips),
        })
    need = math.ceil(n_days * ev.pass_days_pct / 100)
    cov_ratios = [c["ratio"] for c in coverage if c["ratio"] is not None]
    cov = min(cov_ratios) if cov_ratios else None
    cov_ok = cov is not None and cov * 100 >= ev.min_coverage_pct

    def verdict(key: str) -> dict[str, Any]:
        ok_days = sum(1 for d in day_rows if d["done"] and d[f"{key}_ok"])
        if state == "finished":
            result = "pass" if cov_ok and ok_days >= need else "fail"
        elif state in ("stopped", "interrupted"):
            result = state
        else:
            result = "running"
        return {"ok_days": ok_days, "need_days": need, "result": result}

    done_rows = [d for d in day_rows if d["done"]]
    reference = {
        "ok_days": sum(1 for d in done_rows if d["reference_ok"]),
        "need_days": need,
        "differs_days": [d["day"] for d in done_rows if d["reference_ok"] is not None and d["reference_ok"] != d["hold_ok"]],
        "flip_days": [d["day"] for d in day_rows if d["flip"]],
        "net_day": (ref_net / (ref_hours / 24)) if ref_hours > 0 else None,
        "note": ("参考です。合否は、始めたときの見込みとの比較で決めます（変わりません）。"
                 "こちらは15分ごとの記録を、その時点の最新のスコア（木曜の切り替えのあとは、ボーナスが変わったあとの見込み）と比べたものです。"
                 "始めたときの見込みでは外れて、こちらでは当たっている日は、外れた理由が切り替えだった可能性が高い日です。"),
    }
    criteria = {
        "min_coverage_pct": ev.min_coverage_pct, "coverage_pct": cov * 100 if cov is not None else None,
        "coverage_ok": cov_ok, "day_gap_pct": ev.day_gap_pct, "day_gap_capital_pct": ev.day_gap_capital_pct,
        "pass_days_pct": ev.pass_days_pct, "days": n_days, "done_days": done_days,
        "hold": verdict("hold"), "sell": verdict("sell"),
    }
    # 評価の期間に閉じた建玉（2026-10-01 オーナー決定 C: 閉じた建玉はそこで記録を終え、評価は残りで続ける）
    closed = [{"id": r["id"], "pair": f"{r['s0']}/{r['s1']}", "closed_at": r["closed_at"],
               "reason": r["close_reason"], "reason_ja": close_reason_ja(r["close_reason"])}
              for r in conn.execute(
                  """SELECT p.id, p.closed_at, p.close_reason, pl.token0_symbol AS s0, pl.token1_symbol AS s1
                     FROM positions p LEFT JOIN pools pl ON pl.id=p.pool_id
                     WHERE p.is_paper=1 AND p.closed_at>? AND p.closed_at<=? ORDER BY p.closed_at""",
                  (_iso(start_t), _iso(until)))]
    open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='open'").fetchone()[0]
    interrupted = None
    if state == "interrupted":
        note = json.loads(cur["note"] or "{}") if "note" in cur.keys() else {}
        interrupted = {"at": cur["ends_at"], "last_pair": note.get("last_pair"),
                       "reason_ja": close_reason_ja(note.get("reason")),
                       "message": (f"評価の建玉が全部閉じたので、評価は中断しました（最後に閉じたのは {note.get('last_pair') or '—'}、"
                                   f"理由: {close_reason_ja(note.get('reason')) or '—'}）。合否は出しません。"
                                   "もう一度始めるときは、練習を開いてから「評価を始める」を押します。")}
    return {
        **base, "state": state, "started_at": cur["started_at"], "ends_at": cur["ends_at"],
        "closed_positions": closed, "open_positions": open_n, "interrupted": interrupted,
        "elapsed_hours": hours, "left_hours": max(0.0, (end_t - now).total_seconds() / 3600) if state == "running" else 0,
        "coverage": coverage, "positions": len(per_pos),
        "observed_hours": obs_hours, "estimated_hours": est_hours,
        "compare": compare,
        "predicted_net_day": per_day(pred_net), "actual_net_day": per_day(act_net),
        "gap_pct": (act_net / pred_net - 1) * 100 if pred_net else None,
        "hold_net_day": per_day(act_net), "sell_net_day": per_day(sell_net),
        "predicted_sell_net_day": per_day(pred_sell_net),
        "closer": (None if not days else
                   ("hold" if abs(act_net - pred_net) <= abs(sell_net - pred_sell_net) else "sell")),
        "events": events, "criteria": criteria, "days": day_rows, "reference": reference,
        "disclaimer": "この評価は予測が当たるかの確認で、儲かるかの判定ではありません。",
        "note": ("予測はスコア（始めた時の1日の見込み）を、実際に記録した時間の分だけ足したもの。実績は同じ時間の6区分の合計。"
                 "パソコンが止まっていた時間（推定）と、開く・閉じる時の1回きりの費用は比べる対象から外しています。"
                 "1日は評価を始めた時刻から24時間ずつ区切ります。記録のない日は「満たさない日」に数えます。"),
    }


def _flip_times(conn: sqlite3.Connection, config: Config, start: datetime, end: datetime) -> list[datetime]:
    """評価の期間の中の、木曜の切り替えの時刻（評価の建玉がある会場の設定から）。"""
    out: set[datetime] = set()
    for vid in evaluation_venues(conn, config, start, end):
        try:
            epoch = (load_venue(vid, config.root).get("mechanics") or {}).get("epoch") or {}
        except (OSError, ConfigError):
            continue
        length = int(epoch.get("length_seconds") or 0)
        if not length:
            continue
        t = views.next_epoch_flip(start, length, int(epoch.get("offset_seconds") or 0))
        while t < end:
            out.add(t)
            t += timedelta(seconds=length)
    return sorted(out)
