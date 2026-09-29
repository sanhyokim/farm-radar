import math

import pytest

from farm_radar.scoring.judge import NO_HEDGE_TEXT, SignalParams, Warn, judge
from farm_radar.scoring.model import (
    ModelParams, PoolInputs, TokenSide, direction_risk, evaluate, gamma, in_range_ratio,
    in_range_ratio_hold, liquidity_for_usd, rebalances_per_day, sell_now_drop,
)


# --- レンジ内の時間の割合 -------------------------------------------------------------

# 2026-09-29 にオーナーへ出した表（置きっぱなしの参考値）。オーナーが「表の数値は正しい」と確認済み
HOLD_TABLE = {
    0.01: {0.005: 58, 0.01: 85, 0.02: 99, 0.03: 100, 0.05: 100, 0.10: 100, 0.15: 100},
    0.03: {0.005: 24, 0.01: 43, 0.02: 69, 0.03: 84, 0.05: 97, 0.10: 100, 0.15: 100},
    0.05: {0.005: 15, 0.01: 28, 0.02: 49, 0.03: 65, 0.05: 84, 0.10: 98, 0.15: 100},
}


@pytest.mark.parametrize("sigma", sorted(HOLD_TABLE))
def test_hold_ratio_matches_table_shown_to_owner(sigma):
    for r, want in HOLD_TABLE[sigma].items():
        assert round(in_range_ratio_hold(sigma, r) * 100) == pytest.approx(want, abs=1), (sigma, r)


def test_rebalance_ratio_uses_waiting_time_only():
    # σ 3%、±1%: 1日 9回外れる × 15分待つ = 135分 → 1 − 135/1440
    assert rebalances_per_day(0.03, 0.01) == pytest.approx(9)
    assert in_range_ratio(0.03, 0.01, 15) == pytest.approx(1 - 135 / 1440)
    # 待ち時間が長くて1日を超える計算になっても、0より小さくならない
    assert in_range_ratio(0.05, 0.005, 15) == 0.0
    assert in_range_ratio(0.0, 0.01, 15) == 1.0


# --- 値動きの損・ガンマ ----------------------------------------------------------------

def test_direction_risk_formula_approved_by_owner():
    # C_lp $550、片方がステーブル（σ=0）、もう片方 σ 3% → 550 × 0.5 × 0.4 × 0.03 = $3.30/日
    assert direction_risk(550, [0.03, 0.0]) == pytest.approx(3.30)
    # 両方とも値動きする: それぞれのドル建ての σ を足す
    assert direction_risk(550, [0.03, 0.05]) == pytest.approx(550 * 0.5 * 0.4 * 0.08)


def test_gamma_formula():
    assert gamma(550, 0.03, 0.01) == pytest.approx(550 * 0.0009 / 0.04)


# --- v3 の流動性 -----------------------------------------------------------------------

def _amounts(L, price, r, d0, d1):
    p = price * 10 ** (d1 - d0)
    sp, sa, sb = math.sqrt(p), math.sqrt(p * (1 - r)), math.sqrt(p * (1 + r))
    return L * (1 / sp - 1 / sb) / 10 ** d0, L * (sp - sa) / 10 ** d1


@pytest.mark.parametrize("price,d0,d1,u0,u1", [
    (2677.3, 18, 6, 2677.3, 1.0),        # WETH / USDG
    (0.0045, 6, 18, 1.0, 222.0),         # USDG / 株トークン
    (1.0, 18, 18, 1.0, 1.0),
])
def test_liquidity_round_trip(price, d0, d1, u0, u1):
    t0, t1 = TokenSide(u0, d0, 0.0), TokenSide(u1, d1, 0.0)
    for r in (0.005, 0.01, 0.05, 0.15):
        L = liquidity_for_usd(550, price, r, t0, t1)
        x, y = _amounts(L, price, r, d0, d1)
        assert x * u0 + y * u1 == pytest.approx(550, rel=1e-9)
        # 片側ずつの標準の式（Uniswap v3 の getLiquidityForAmount0/1）でも同じ L になる
        p = price * 10 ** (d1 - d0)
        sp, sa, sb = math.sqrt(p), math.sqrt(p * (1 - r)), math.sqrt(p * (1 + r))
        assert x * 10 ** d0 * sp * sb / (sb - sp) == pytest.approx(L, rel=1e-9)
        assert y * 10 ** d1 / (sp - sa) == pytest.approx(L, rel=1e-9)


