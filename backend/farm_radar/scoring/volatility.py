"""値動きの大きさ σ（日次の実現ボラティリティ）の計算（SPEC 3.1章）。

- 1時間ごとの終値から、1時間ごとの対数の変化率を出し、その2乗平均の平方根 × √24 を「1日あたり」とする。
- 取引がなかった時間は、直前の値を引き継ぐ（その1時間の変化は0）。
- 株トークンは、米国市場の時間中（ニューヨーク 9:30〜16:00、平日）と時間外で分けても計算する。
  どちらも「その状態が1日続いたとしたら」の値（1時間の値 × √24）で表す。祝日は考えない。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

HOUR = 3600
NY = ZoneInfo("America/New_York")


def hourly_grid(points: list[tuple[int, float]], start: int, end: int) -> list[tuple[int, float]]:
    """(UNIX秒, 価格) の並び（古い順）を、毎時0分の値に並べ直す。値はその時刻までの最後の値。"""
    pts = sorted((t, v) for t, v in points if v and v > 0 and math.isfinite(v))
    out: list[tuple[int, float]] = []
    h = start - start % HOUR
    i, last = 0, None
    while h <= end:
        while i < len(pts) and pts[i][0] <= h:
            last = pts[i][1]
            i += 1
        if last is not None:
            out.append((h, last))
        h += HOUR
    return out


def ratio_grid(a: list[tuple[int, float]], b: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """2つの毎時の値から、同じ時刻の比率 a/b を作る（2つのトークンの比率の値動き用）。"""
    bm = dict(b)
    return [(t, v / bm[t]) for t, v in a if t in bm and bm[t] > 0]


def hourly_returns(grid: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """(その1時間の終わりの時刻, 対数の変化率)。"""
    return [(t1, math.log(v1 / v0)) for (_, v0), (t1, v1) in zip(grid, grid[1:])]


def daily_sigma(returns: list[float], min_count: int = 24) -> float | None:
    if len(returns) < min_count:
        return None
    return math.sqrt(sum(r * r for r in returns) / len(returns)) * math.sqrt(24)


def us_market_open(ts: int) -> bool:
    """その時刻に米国の株式市場が開いているか（平日 9:30〜16:00 ニューヨーク時間。夏時間は自動）。"""
    t = datetime.fromtimestamp(ts, UTC).astimezone(NY)
    return t.weekday() < 5 and time(9, 30) <= t.time() < time(16, 0)


def split_sigma(returns: list[tuple[int, float]], min_count: int = 6) -> tuple[float | None, float | None]:
    """(市場時間中の σ, 時間外の σ)。1時間の区間の真ん中の時刻で分ける。"""
    op = [r for t, r in returns if us_market_open(t - HOUR // 2)]
    cl = [r for t, r in returns if not us_market_open(t - HOUR // 2)]
    return daily_sigma(op, min_count), daily_sigma(cl, min_count)


def covers(points: list[tuple[int, float]], now: int, days: float, slack_hours: int = 2) -> bool:
    """いちばん古い記録が「days 日前」より前にあるか（自分の記録で足りるかの判定）。"""
    return bool(points) and min(t for t, _ in points) <= now - int(days * 86400) + slack_hours * HOUR
