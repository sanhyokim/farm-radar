"""米国市場の休日・短縮取引日（M5d）のテスト。"""

from datetime import UTC, date, datetime

from farm_radar import market_calendar as mc
from farm_radar.scheduler import in_fast_window
from farm_radar.scoring.volatility import us_market_open


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=mc.NY).timestamp())


def test_regular_day_and_weekend():
    assert us_market_open(_ts(2026, 9, 29, 10, 0))
    assert not us_market_open(_ts(2026, 9, 29, 16, 0))
    assert not us_market_open(_ts(2026, 10, 3, 11, 0))              # 土曜


def test_holidays_and_early_close_from_nyse_page():
    assert not us_market_open(_ts(2026, 11, 26, 11, 0))            # 感謝祭
    assert mc.day_info(date(2026, 11, 26))["holiday"] == "感謝祭"
    assert us_market_open(_ts(2026, 11, 27, 12, 30))               # 翌日は 13:00 まで
    assert not us_market_open(_ts(2026, 11, 27, 13, 30))
    assert not us_market_open(_ts(2026, 12, 25, 11, 0))            # クリスマス
    assert not us_market_open(_ts(2026, 7, 3, 11, 0))              # 独立記念日（振替）
    assert us_market_open(_ts(2028, 1, 3, 11, 0))                  # 2028-01-01 は土曜で振替なし（公式の注記）


def test_uncovered_year_falls_back_to_weekdays():
    info = mc.day_info(date(2029, 12, 25))
    assert info["trading"] and not info["covered"]                 # 表にない年は平日だけで判定し、未確認の印


def test_fast_window_skips_us_holidays_and_weekends():
    w = ("22:00", "23:30")
    assert in_fast_window(datetime(2026, 9, 29, 13, 5, tzinfo=UTC), w)          # 火曜 22:05 JST
    assert not in_fast_window(datetime(2026, 11, 26, 13, 30, tzinfo=UTC), w)    # 感謝祭の日（NY 8:30）
    assert not in_fast_window(datetime(2026, 10, 3, 13, 30, tzinfo=UTC), w)     # 土曜
    assert in_fast_window(datetime(2026, 11, 26, 13, 30, tzinfo=UTC), w, trading_days_only=False)


def test_status_for_home_screen():
    s = mc.status(datetime(2026, 11, 27, 15, 0, tzinfo=UTC))       # NY 10:00 の短縮取引日
    assert s["open"] and s["early_close"] == "13:00" and s["calendar_covered"]
    assert s["source_url"].startswith("https://www.nyse.com/")