def test_narrower_range_gives_more_liquidity():
    t0, t1 = TokenSide(1.0, 18, 0.0), TokenSide(1.0, 18, 0.0)
    assert liquidity_for_usd(550, 1.0, 0.01, t0, t1) > liquidity_for_usd(550, 1.0, 0.05, t0, t1)


# --- 全体の計算 ------------------------------------------------------------------------

def _inputs(**kw):
    base = dict(
        price=1.0,
        token0=TokenSide(1.0, 18, 0.03), token1=TokenSide(1.0, 18, 0.0, stable=True),
        fee=0.003, unstaked_fee=0.10, liquidity_total=10 ** 24, liquidity_staked=5 * 10 ** 23,
        reward_usd_day=500.0, fees_usd_day=100.0, sigma_pair=0.03,
    )
    base.update(kw)
    return PoolInputs(**base)


def test_net_is_income_minus_all_costs():
    ev = evaluate(_inputs(reward_trend_daily=-0.01), ModelParams())
    for x in ev.rows:
        assert x.net == pytest.approx(x.income - x.gamma - x.rebalance - x.hedge - x.haircut - x.direction_risk)
    assert ev.best.net == max(x.net for x in ev.rows)
    assert ev.net_daily_pct == pytest.approx(ev.best.net / 1000 * 100)
    assert ev.net_daily_pct_lp == pytest.approx(ev.best.net / 550 * 100)


def test_income_uses_rebalance_in_range_ratio_and_own_share():
    p = ModelParams(ranges=(0.01,))
    inp = _inputs(fees_usd_day=None)
    x = evaluate(inp, p).rows[0]
    share = x.liquidity_mine / (inp.liquidity_staked + x.liquidity_mine)
    assert x.income == pytest.approx(500 * share * in_range_ratio(0.03, 0.01, 15))
    assert x.mode == "staked"


def test_unstaked_chosen_when_fees_beat_rewards():
    x = evaluate(_inputs(reward_usd_day=0.0, fees_usd_day=1000.0), ModelParams(ranges=(0.01,))).rows[0]
    assert x.mode == "unstaked"
    share = x.liquidity_mine / (10 ** 24 + x.liquidity_mine)
    assert x.income == pytest.approx(1000 * 0.9 * share * x.in_range_ratio)
    assert x.haircut == 0   # 手数料はプールのトークンで受け取るので、報酬トークンの値下がりは関係ない


def test_haircut_only_when_reward_token_falls():
    up = evaluate(_inputs(reward_trend_daily=0.02, fees_usd_day=None), ModelParams(ranges=(0.01,))).rows[0]
    down = evaluate(_inputs(reward_trend_daily=-0.02, fees_usd_day=None), ModelParams(ranges=(0.01,))).rows[0]
    assert up.haircut == 0
    assert down.haircut == pytest.approx(down.income_staked * 0.02)


# --- 参考値: 報酬をすぐ売る前提（2026-09-29 オーナー指示。判定には使わない） ------------------

def test_sell_now_drop_uses_only_the_holding_hours():
    # 1日 −2% のペースなら、1時間で売るときの値下がりは 1 − 0.98^(1/24) ≈ 0.084%
    assert sell_now_drop(-0.02, 1) == pytest.approx(1 - 0.98 ** (1 / 24))
    assert sell_now_drop(-0.02, 24) == pytest.approx(0.02)          # 24時間なら1日分と同じ
    assert sell_now_drop(0.02, 1) == 0 and sell_now_drop(None, 1) == 0   # 値上がり・不明なら引かない


