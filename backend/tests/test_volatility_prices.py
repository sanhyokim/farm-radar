import math
from datetime import UTC, datetime

import pytest

from farm_radar.scoring import volatility as vol
from farm_radar.scoring.prices import PoolPrice, Q96, usd_prices

H = 3600


def test_hourly_grid_carries_last_value_forward():
    pts = [(10, 1.0), (3600 + 5, 2.0), (3 * H + 100, 4.0)]
    grid = vol.hourly_grid(pts, 0, 4 * H)
    # 0時は値がまだないので入らない。取引がない2時は直前の値を引き継ぐ
    assert grid == [(H, 1.0), (2 * H, 2.0), (3 * H, 2.0), (4 * H, 4.0)]


def test_daily_sigma_is_rms_hourly_times_sqrt24():
    grid, p = [], 100.0
    for i in range(49):
        grid.append((i * H, p))
        p *= math.exp(0.01 if i % 2 == 0 else -0.01)
    rets = [r for _, r in vol.hourly_returns(grid)]
    assert vol.daily_sigma(rets) == pytest.approx(0.01 * math.sqrt(24))
    assert vol.daily_sigma(rets[:10]) is None   # 少なすぎるときは計算しない


def test_ratio_grid_matches_pool_price():
    a = [(0, 2000.0), (H, 2100.0)]
    b = [(0, 1.0), (H, 1.0)]
    assert vol.ratio_grid(a, b) == [(0, 2000.0), (H, 2100.0)]


def test_us_market_hours_follow_new_york_time():
    # 2026-09-29（火）14:00 UTC = ニューヨーク 10:00（夏時間）→ 開いている
    assert vol.us_market_open(int(datetime(2026, 9, 29, 14, 0, tzinfo=UTC).timestamp()))
    # 13:00 UTC = 9:00 → まだ
    assert not vol.us_market_open(int(datetime(2026, 9, 29, 13, 0, tzinfo=UTC).timestamp()))
    # 冬時間（2026-12-01 火）: 14:30 UTC = 9:30 → 開いている、14:00 UTC → まだ
    assert vol.us_market_open(int(datetime(2026, 12, 1, 14, 30, tzinfo=UTC).timestamp()))
    assert not vol.us_market_open(int(datetime(2026, 12, 1, 14, 0, tzinfo=UTC).timestamp()))
    # 土曜は閉まっている
    assert not vol.us_market_open(int(datetime(2026, 10, 3, 15, 0, tzinfo=UTC).timestamp()))


def test_split_sigma_separates_open_and_closed_hours():
    start = int(datetime(2026, 9, 28, 0, 0, tzinfo=UTC).timestamp())   # 月曜
    rets = []
    for i in range(1, 24 * 5 + 1):
        t = start + i * H
        rets.append((t, 0.02 if vol.us_market_open(t - H // 2) else 0.001))
    so, sc = vol.split_sigma(rets)
    assert so == pytest.approx(0.02 * math.sqrt(24))
    assert sc == pytest.approx(0.001 * math.sqrt(24))


def test_covers_needs_data_older_than_window():
    now = 10 * 86400
    assert vol.covers([(now - 7 * 86400, 1.0), (now, 1.0)], now, 7)
    assert not vol.covers([(now - 86400, 1.0)], now, 7)
    assert not vol.covers([], now, 7)


# --- オンチェーンのドル価格 ------------------------------------------------------------

USDG, WETH, X = "0xusdg", "0xweth", "0xx"


def _pool(t0, t1, d0, d1, price, liq):
    sp = math.sqrt(price * 10 ** (d1 - d0))
    return PoolPrice(t0, t1, d0, d1, price, liq, int(sp * Q96))


def test_usd_prices_walk_from_stablecoin():
    pools = [
        _pool(WETH, USDG, 18, 6, 2500.0, 10 ** 15),      # WETH = $2500
        _pool(X, WETH, 18, 18, 0.001, 10 ** 22),         # X = 0.001 WETH = $2.5
    ]
    p = usd_prices(pools, frozenset({USDG}))
    assert p[USDG] == 1.0
    assert p[WETH] == pytest.approx(2500)
    assert p[X] == pytest.approx(2.5)


def test_usd_prices_prefer_deeper_pool():
    pools = [
        _pool(X, USDG, 18, 6, 3.0, 10 ** 10),            # とても薄いプール（おかしな価格）
        _pool(X, USDG, 18, 6, 2.0, 10 ** 16),            # 厚いプール
    ]
    assert usd_prices(pools, frozenset({USDG}))[X] == pytest.approx(2.0)


def test_token_without_path_has_no_price():
    pools = [_pool("0xa", "0xb", 18, 18, 1.0, 10 ** 20)]
    assert usd_prices(pools, frozenset({USDG})) == {USDG: 1.0}


# --- N4b: 市場が閉まっていたあとの飛び（2026-10-03 オーナーの質問1） ---------------------------------

def test_jump_after_a_flat_stretch_is_found_and_split_from_smooth_moves():
    h = 3600
    grid = [(0, 100.0), (h, 101.0)] + [(h * k, 101.0) for k in range(2, 10)] + [(h * 10, 105.0), (h * 11, 105.5)]
    jt = vol.jump_times(grid, 6 * h)
    assert jt == {h * 10}                                  # 8時間止まっていたあと
    smooth, gaps = vol.split_jumps(vol.hourly_returns(grid), jt)
    assert gaps == [pytest.approx(math.log(105 / 101))]
    assert len(smooth) == len(grid) - 2
    assert vol.jump_times(grid, 12 * h) == set()          # 止まっていた時間が短ければ飛びではない


def test_daily_sigma_for_four_hour_steps():
    assert vol.daily_sigma([0.01] * 30, per_day=6) == pytest.approx(0.01 * math.sqrt(6))


def test_jump_loss_follows_gamma_inside_the_range_and_grows_linearly_outside():
    from farm_radar.scoring import model as m

    c, r = 1000.0, 0.01
    assert m.jump_loss(c, 0.005, r) == pytest.approx(c * 0.005 ** 2 / (4 * r))
    at_edge = m.jump_loss(c, r, r)
    assert at_edge == pytest.approx(c * r / 4)
    assert m.jump_loss(c, -0.03, r) == pytest.approx(at_edge + c * 0.02 / 2)
    assert m.jump_loss(c, 0.03, r) < c * 0.03 ** 2 / (4 * r)   # 大きな飛びを 2乗で数えすぎない
