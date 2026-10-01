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
2026-10-01 オーナー提案（【3】案B）: 参考の練習（positions.purpose = 'reference'）は合否に入れない。参考は建玉ごとに、
開いた時刻から24時間ずつ区切って、同じ基準で「満たす日」を数える（reference_tracks）。
2026-10-01 オーナー提案（【2】3。evaluation.weekly_prediction）: 木曜の切り替えのあとは「その週の見込み」で比べる。
その週の見込みは、切り替えの rewards.epoch_fresh_minutes 後（ボーナスの値が落ち着いたあと）の最初のスコアを、建玉の幅と mode で読んだもの。
始めたときの見込みで比べた結果も、参考として並べる。
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
from .flip import live_per_day, week_per_day
from .paper import CATS, OFFICIAL_SQL, REFERENCE, official_sql
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
    if conn.execute(f"SELECT 1 FROM positions WHERE is_paper=1 AND status='open' AND {OFFICIAL_SQL} LIMIT 1"
                    ).fetchone() is None:
        raise ValueError("練習の建玉が1つもないので、評価を始められません。先に練習を開いてください（2026-10-01 の決まり）。"
                         "参考の練習は評価に入りません。")
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
        f"""SELECT DISTINCT venue_id FROM positions WHERE is_paper=1 AND opened_at<=?
           AND (closed_at IS NULL OR closed_at>=?) AND {OFFICIAL_SQL} ORDER BY venue_id""",
        (_iso(until), _iso(start))).fetchall()
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


class _Predictor:
    """1行ごとの1日の見込み。始めたときの見込み（建玉の predicted_json）と、切り替えのあとの「その週の見込み」。"""

    def __init__(self, conn: sqlite3.Connection, config: Config):
        self.conn, self.config = conn, config
        self.cache: dict = {}
        self.epochs: dict[str, tuple[int, int] | None] = {}
        self.settle = timedelta(minutes=config.epoch_fresh_minutes)

    def _epoch(self, venue_id: str | None) -> tuple[int, int] | None:
        if not venue_id:
            return None
        if venue_id not in self.epochs:
            try:
                epoch = (load_venue(venue_id, self.config.root).get("mechanics") or {}).get("epoch") or {}
            except (OSError, ConfigError):
                epoch = {}
            length = int(epoch.get("length_seconds") or 0)
            self.epochs[venue_id] = (length, int(epoch.get("offset_seconds") or 0)) if length else None
        return self.epochs[venue_id]

    def flips_between(self, venue_id: str | None, start: datetime, end: datetime) -> list[datetime]:
        """start より後で end より前の、木曜の切り替えの時刻（古い順）。
        記録の行はその時刻までの15分の分なので、ちょうど切り替えの時刻の行は前の週に入れる。"""
        ep = self._epoch(venue_id)
        if ep is None:
            return []
        out, t = [], views.next_epoch_flip(start, ep[0], ep[1])
        while t < end:
            out.append(t)
            t += timedelta(seconds=ep[0])
        return out

    @staticmethod
    def cats(src: dict[str, Any]) -> dict[str, float]:
        """見込みの値（スコアの行の名前）を、損益の6区分（収入はプラス、ほかはマイナス）に直す。"""
        return {c: sign * float(src.get(key) or 0.0) for c, (key, sign) in PRED_KEYS.items()}

    def week(self, pool_id: str, venue_id: str | None, opened_at: datetime, at: datetime, r_pct: float,
             mode: str | None) -> dict[str, Any] | None:
        """at の時点で使う「その週の見込み」。開いたあとに切り替えがなければ None（始めたときの見込みを使う）。
        いちばん新しい切り替えの見込みがまだ読めない（落ち着く前）ときは、1つ前の切り替えの見込みを使う。"""
        for f in reversed(self.flips_between(venue_id, opened_at, at)):
            key = ("pred", pool_id, f, round(r_pct, 6), mode)
            if key not in self.cache:       # 1回の集計の中だけ覚えておく（15分ごとの行の数だけ呼ばれるため）
                self.cache[key] = week_per_day(self.conn, pool_id, _iso(f), _iso(f + self.settle), r_pct, mode,
                                               self.cache)
            if self.cache[key] is not None:
                return self.cache[key]
        return None


