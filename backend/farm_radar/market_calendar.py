"""米国の株式市場（NYSE）の営業日と取引時間（M5d）。venues/us-market-calendar.yaml を読む。

- 休日（感謝祭・クリスマスなど）は終日「時間外」、短縮取引日は 13:00 まで「時間中」
- yaml に書いてある年（covered_years）より先は、平日 9:30〜16:00 だけで判定する（covered=False を返す）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .config import REPO_ROOT

NY = ZoneInfo("America/New_York")
_PATH = REPO_ROOT / "venues" / "us-market-calendar.yaml"


@dataclass(frozen=True)
class Calendar:
    holidays: dict[date, str]
    early_closes: dict[date, time]
    years: frozenset[int]
    open_t: time
    close_t: time
    source_url: str
    checked_at: str


def _t(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


@lru_cache(maxsize=4)
def load(path: str | None = None) -> Calendar:
    p = Path(path) if path else _PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return Calendar(
        holidays={date.fromisoformat(str(k)): str(v) for k, v in (raw.get("holidays") or {}).items()},
        early_closes={date.fromisoformat(str(k)): _t(v) for k, v in (raw.get("early_closes") or {}).items()},
        years=frozenset(int(y) for y in raw.get("covered_years") or []),
        open_t=_t(raw.get("regular_open", "09:30")), close_t=_t(raw.get("regular_close", "16:00")),
        source_url=str(raw.get("source_url") or ""), checked_at=str(raw.get("checked_at") or ""),
    )


def day_info(d: date, cal: Calendar | None = None) -> dict:
    """ニューヨークの日付 d の様子: 取引日か、休日の名前、終わる時刻、カレンダーで確認済みの年か。"""
    cal = cal or load()
    covered = d.year in cal.years
    if d.weekday() >= 5:
        return {"trading": False, "holiday": None, "weekend": True, "close": None, "covered": covered}
    if d in cal.holidays:
        return {"trading": False, "holiday": cal.holidays[d], "weekend": False, "close": None, "covered": covered}
    close = cal.early_closes.get(d, cal.close_t)
    return {"trading": True, "holiday": None, "weekend": False, "close": close, "early": d in cal.early_closes,
            "covered": covered}


def is_trading_day(d: date, cal: Calendar | None = None) -> bool:
    return day_info(d, cal)["trading"]


def market_open(ts: int, cal: Calendar | None = None) -> bool:
    """その時刻（UNIX秒）に米国の株式市場が開いているか（休日・短縮取引日・夏時間を考える）。"""
    cal = cal or load()
    t = datetime.fromtimestamp(ts, UTC).astimezone(NY)
    info = day_info(t.date(), cal)
    return info["trading"] and cal.open_t <= t.time() < info["close"]


def status(now: datetime, cal: Calendar | None = None) -> dict:
    """画面用: 今日（ニューヨークの日付）の様子。"""
    cal = cal or load()
    t = now.astimezone(NY)
    info = day_info(t.date(), cal)
    return {"ny_date": t.date().isoformat(), "open": market_open(int(now.timestamp()), cal),
            "trading_day": info["trading"], "holiday": info["holiday"], "weekend": info["weekend"],
            "early_close": info["close"].strftime("%H:%M") if info.get("early") else None,
            "calendar_covered": info["covered"], "source_url": cal.source_url, "checked_at": cal.checked_at}