def test_sell_now_reference_does_not_change_judged_net():
    inp = _inputs(reward_trend_daily=-0.05, fees_usd_day=None)
    ev = evaluate(inp, ModelParams(reward_sell_hours=1))
    for x in ev.rows:
        # 判定用の値は7日の傾向（1日 −5%）で引いたまま
        assert x.haircut == pytest.approx(x.income_staked * 0.05)
        assert x.haircut_sell_now == pytest.approx(x.income_staked * (1 - 0.95 ** (1 / 24)))
        assert x.net_sell_now - x.net == pytest.approx(x.haircut - x.haircut_sell_now)
    assert ev.net_daily_pct_sell_now >= ev.net_daily_pct
    # 売るまでの時間が長いほど、参考値は小さくなる
    slow = evaluate(inp, ModelParams(reward_sell_hours=12))
    assert slow.net_daily_pct_sell_now < ev.net_daily_pct_sell_now
    assert slow.net_daily_pct == pytest.approx(ev.net_daily_pct)


def test_hedge_vs_direction_risk():
    p = ModelParams(ranges=(0.01,))
    # ヘッジできない: 値動きの損を引く、ヘッジ費用は0
    no = evaluate(_inputs(), p)
    assert not no.has_perp
    assert no.best.direction_risk == pytest.approx(550 * 0.5 * 0.4 * 0.03)
    assert no.best.hedge == 0
    # ヘッジできる: 値動きの損は0、ヘッジ費用（資金調達の支払い）を引く
    hedged = TokenSide(1.0, 18, 0.03, hedgeable=True, funding_cost_daily=0.001)
    yes = evaluate(_inputs(token0=hedged), p)
    assert yes.has_perp
    assert yes.best.direction_risk == 0
    assert yes.best.hedge == pytest.approx(275 * 0.001)
    # 資金調達を受け取る側でも、既定では収入に数えない（安全側）
    recv = TokenSide(1.0, 18, 0.03, hedgeable=True, funding_cost_daily=-0.001)
    assert evaluate(_inputs(token0=recv), p).best.hedge == 0
    # 片方だけヘッジできるときは、できない方だけ値動きの損に入れる
    both = evaluate(_inputs(token1=TokenSide(1.0, 18, 0.05)), p)
    half = evaluate(_inputs(token0=hedged, token1=TokenSide(1.0, 18, 0.05)), p)
    assert both.best.direction_risk == pytest.approx(550 * 0.5 * 0.4 * 0.08)
    assert half.best.direction_risk == pytest.approx(550 * 0.5 * 0.4 * 0.05)
    assert not half.has_perp


# --- 判定 ------------------------------------------------------------------------------

def _ev(net_pct, has_perp=True):
    """純日利が net_pct% になる計算結果を作る。"""
    hedged = TokenSide(1.0, 18, 0.0, stable=True)
    t0 = TokenSide(1.0, 18, 0.03, hedgeable=has_perp)
    ev = evaluate(_inputs(token0=t0, token1=hedged, sigma_pair=0.0, fees_usd_day=None, reward_usd_day=0.0),
                  ModelParams(ranges=(0.01,)))
    b = ev.best
    object.__setattr__(b, "income", b.income + net_pct * 10 - b.net)
    object.__setattr__(b, "net", net_pct * 10)
    return ev


SP = SignalParams()


def test_green_needs_everything():
    j = judge(_ev(0.5), SP, warnings=[], tvl_usd=300_000)
    assert j.signal == "green"
    assert len(j.reason_ja.splitlines()) <= 3


@pytest.mark.parametrize("net,want", [(0.05, "red"), (0.2, "yellow"), (0.35, "green")])
def test_thresholds_use_total_asset_pct(net, want):
    assert judge(_ev(net), SP, warnings=[], tvl_usd=300_000).signal == want