def _jst(t: datetime) -> str:
    j = t.astimezone(views.JST)
    return f"{j:%Y-%m-%d %H:%M}（日本時間）"


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
    # 合否に使うのは参考の練習でない建玉だけ（2026-10-01 案B）
    rows = conn.execute(f"""SELECT n.*, p.predicted_json, p.capital, p.pool_id, p.mode, p.venue_id, p.opened_at
                            FROM position_pnl n JOIN positions p ON p.id=n.position_id
                            WHERE p.is_paper=1 AND n.ts>? AND n.ts<=? AND {official_sql('p.')}
                            ORDER BY n.position_id, n.ts""",
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
              "ref": 0.0, "ref_hours": 0.0, "pred_start": 0.0, "pred_sell_start": 0.0, "week_hours": 0.0}
             for _ in range(n_days)]
    weekly = ev.weekly_prediction
    pr = _Predictor(conn, config)
    week_used: dict[str, str] = {}      # 使った「その週の見込み」: 切り替えの時刻 → スコアの時刻
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
        start_cats = pr.cats(pred)
        pred_start_row = sum(start_cats.values()) * dt_h / 24
        # すぐ売る前提の予測（スコアの参考値。なければ持ち続ける前提と同じ）
        ns = pred.get("net_sell_now")
        pred_sell_start_row = float(ns) * dt_h / 24 if ns is not None else pred_start_row
        # 切り替えのあとは「その週の見込み」（evaluation.weekly_prediction。2026-10-01 オーナー提案【2】3）
        wk = (pr.week(r["pool_id"], r["venue_id"], datetime.fromisoformat(r["opened_at"]),
                      datetime.fromisoformat(r["ts"]), r_now[r["position_id"]], r["mode"])
              if weekly and dt_h else None)
        if wk is not None:
            week_used[wk["flip_at"]] = wk["score_ts"]
            cats = pr.cats(wk)
            pred_row = sum(cats.values()) * dt_h / 24
            pred_sell_row = float(wk["net_sell"]) * dt_h / 24
        else:
            cats, pred_row, pred_sell_row = start_cats, pred_start_row, pred_sell_start_row
        for c in CATS:
            predicted[c] += cats.get(c, 0.0) * dt_h / 24
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
            day["pred_start"] += pred_start_row
            day["pred_sell_start"] += pred_sell_start_row
            if wk is not None:
                day["week_hours"] += dt_h
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
    # 見張りの記録の件数（参考の練習の記録は数えない。2026-10-01 案B）
    events = [{"level": r["level"], "level_ja": LEVEL_JA.get(r["level"], r["level"]), "n": r["n"]}
              for r in conn.execute(
                  f"""SELECT e.level, COUNT(*) AS n FROM risk_events e LEFT JOIN positions p ON p.id=e.position_id
                      WHERE e.ts>? AND e.ts<=? AND (e.position_id IS NULL OR {official_sql('p.')})
                        AND e.action <> 'closed_reference'
                      GROUP BY e.level ORDER BY n DESC""", (_iso(start_t), _iso(until)))]

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
            # 週ごとの見込みを使った時間（0 なら始めたときの見込みだけ）と、始めたときの見込みで比べた結果（参考）
            "week_hours": day["week_hours"],
            "predicted_start": day["pred_start"] if has else None,
            "start_ok": has and day_ok(day["pred_start"], day["hold"], cap),
            "start_sell_ok": has and day_ok(day["pred_sell_start"], day["sell"], cap),
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
        "note": (("参考です。合否は、週ごとの見込み（切り替えのあとはその週の見込み）との比較で決めます。" if weekly else
                  "参考です。合否は、始めたときの見込みとの比較で決めます（変わりません）。")
                 + "こちらは15分ごとの記録を、その時点の最新のスコア（木曜の切り替えのあとは、ボーナスが変わったあとの見込み）と比べたものです。"
                 + ("合否の見込みでは外れて、こちらでは当たっている日は、外れた理由がその日のうちの見込みの変化だった可能性が高い日です。"
                    if weekly else
                    "始めたときの見込みでは外れて、こちらでは当たっている日は、外れた理由が切り替えだった可能性が高い日です。")),
    }
    criteria = {
        "min_coverage_pct": ev.min_coverage_pct, "coverage_pct": cov * 100 if cov is not None else None,
        "coverage_ok": cov_ok, "day_gap_pct": ev.day_gap_pct, "day_gap_capital_pct": ev.day_gap_capital_pct,
        "pass_days_pct": ev.pass_days_pct, "days": n_days, "done_days": done_days,
        "hold": verdict("hold"), "sell": verdict("sell"),
        # 合否の比べる相手（2026-10-01 オーナー提案【2】3）。weekly なら切り替えのあとはその週の見込み
        "prediction": "weekly" if weekly else "start",
        # 参考: 始めたときの見込みだけで比べた結果（weekly のときに並べる。合否には使わない）
        "start_only": {"hold": verdict("start"), "sell": verdict("start_sell")} if weekly else None,
    }
    # 評価の期間に閉じた建玉（2026-10-01 オーナー決定 C: 閉じた建玉はそこで記録を終え、評価は残りで続ける）
    closed = [{"id": r["id"], "pair": f"{r['s0']}/{r['s1']}", "closed_at": r["closed_at"],
               "reason": r["close_reason"], "reason_ja": close_reason_ja(r["close_reason"])}
              for r in conn.execute(
                  f"""SELECT p.id, p.closed_at, p.close_reason, pl.token0_symbol AS s0, pl.token1_symbol AS s1
                      FROM positions p LEFT JOIN pools pl ON pl.id=p.pool_id
                      WHERE p.is_paper=1 AND p.closed_at>? AND p.closed_at<=? AND {official_sql('p.')}
                      ORDER BY p.closed_at""",
                  (_iso(start_t), _iso(until)))]
    open_n = conn.execute(f"SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='open' AND {OFFICIAL_SQL}"
                          ).fetchone()[0]
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
        # コマンドで確かめやすいように日本時間の文字も返す（2026-10-01 オーナー依頼3）
        "started_jst": _jst(start_t), "ends_jst": _jst(end_t),
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
        # 合否に使った「その週の見込み」（切り替えの時刻と、読んだスコアの時刻）
        "weekly": {"enabled": weekly, "used": [{"flip_at": f, "score_ts": t} for f, t in sorted(week_used.items())],
                   "note": ("木曜の切り替えのあとは、ボーナスの値が落ち着いたあと（切り替えの"
                            f"{config.epoch_fresh_minutes // 60}時間後）の最初のスコアを、その週の見込みにして比べます。"
                            "始めたときの見込みだけで比べた結果も、参考として並べます。") if weekly else None},
        "reference_tracks": reference_tracks(conn, config, now),
        "disclaimer": "この評価は予測が当たるかの確認で、儲かるかの判定ではありません。",
        "note": (("予測はスコア（始めた時の1日の見込み。木曜の切り替えのあとはその週の見込み）を、"
                  if weekly else "予測はスコア（始めた時の1日の見込み）を、")
                 + "実際に記録した時間の分だけ足したもの。実績は同じ時間の6区分の合計。"
                 "パソコンが止まっていた時間（推定）と、開く・閉じる時の1回きりの費用は比べる対象から外しています。"
                 "1日は評価を始めた時刻から24時間ずつ区切ります。記録のない日は「満たさない日」に数えます。"),
    }


