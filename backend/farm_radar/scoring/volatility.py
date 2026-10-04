"""値動きの大きさ σ（日次の実現ボラティリティ）の計算（SPEC 3.1章）。

- 1時間ごとの終値から、1時間ごとの対数の変化率を出し、その2乗平均の平方根 × √24 を「1日あたり」とする。
- 取引がなかった時間は、直前の値を引き継ぐ（その1時間の変化は0）。
- 株トークンは、米国市場の時間中（ニューヨーク 9:30〜16:00、平日）と時間外で分けても計算する。
  どちらも「その状態が1日続いたとしたら」の値（1時間の値 × √24）で表す。
  休日・短縮取引日は venues/us-market-calendar.yaml（NYSE の公式ページ）で判定する（M5d）。
"""

from __future__ import annotations

import math
from zoneinfo import ZoneInfo

from .. import market_calendar

HOUR = 3600
NY = ZoneInfo("America/New_York")


def hourly_grid(points: list[tuple[int, float]], start: int, end: int) -> list[tuple[int, float]]:
    """(UNIX秒, 価格) の並び（古い順）を、毎時0分の値に並べ直す。値はその時刻までの最後の値。"""
    return step_grid(points, start, end, HOUR)


def step_grid(points: list[tuple[int, float]], start: int, end: int, step: int) -> list[tuple[int, float]]:
    """hourly_grid と同じことを、step 秒ごとに（N4b: 30日分は4時間ごと）。"""
    pts = sorted((t, v) for t, v in points if v and v > 0 and math.isfinite(v))
    out: list[tuple[int, float]] = []
    h = start - start % step
    i, last = 0, None
    while h <= end:
        while i < len(pts) and pts[i][0] <= h:
            last = pts[i][1]
            i += 1
        if last is not None:
            out.append((h, last))
        h += step
    return out


def ratio_grid(a: list[tuple[int, float]], b: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """2つの毎時の値から、同じ時刻の比率 a/b を作る（2つのトークンの比率の値動き用）。"""
    bm = dict(b)
    return [(t, v / bm[t]) for t, v in a if t in bm and bm[t] > 0]


def hourly_returns(grid: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """(その1時間の終わりの時刻, 対数の変化率)。"""
    return [(t1, math.log(v1 / v0)) for (_, v0), (t1, v1) in zip(grid, grid[1:])]


def daily_sigma(returns: list[float], min_count: int = 24, per_day: float = 24) -> float | None:
    """per_day は1日あたりの区切りの数（1時間ごとなら24、4時間ごとなら6）。"""
    if len(returns) < min_count:
        return None
    return math.sqrt(sum(r * r for r in returns) / len(returns)) * math.sqrt(per_day)


def recent_sigma(returns: list[tuple[int, float]], end: int, hours: int = 24, min_count: int = 12) -> float | None:
    """直近 hours 時間の1時間ごとの変化だけで出した1日あたりの値動き（2026-10-04 オーナー決定 ①A）。
    急に値動きが大きくなったコイン（例: WETH/MOO で 7日 19.3% → 実際 39.5%）に、7日の値より早く追いつくため。"""
    rets = [r for t, r in returns if t > end - hours * HOUR]
    return daily_sigma(rets, min_count=min_count)


def jump_times(grid: list[tuple[int, float]], min_flat: int) -> set[int]:
    """値段が min_flat 秒以上まったく変わらなかったあと、最初に変わった区切りの時刻（N4b。2026-10-03 オーナー）。

    株のコインは、市場が閉まっている間（週末など）は値段が止まり、開いたときに一度に動く（飛び）。
    この飛びは「なめらかに動く」前提の回数・目減りの式では数えきれないので、別に数える。
    """
    out: set[int] = set()
    flat_since: int | None = None
    for (t0, v0), (t1, v1) in zip(grid, grid[1:]):
        if v1 == v0:
            if flat_since is None:
                flat_since = t0
            continue
        if flat_since is not None and t0 - flat_since >= min_flat:
            out.add(t1)
        flat_since = None
    return out


def split_jumps(returns: list[tuple[int, float]], jumps: set[int]) -> tuple[list[float], list[float]]:
    """(なめらかな動き, 飛び)。returns は (その区切りの終わりの時刻, 対数の変化率)。"""
    smooth = [r for t, r in returns if t not in jumps]
    gaps = [r for t, r in returns if t in jumps]
    return smooth, gaps


def us_market_open(ts: int) -> bool:
    """その時刻に米国の株式市場が開いているか（9:30〜16:00 ニューヨーク時間。休日・短縮取引日・夏時間を考える）。"""
    return market_calendar.market_open(ts)


def split_sigma(returns: list[tuple[int, float]], min_count: int = 6) -> tuple[float | None, float | None]:
    """(市場時間中の σ, 時間外の σ)。1時間の区間の真ん中の時刻で分ける。"""
    op = [r for t, r in returns if us_market_open(t - HOUR // 2)]
    cl = [r for t, r in returns if not us_market_open(t - HOUR // 2)]
    return daily_sigma(op, min_count), daily_sigma(cl, min_count)


def covers(points: list[tuple[int, float]], now: int, days: float, slack_hours: int = 2) -> bool:
    """いちばん古い記録が「days 日前」より前にあるか（自分の記録で足りるかの判定）。"""
    return bool(points) and min(t for t, _ in points) <= now - int(days * 86400) + slack_hours * HOUR
