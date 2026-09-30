"""木曜の切り替え（エポック）のあとの見込み（2026-09-30 オーナー決定②③）。

- ② 評価の参考: 始めたときの見込みではなく、その時点の最新のスコア（切り替えのあとはボーナスが変わったあと）で比べる
- ③ ボーナスが減ったときの比べ方: 次の切り替えまでの見込みで「そのまま／ステークをやめて手数料／抜ける」を比べる

どちらも「その建玉の形」（開いた時に決めた mode と、今のレンジ幅）に合わせて、スコアのレンジ幅ごとの行から1日の見込みを作る。
スコアの行は、ステークするかどうかをその時の有利な方で選んでいるので、建玉の mode と違うときは作り直す。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# 1日の見込みの内訳（スコアの行のキー）。income だけプラス、ほかは引く
COST_KEYS = ("gamma", "rebalance", "hedge", "haircut", "direction_risk")


def nearest_range(details: dict[str, Any], r_pct: float) -> dict[str, Any] | None:
    """スコアのレンジ幅ごとの行のうち、r_pct にいちばん近いもの。"""
    rows = [x for x in details.get("ranges") or [] if x.get("r_pct") is not None]
    if not rows:
        return None
    return min(rows, key=lambda x: abs(float(x["r_pct"]) - r_pct))


def per_day_for_mode(row: dict[str, Any], mode: str | None, trend_daily: float | None) -> dict[str, float] | None:
    """スコアの1行を、建玉の mode（staked / unstaked / rewards）での1日の見込みに直す。できなければ None。

    ステークしない（unstaked）ときは報酬トークンを持たないので、値下がり（haircut）は0。
    ステークする（staked / rewards）ときの値下がりは、スコアと同じく 収入 × 報酬トークンの1日の下落率。
    """
    staked = float(row.get("income_staked") or 0.0)
    unstaked = row.get("income_unstaked")
    if mode == "unstaked":
        if unstaked is None:
            return None
        income, haircut = float(unstaked), 0.0
    else:
        income = staked
        haircut = staked * -trend_daily if trend_daily is not None and trend_daily < 0 else 0.0
    out = {"income": income, "haircut": haircut,
           **{k: float(row.get(k) or 0.0) for k in COST_KEYS if k != "haircut"}}
    out["net"] = out["income"] - sum(out[k] for k in COST_KEYS)
    return out


def score_inputs(score: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    details = json.loads(score["details_json"] or "{}")
    return details, details.get("inputs") or {}


def live_per_day(conn: sqlite3.Connection, pool_id: str, at: str, r_pct: float, mode: str | None,
                 cache: dict[tuple, Any] | None = None) -> dict[str, float] | None:
    """時刻 at の時点の最新のスコアで作った、この建玉の形での1日の見込み（なければ None）。

    cache を渡すと、同じスコアの読み直しをしない（評価の集計は15分ごとの行の数だけ呼ぶため）。
    """
    row = conn.execute("SELECT ts FROM scores WHERE pool_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                       (pool_id, at)).fetchone()
    if row is None:
        return None
    key = (pool_id, row["ts"], round(r_pct, 6), mode)
    if cache is not None and key in cache:
        return cache[key]
    score = conn.execute("SELECT details_json FROM scores WHERE pool_id=? AND ts=?", (pool_id, row["ts"])).fetchone()
    details, inp = score_inputs(score)
    rr = nearest_range(details, r_pct)
    out = per_day_for_mode(rr, mode, inp.get("reward_token_trend_daily")) if rr else None
    if cache is not None:
        cache[key] = out
    return out