def test_no_perp_is_at_most_yellow_and_says_so():
    j = judge(_ev(0.5, has_perp=False), SP, warnings=[], tvl_usd=300_000)
    assert j.signal == "yellow"
    assert NO_HEDGE_TEXT in j.reason_ja
    # 🟡や🔴でも、ヘッジできないことは必ず書く
    assert NO_HEDGE_TEXT in judge(_ev(0.05, has_perp=False), SP, warnings=[], tvl_usd=300_000).reason_ja


def test_minor_warning_caps_at_yellow_and_major_is_red():
    minor = Warn("C4", "minor", "報酬の上限を決める仕組みのソースが非公開")
    major = Warn("C2", "major", "報酬トークンが7日で -35%")
    assert judge(_ev(0.5), SP, warnings=[minor], tvl_usd=300_000).signal == "yellow"
    assert judge(_ev(0.5), SP, warnings=[major], tvl_usd=300_000).signal == "red"


def test_small_or_unknown_pool_is_not_green():
    assert judge(_ev(0.5), SP, warnings=[], tvl_usd=50_000).signal == "yellow"
    assert judge(_ev(0.5), SP, warnings=[], tvl_usd=None).signal == "yellow"


def test_missing_data_is_red_with_reason():
    j = judge(None, SP, warnings=[], tvl_usd=None, missing="最新のデータが古い")
    assert j.signal == "red" and "最新のデータが古い" in j.reason_ja


# --- 両替のずれ（スリッページ） ----------------------------------------------------------

def test_swap_price_impact_from_pool_liquidity():
    from farm_radar.scoring.model import swap_price_impact
    t0, t1 = TokenSide(2500.0, 18, 0.0), TokenSide(1.0, 6, 0.0, stable=True)
    price = 2500.0
    sp = math.sqrt(price * 10 ** (6 - 18))
    # 今の価格のところの「見かけの在庫」が USDG 側 $100,000 になる L
    L = int(100_000 * 10 ** 6 / sp)
    got = swap_price_impact(550, L, price, t0, t1)
    # USDG $550 を入れると価格は (1 + 550/100000)^2 − 1 ≈ 1.1% 上がる
    assert got == pytest.approx((1 + 550 / 100_000) ** 2 - 1, rel=1e-3)
    # 流動性が10倍なら、ずれはおよそ1/10
    assert swap_price_impact(550, L * 10, price, t0, t1) == pytest.approx(got / 10, rel=0.02)
    # 流動性がない・価格が分からないときは計算しない（初期値を使う）
    assert swap_price_impact(550, 0, price, t0, t1) is None
    # とても薄いプールでも100%が上限
    assert swap_price_impact(550, 1, price, t0, t1) == 1.0


def test_rebalance_cost_uses_pool_slippage():
    p = ModelParams(ranges=(0.01,))
    lo = evaluate(_inputs(slippage=0.001), p).rows[0]
    hi = evaluate(_inputs(slippage=0.02), p).rows[0]
    per = lo.rebalances_per_day * 550 * 0.5
    assert hi.rebalance - lo.rebalance == pytest.approx(per * 0.019)


def test_too_high_net_gets_warning_and_is_not_green():
    from farm_radar.scoring.judge import TOO_HIGH_TEXT
    j = judge(_ev(6.0), SP, warnings=[], tvl_usd=300_000)
    assert j.signal == "yellow"
    assert any(w.code == "HIGH" for w in j.warnings)
    assert TOO_HIGH_TEXT in j.reason_ja and len(j.reason_ja.splitlines()) <= 3
    # しきい値は設定で変えられる
    assert judge(_ev(6.0), SignalParams(too_high_pct=10.0), warnings=[], tvl_usd=300_000).signal == "green"
    # ヘッジできないプールでも両方書く
    j2 = judge(_ev(6.0, has_perp=False), SP, warnings=[], tvl_usd=300_000)
    assert TOO_HIGH_TEXT in j2.reason_ja and NO_HEDGE_TEXT in j2.reason_ja