def reference_tracks(conn: sqlite3.Connection, config: Config, now: datetime, limit: int = 10) -> dict[str, Any]:
    """参考の練習（2026-10-01 オーナー提案【3】案B）の、建玉ごとの「見込みと実際」。合否には使わない。

    建玉ごとに、開いた時刻から24時間ずつ区切り、評価と同じ基準（±30% か、元手の0.1% 以内）で満たす日を数える。
    比べる見込みは評価と同じ（evaluation.weekly_prediction なら切り替えのあとはその週の見込み）。
    推定の行（パソコンが止まっていた時間）と、開く・閉じる時の1回きりの費用は除く。
    """
    ev = config.evaluation
    n_days = ev.days
    need = math.ceil(n_days * ev.pass_days_pct / 100)
    pr = _Predictor(conn, config)
    weekly = ev.weekly_prediction
    positions = conn.execute(
        """SELECT p.*, pl.token0_symbol AS s0, pl.token1_symbol AS s1 FROM positions p
           LEFT JOIN pools pl ON pl.id=p.pool_id WHERE p.is_paper=1 AND p.purpose=?
           ORDER BY p.status='open' DESC, p.opened_at DESC LIMIT ?""", (REFERENCE, limit)).fetchall()
    tracks = []
    for pos in positions:
        start_t = datetime.fromisoformat(pos["opened_at"])
        end_t = start_t + timedelta(days=n_days)
        stop_t = min(now, end_t, datetime.fromisoformat(pos["closed_at"]) if pos["closed_at"] else now)
        pred = json.loads(pos["predicted_json"] or "{}")
        start_net = sum(pr.cats(pred).values())
        r_now = float(pred.get("r_pct") or (pos["r"] or 0) * 100)
        days = [{"pred": 0.0, "hold": 0.0, "hours": 0.0} for _ in range(n_days)]
        pred_sum = act_sum = obs_h = est_h = 0.0
        for r in conn.execute("SELECT * FROM position_pnl WHERE position_id=? AND ts>? AND ts<=? ORDER BY ts",
                              (pos["id"], _iso(start_t), _iso(stop_t))):
            d = json.loads(r["detail_json"] or "{}")
            if d.get("event") == "rebalance" and d.get("r_pct") is not None:
                r_now = float(d["r_pct"])
            if d.get("event") in ("open", "close"):
                continue
            dt_h = float(d.get("dt_s") or 0.0) / 3600
            if r["is_estimated"]:
                est_h += dt_h
                continue
            wk = (pr.week(pos["pool_id"], pos["venue_id"], start_t, datetime.fromisoformat(r["ts"]), r_now, pos["mode"])
                  if weekly and dt_h else None)
            p_row = (sum(pr.cats(wk).values()) if wk is not None else start_net) * dt_h / 24
            a_row = sum(float(r[c] or 0.0) for c in CATS)
            pred_sum += p_row
            act_sum += a_row
            obs_h += dt_h
            k = int((datetime.fromisoformat(r["ts"]) - start_t).total_seconds() // 86400)
            if 0 <= k < n_days:
                days[k]["pred"] += p_row
                days[k]["hold"] += a_row
                days[k]["hours"] += dt_h
        hours = max(0.0, (stop_t - start_t).total_seconds() / 3600)
        done_days = min(n_days, int(hours // 24))
        cap = float(pos["capital"] or 0.0)

        def ok(day: dict[str, float]) -> bool:
            gap = abs(day["hold"] - day["pred"])
            return day["hours"] > 0 and (gap <= abs(day["pred"]) * ev.day_gap_pct / 100
                                         or gap <= cap * ev.day_gap_capital_pct / 100)

        rows = [{"day": k + 1, "done": k < done_days, "hours": d["hours"],
                 "predicted": d["pred"] if d["hours"] > 0 else None, "hold": d["hold"] if d["hours"] > 0 else None,
                 "ok": ok(d)} for k, d in enumerate(days[:max(done_days, min(n_days, math.ceil(hours / 24)))])]
        per_day = (lambda v: v / (obs_h / 24)) if obs_h > 0 else (lambda v: None)
        tracks.append({
            "id": pos["id"], "pool_id": pos["pool_id"], "pair": f"{pos['s0']}/{pos['s1']}", "status": pos["status"],
            "opened_at": pos["opened_at"], "closed_at": pos["closed_at"],
            "close_reason_ja": close_reason_ja(pos["close_reason"]) if pos["closed_at"] else None,
            "mode": pos["mode"], "capital": cap, "ends_at": _iso(end_t),
            "done_days": done_days, "ok_days": sum(1 for x in rows if x["done"] and x["ok"]), "need_days": need,
            "observed_hours": obs_h, "estimated_hours": est_h,
            "predicted_net_day": per_day(pred_sum), "actual_net_day": per_day(act_sum),
            "days": rows,
        })
    return {
        "tracks": tracks,
        "note": ("参考の練習は合否に使いません。種類の違うプールで、見込みが当たるかを並べて確かめるためのものです。"
                 "建玉ごとに、開いた時刻から24時間ずつ区切って、評価と同じ基準で「満たす日」を数えます。"),
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
