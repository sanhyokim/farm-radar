"""N2b: 機会の一覧（本当に残る利回り・保険あり／なし・自動の分け方・ふるい分け）と、計算に使う一覧の保存のテスト。

外のサイトには行かず、偽の応答と仮のデータベースで確かめる。
"""

import dataclasses
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, app_settings, opportunities as opps, standard
from farm_radar.config import FeedSettings, load_config
from farm_radar.db import database as db
from farm_radar.feeds import sources, store
from farm_radar.feeds.run import TOKEN_BATCH, run_source, wanted_coins
from farm_radar.registry import coin_chains
from farm_radar.scoring.run import score_venue

from .test_feeds import FakeFetcher
from .test_scoring_run import NOW as SCORE_NOW
from .test_scoring_run import FakeGT, _ctx, _fill

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
NOW_S = int(NOW.timestamp())
CFG = load_config()
ETH, USDC, RWD = "0x" + "e" * 40, "0x" + "c" * 40, "0x" + "a" * 40


# --- 仮の一覧 --------------------------------------------------------------------------------------

def _campaign(cid, opp, *, dtype="DUTCH_AUCTION", method="DUTCH_AUCTION", daily=1000.0, apr=None, settings=None,
              start=NOW_S - 5 * 86400, end=NOW_S + 30 * 86400, reward=("RWD", RWD, 2.0), rtype="TOKEN",
              whitelist=None, hidden=False, dist_chain=8453):
    sym, addr, price = reward
    params = {"distributionMethodParameters": {"distributionMethod": method,
                                               "distributionSettings": settings or {}}}
    if whitelist:
        params["whitelist"] = whitelist
    return {"id": f"m-{cid}", "campaignId": cid, "opportunityId": opp, "computeChainId": 8453,
            "distributionChainId": dist_chain, "type": "X", "distributionType": dtype, "startTimestamp": start,
            "endTimestamp": end, "amount": str(int(daily * (end - start) / 86400 / price * 1e18)),
            "rewardToken": {"symbol": sym, "address": addr, "decimals": 18, "price": price, "type": rtype},
            "dailyRewards": daily, "apr": apr, "params": params, "hidden": hidden}


def _opp(oid, name, *, action="POOL", typ="UNISWAP_V3", tokens=(("WETH", ETH), ("USDC", USDC)), tvl=1_000_000.0,
         apr=36.5, native=None, campaigns=()):
    return {"id": oid, "chainId": 8453, "chain": {"id": 8453, "name": "Base"}, "type": typ, "action": action,
            "name": name, "status": "LIVE", "apr": apr, "nativeApr": native, "tvl": tvl, "dailyRewards": 1000.0,
            "liveCampaigns": len(campaigns), "latestCampaignEnd": str(NOW_S + 30 * 86400),
            "protocol": {"id": "p", "name": "Proto"}, "depositUrl": f"https://example/{oid}",
            "tokens": [{"symbol": s, "address": a} for s, a in tokens], "campaigns": list(campaigns)}


OPPS = [
    _opp("o-eth", "Provide WETH/USDC", campaigns=[_campaign("c-eth", "o-eth", daily=20_000.0)]),
    _opp("o-lend", "Lend USDC", action="LEND", typ="ERC20", tokens=(("USDC", USDC),), tvl=2_000_000.0, native=4.0,
         campaigns=[_campaign("c-lend", "o-lend", dtype="MAX_REWARD_VALUE_PER_LIQUIDITY_VALUE", method="MAX_APR",
                              daily=500.0, settings={"apr": "0.05"}, reward=("USDC", USDC, 1.0))]),
    _opp("o-small", "Tiny pool", tvl=10_000.0, campaigns=[_campaign("c-small", "o-small", daily=5.0,
                                                                    start=NOW_S - 10 * 86400)]),
    _opp("o-only", "[Robinhood Users Only] Supply USDC", action="LEND", typ="ERC20", tokens=(("USDC", USDC),),
         campaigns=[_campaign("c-only", "o-only", reward=("USDC", USDC, 1.0))]),
    _opp("o-wl", "Whitelisted", action="LEND", typ="ERC20", tokens=(("USDC", USDC),),
         campaigns=[_campaign("c-wl", "o-wl", whitelist=["0x1"], reward=("USDC", USDC, 1.0))]),
    _opp("o-pts", "Points and other chain", campaigns=[_campaign("c-pts1", "o-pts", rtype="POINT"),
                                                       _campaign("c-pts2", "o-pts", dist_chain=1)]),
    _opp("o-new", "Brand new pool", tvl=5_000.0, campaigns=[_campaign("c-new", "o-new", daily=50.0,
                                                                      start=NOW_S - 86400)]),
    _opp("o-noprice", "Unknown coins", tokens=(("FOO", "0x" + "f" * 40), ("USDC", USDC)),
         campaigns=[_campaign("c-np", "o-noprice")]),
    _opp("o-borrow", "Borrow USDC", action="BORROW", tokens=(("USDC", USDC),),
         campaigns=[_campaign("c-b", "o-borrow")]),
    _opp("o-soon", "Starts later", campaigns=[_campaign("c-soon", "o-soon", start=NOW_S + 86400)]),
    _opp("o-guess", "Lend USDC for an unknown coin", action="LEND", typ="ERC20", tokens=(("USDC", USDC),),
         campaigns=[_campaign("c-guess", "o-guess", daily=500.0, reward=("GUESS", "0x" + "9" * 40, 1.0))]),
    _opp("o-short", "Ends in an hour", campaigns=[_campaign("c-short", "o-short", daily=2.0,
                                                            end=NOW_S + 3600)]),
]


def _prices(base, sigma_hour, trend_day=0.0):
    out, p = [], base
    for h in range(8 * 24 + 1):
        ts = NOW_S - (8 * 24 - h) * 3600
        out.append({"timestamp": ts, "price": p})
        p = p * (1 + (sigma_hour if h % 2 == 0 else -sigma_hour)) * (1 + trend_day) ** (1 / 24)
    return out


def _feeds_db(path):
    conn = store.connect(path)
    ts = store.iso(NOW)
    store.upsert_items(conn, "merkl_opportunities", sources.merkl_opportunities(OPPS), ts, baseline=True)
    store.write_merkl(conn, ts, OPPS)
    store.write_token_prices(conn, ts, {
        f"base:{ETH}": {"symbol": "WETH", "confidence": 0.99, "prices": _prices(2700.0, 0.006)},
        f"base:{USDC}": {"symbol": "USDC", "confidence": 0.99, "prices": _prices(1.0, 0.0)},
        f"base:{RWD}": {"symbol": "RWD", "confidence": 0.9, "prices": _prices(2.0, 0.01, trend_day=-0.02)},
    })
    store.write_lighter_markets(conn, ts, sources.lighter_markets({"order_book_details": [
        {"market_id": 0, "symbol": "ETH", "market_type": "perp", "status": "active", "taker_fee": "0.0000",
         "maker_fee": "0.0000", "default_initial_margin_fraction": 500, "maintenance_margin_fraction": 300},
        {"market_id": 9, "symbol": "OLD", "market_type": "perp", "status": "inactive", "taker_fee": "0",
         "default_initial_margin_fraction": 500, "maintenance_margin_fraction": 300},
        {"market_id": 2048, "symbol": "ETH/USDC", "market_type": "spot", "status": "active"}]}))
    for h in range(3):
        store.write_lighter_funding(conn, store.iso(NOW - timedelta(hours=h)), sources.lighter_funding({"funding_rates": [
            {"market_id": 0, "exchange": "lighter", "symbol": "ETH", "rate": -0.0001},   # マイナス = 売りが払う
            {"market_id": 0, "exchange": "binance", "symbol": "ETH", "rate": 0.05}]}))
    conn.commit()
    conn.close()


@pytest.fixture
def feeds(tmp_path):
    path = tmp_path / "feeds.sqlite3"
    _feeds_db(path)
    return path


@pytest.fixture
def cfg(tmp_path, feeds):
    return dataclasses.replace(CFG, database_path=tmp_path / "main.sqlite3",
                               feeds=dataclasses.replace(CFG.feeds, database_path=feeds))


def _collect(cfg):
    conn = db.connect(cfg.database_path)
    try:
        return {o.base.key: o for o in opps.collect(conn, cfg, NOW)}
    finally:
        conn.close()


# --- 一覧の保存（計算に使う数字） ------------------------------------------------------------------

def test_campaign_terms_keep_how_bonus_is_paid_and_who_can_join():
    c = _campaign("c", "o", dtype="MAX_REWARD_VALUE_PER_LIQUIDITY_VALUE", method="MAX_APR",
                  settings={"apr": "0.05", "ignored": 1}, whitelist=["0x1"], rtype="POINT", hidden=True)
    t = sources.campaign_terms(c)
    assert t["distribution_method"] == "MAX_APR" and json.loads(t["settings_json"]) == {"apr": "0.05"}
    assert (t["restricted"], t["hidden"], t["reward_type"]) == (1, 1, "POINT")
    assert sources.campaign_terms({})["restricted"] == 0


def test_lighter_rows_and_token_prices_are_saved(feeds):
    conn = store.connect(feeds)
    markets = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM lighter_markets")}
    assert set(markets) == {"ETH", "OLD"}                                        # 現物（spot）は入れない
    assert (markets["ETH"]["initial_margin_fraction"], markets["ETH"]["maintenance_margin_fraction"]) == (500, 300)
    rates = conn.execute("SELECT rate_8h FROM lighter_funding_snaps").fetchall()
    assert [r[0] for r in rates] == [-0.0001] * 3                                # Lighter の行だけ
    assert conn.execute("SELECT COUNT(*) FROM token_prices WHERE coin=?", (f"base:{ETH}",)).fetchone()[0] == 193
    assert conn.execute("SELECT symbol FROM token_meta WHERE coin=?", (f"base:{RWD}",)).fetchone()[0] == "RWD"
    # 30日より古い値段は消す
    store.write_token_prices(conn, store.iso(NOW + timedelta(days=40)), {})
    assert conn.execute("SELECT COUNT(*) FROM token_prices").fetchone()[0] == 0
    conn.close()


def test_old_feeds_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript("""CREATE TABLE merkl_opportunity_snaps (ts TEXT, opportunity_id TEXT, status TEXT, apr REAL,
                         max_apr REAL, native_apr REAL, tvl REAL, daily_rewards REAL, live_campaigns INTEGER,
                         PRIMARY KEY (ts, opportunity_id));
                         CREATE TABLE merkl_campaigns (campaign_id TEXT PRIMARY KEY, merkl_id TEXT, opportunity_id TEXT,
                         chain_id INTEGER, distribution_chain_id INTEGER, type TEXT, sub_type TEXT,
                         distribution_type TEXT, start_ts INTEGER, end_ts INTEGER, amount_raw TEXT, reward_symbol TEXT,
                         reward_address TEXT, reward_decimals INTEGER, reward_price REAL, daily_rewards REAL, apr REAL,
                         creator TEXT, created_at INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);""")
    raw.close()
    conn = store.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(merkl_campaigns)")}
    assert {"distribution_method", "settings_json", "restricted", "hidden", "reward_type"} <= cols
    assert "max_daily_rewards" in {r[1] for r in conn.execute("PRAGMA table_info(merkl_opportunity_snaps)")}
    store.write_merkl(conn, store.iso(NOW), OPPS[:1])                            # 足した列に書ける
    conn.close()


def test_token_prices_are_read_two_coins_at_a_time(feeds, tmp_path):
    conn = store.connect(feeds)
    keys = coin_chains(CFG.chains, CFG.root)
    assert keys == {4663: "robinhood", 8453: "base"}
    coins = wanted_coins(conn, keys)
    assert f"base:{ETH}" in coins and f"base:{RWD}" in coins and coins == sorted(coins)
    src = sources.BY_ID["token_prices"]
    texts = {}
    for i in range(0, len(coins), TOKEN_BATCH):
        part = coins[i:i + TOKEN_BATCH]
        texts[f"{src.url}/{','.join(part)}"] = json.dumps({"coins": {
            c: {"symbol": "X", "confidence": 0.9, "prices": [{"timestamp": NOW_S, "price": 1.5}]} for c in part}})
    f = FakeFetcher(texts=texts)
    settings = FeedSettings(database_path=feeds, raw_dir=tmp_path / "raw")
    out = run_source(conn, src, f, settings, NOW, keys)
    assert out["status"] == "ok" and out["items"] == len(coins)
    assert len(f.calls) == math.ceil(len(coins) / TOKEN_BATCH)
    assert all(p == {"span": "169", "period": "1h"} for _, p in f.calls)
    conn.close()


# --- 配り方ごとのボーナス --------------------------------------------------------------------------

def _row(**kw):
    c = _campaign("c", "o", **kw)
    return sources.merkl_campaigns({"id": "o", "campaigns": [c]})[0]


def test_bonus_share_by_how_it_is_paid():
    x, tvl = 1000.0, 99_000.0
    dutch, note = opps.campaign_income(_row(daily=1000.0), x, tvl)
    assert dutch == pytest.approx(1000.0 * x / (tvl + x)) and note is None      # 山分け: D × X ÷ (T + X)
    capped, _ = opps.campaign_income(_row(dtype="MAX_REWARD_VALUE_PER_LIQUIDITY_VALUE", settings={"apr": "0.05"}),
                                     x, tvl)
    assert capped == pytest.approx(0.05 * x / 365)                               # 上限 5%/年のほうが小さい
    fixed, _ = opps.campaign_income(_row(dtype="FIX_REWARD_VALUE_PER_LIQUIDITY_VALUE", settings={"apr": "0.2"}), x, tvl)
    assert fixed == pytest.approx(0.2 * x / 365)
    per, _ = opps.campaign_income(_row(dtype="FIX_REWARD_AMOUNT_PER_LIQUIDITY_VALUE", settings={"apr": "0.1"}),
                                  x, tvl)
    assert per == pytest.approx(0.1 * 2.0 * x / 365)                             # コインの数 × 値段
    top, note = opps.campaign_income(_row(dtype="ERC4626", method="X", apr=3.0), x, tvl)
    assert top == pytest.approx(min(0.03 * x / 365, 1000.0 * x / (tvl + x))) and "上乗せ" in note
    air, note = opps.campaign_income(_row(method="AIRDROP"), x, tvl)
    assert air == 0.0 and "一度きり" in note
    odd, note = opps.campaign_income(_row(dtype="SOMETHING_NEW", method="X"), x, tvl)
    assert odd == pytest.approx(dutch) and "山分けとして" in note
    assert opps.budget_daily(_row(daily=1000.0)) == pytest.approx(1000.0, rel=1e-6)


# --- 保険の分け方（2026-10-02 オーナー決定「自動で計算」） --------------------------------------------

def test_hedge_deposit_is_enough_to_survive_the_rise():
    perp = {"imf": 0.05, "mmf": 0.03}
    need = opps.margin_need(perp, 0.5)
    assert need == pytest.approx(0.5 + 1.5 * 0.03)                               # 50% 上がっても強制的に閉じられない
    assert opps.margin_need({"imf": 0.9, "mmf": 0.01}, 0.5) == 0.9               # 最初に要る割合のほうが大きいとき
    split = opps._split(True, 0.5 * need, 0.01)                                  # プールは半分が ETH・予備 $10 / $1,000
    assert sum(split.values()) == pytest.approx(1.0)
    assert split["hedge_margin"] == pytest.approx(split["pool"] * 0.5 * need)
    assert split["reserve"] == 0.01
    assert 0.77 < split["pool"] < 0.79                                           # $1,000 → 約 $780 を置く
    assert opps._split(False, reserve=0.01) == {"pool": 0.99, "hedge_margin": 0.0, "reserve": 0.01}


def test_reserve_is_gas_money_per_chain():
    s = CFG.opportunities
    assert opps.reserve_usd(s, "robinhood") == 20.0 and opps.reserve_usd(s, "base") == 10.0
    assert opps.reserve_usd(s, "unknown") == 20.0                                # 書いていないチェーンは一番大きい値


def test_stay_factor_and_provisional_drop():
    s = CFG.opportunities
    r = opps.provisional_trend(s)
    assert (1 + r) ** 30 == pytest.approx(0.70)                                  # 月 −30%
    f = opps.stay_factor(r, 14)
    assert f == pytest.approx(0.9212, abs=1e-4)                                  # 14日いる間の平均の値段は約 92%
    assert opps.stay_factor(0.01, 14) == 1.0 and opps.stay_factor(None, 14) == 1.0
    assert opps.stay_factor(r, 30) == pytest.approx(-0.3 / math.log(0.7))


# --- 1つの機会の計算 ------------------------------------------------------------------------------

def test_pool_with_a_hedge_has_both_variants_and_the_split(cfg):
    o = _collect(cfg)["o-eth"]
    assert o.computable and o.kind == "pool_range" and not o.excluded
    row = o.calc["1000"]
    no, yes = row["cautious"]["no_hedge"], row["cautious"]["hedge"]
    assert no is not None and yes is not None
    for v in (no, yes):
        assert sum(v.split.values()) == pytest.approx(1000.0)                    # 総額 $1,000 を分ける
        assert v.apr_pct == pytest.approx(v.net_after_move / 1000 * 365 * 100)  # 総額あたりの年利
        assert v.net_after_move == pytest.approx(v.net - v.move_cost / v.stay_days)
    assert yes.split["hedge_margin"] > 0 and no.split["hedge_margin"] == 0
    assert yes.split["pool"] < no.split["pool"]
    assert yes.hedge_cost > 0 and yes.direction == 0                            # 保険の費用を払い、値動きの損は消える
    assert no.hedge_cost == 0 and no.direction > 0
    assert no.haircut > 0                                                        # ボーナスのコインが下がっている
    # 控えめの見込み（ほかの人のお金 1.5 倍）は、ふつうより低い
    assert row["cautious"]["hedge"].income < row["normal"]["hedge"].income
    # 保険で守れない値動き = ボーナスのコイン
    assert o.unprotected == ["ボーナスのコイン（RWD）の値下がり"]
    assert {f.code for f in o.flags} >= {"RANGE", "NO_FEES"}
    # 金額が大きいほど自分のお金で薄まる
    assert o.calc["100000"]["cautious"]["hedge"].apr_pct < yes.apr_pct
    assert o.cap_usd == pytest.approx(1_000_000 * cfg.opportunities.max_pool_share)


def test_stable_deposit_is_no_hedge_only_and_counts_its_own_interest(cfg):
    o = _collect(cfg)["o-lend"]
    assert o.kind == "hold" and o.computable and o.unprotected == []
    v = o.calc["1000"]["normal"]["no_hedge"]
    assert o.calc["1000"]["normal"]["hedge"] is None                            # 値動きしないコインだけ → 保険なしだけ
    pool = v.split["pool"]
    assert v.income == pytest.approx(0.05 * pool / 365 + 0.04 * pool / 365)     # 上限 5% のボーナス ＋ 元の利息 4%
    assert v.gamma == v.direction == v.haircut == 0
    assert not any(f.code == "NO_HEDGE" for f in o.flags)


def test_screening_flags(cfg):
    ops = _collect(cfg)
    codes = {k: {f.code for f in o.flags} for k, o in ops.items()}
    assert "SMALL" in codes["o-small"] and ops["o-small"].excluded
    assert "RESTRICTED" in codes["o-only"] and ops["o-only"].excluded         # 名前の [... Only]
    assert "RESTRICTED" in codes["o-wl"] and ops["o-wl"].excluded             # 名簿つき
    assert {"POINTS", "CROSS_CHAIN", "RWD_GUESS"} <= codes["o-pts"]                  # 別のチェーンのコインは値動きが分からない
    assert ops["o-pts"].flags[[f.code for f in ops["o-pts"].flags].index("RWD_GUESS")].level == "warn"
    assert ops["o-new"].new_pool and "NEW_POOL" in codes["o-new"] and "SMALL" not in codes["o-new"]
    assert "PAYBACK" in codes["o-short"] and ops["o-short"].excluded          # 1時間では費用を取り返せない
    assert not ops["o-noprice"].computable and "FOO" in ops["o-noprice"].reason
    assert not ops["o-borrow"].computable and ops["o-borrow"].kind == "other"
    assert not ops["o-soon"].computable and "予定" in ops["o-soon"].reason
    # ポイントは0として数え、印を付ける
    assert ops["o-pts"].calc["1000"]["normal"]["no_hedge"].points


def test_unhedgeable_coin_is_listed_as_not_protectable(cfg, tmp_path):
    conn = store.connect(cfg.feeds.database_path)
    conn.execute("DELETE FROM lighter_markets")
    conn.commit()
    conn.close()
    o = _collect(cfg)["o-eth"]
    assert o.calc["1000"]["cautious"]["hedge"] is None
    assert "NO_HEDGE" in {f.code for f in o.flags}
    assert "WETH の値下がり（保険の売り場がない）" in o.unprotected


def test_rank_puts_above_target_first_and_hides_excluded(cfg):
    ops = list(_collect(cfg).values())
    ranked = opps.rank([o for o in ops if o.computable], 1000.0, 30.0)
    assert all(not o.excluded for o in ranked)
    aprs = [o.best(1000.0).apr_pct for o in ranked]
    above = [a >= 30.0 for a in aprs]
    assert above == sorted(above, reverse=True)
    assert len(opps.rank([o for o in ops if o.computable], 1000.0, 30.0, include_excluded=True)) > len(ranked)


def test_own_venue_reuses_the_approved_model_with_the_split(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    _fill(conn, 24 * 8)
    score_venue(conn, _ctx(FakeGT()), now=SCORE_NOW)
    data = opps.FeedData(None, CFG, SCORE_NOW)
    base = next(b for b in standard.from_own(conn, CFG, SCORE_NOW) if b.key.endswith("p-weth"))
    # Lighter の証拠金の割合が分からないと、保険ありは出さない（分け方を計算できないため）
    o = opps.evaluate_own(base, conn, CFG, data)
    assert o.computable and o.calc["1000"]["normal"]["hedge"] is None
    assert "WETH の値下がり（保険の売り場がない）" in o.unprotected
    data.perps["ETH"] = {"symbol": "ETH", "funding_daily": 0.0003, "imf": 0.04, "mmf": 0.024, "taker_pct": 0.0}
    o = opps.evaluate_own(base, conn, CFG, data)
    yes, no = o.calc["1000"]["normal"]["hedge"], o.calc["1000"]["normal"]["no_hedge"]
    assert yes.split["hedge_margin"] == pytest.approx(yes.split["pool"] * 0.5 * opps.margin_need(data.perps["ETH"], 0.5))
    assert sum(yes.split.values()) == pytest.approx(1000.0)
    assert yes.direction == 0 and no.direction > 0 and yes.hedge_cost > 0
    assert any(f.code == "VENUE" for f in o.flags)                               # 会場の警告（C4 など）をそのまま
    conn.close()


def test_unknown_bonus_coin_gets_a_provisional_drop_in_the_cautious_case(cfg):
    o = _collect(cfg)["o-guess"]
    guess = next(f for f in o.flags if f.code == "RWD_GUESS")
    assert "値下がり未計算（仮の値で計算）" in guess.text
    nor, cau = o.calc["1000"]["normal"]["no_hedge"], o.calc["1000"]["cautious"]["no_hedge"]
    assert nor.haircut == 0                                                     # ふつうは引かない（記録がない）
    s = cfg.opportunities
    r = opps.provisional_trend(s)
    pool, tvl = cau.split["pool"], 1_000_000.0 * s.cautious_tvl_multiple
    bonus = 500.0 * pool / (tvl + pool) * opps.stay_factor(r, cau.stay_days)
    assert cau.haircut == pytest.approx(bonus * -r)
    assert cau.income == pytest.approx(bonus)                                   # 元の利息はない機会
    assert cau.split["reserve"] == 10.0 and cau.split["pool"] == 990.0          # Base の予備 $10、保険なし


def test_known_bonus_coin_uses_its_record(cfg):
    o = _collect(cfg)["o-eth"]
    nor, cau = o.calc["1000"]["normal"]["no_hedge"], o.calc["1000"]["cautious"]["no_hedge"]
    assert "RWD_GUESS" not in {f.code for f in o.flags}
    assert nor.haircut > 0 and cau.haircut > 0                                  # 記録の傾き（1日 −2%）で引く
    assert cau.haircut / cau.income < nor.haircut / nor.income * 1.0001        # 控えめは平均の値段で数えるので比は同じか小さい


# --- 設定と読み取り口 ----------------------------------------------------------------------------

def test_target_yield_setting(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    assert app_settings.target_apr_pct(conn, CFG) == CFG.opportunities.target_apr_pct == 30.0
    app_settings.set_target_apr_pct(conn, "45", NOW)
    assert app_settings.view(conn, CFG)["target_apr_pct"] == 45.0
    for bad in ("abc", 0, -5, 1001):
        with pytest.raises(app_settings.SettingsError):
            app_settings.set_target_apr_pct(conn, bad, NOW)
    assert app_settings.target_apr_pct(conn, CFG) == 45.0
    conn.close()


def test_api_opportunities_and_settings(cfg, monkeypatch):
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW)
    api._opps_cache.clear()
    c = TestClient(api.app)
    d = c.get("/api/opportunities").json()
    assert d["amount"] == 1000.0 and d["target_apr_pct"] == 30.0
    assert d["counts"]["total"] == len(OPPS) and d["counts"]["not_computable"] == 3
    keys = [i["key"] for i in d["items"]]
    assert "o-small" not in keys and "o-eth" in keys
    eth = next(i for i in d["items"] if i["key"] == "o-eth")
    assert set(eth["calc"]) == {"1000"} and eth["best"]["amount"] == 1000.0
    assert eth["above_target"] == (eth["best"]["apr_pct"] >= 30.0)
    assert eth["unprotected"] == ["ボーナスのコイン（RWD）の値下がり"]
    assert "o-small" in [i["key"] for i in c.get("/api/opportunities?show_excluded=true").json()["items"]]
    assert {i["chain"] for i in c.get("/api/opportunities?chain=base").json()["items"]} == {"base"}
    assert c.get("/api/opportunities?chain=robinhood").json()["items"] == []
    assert c.get("/api/opportunities?amount=123").status_code == 400
    assert c.get("/api/settings").json()["target_apr_pct"] == 30.0
    assert c.put("/api/settings", json={"target_apr_pct": 5}).json()["target_apr_pct"] == 5.0
    assert c.get("/api/opportunities").json()["target_apr_pct"] == 5.0
    r = c.put("/api/settings", json={"target_apr_pct": 0})
    assert r.status_code == 400 and "狙い利回り" in r.json()["detail"]
    api._opps_cache.clear()


def test_uncertain_venue_is_listed_but_not_recommended(cfg):
    # オーナー 2026-10-02 23:15 JST: 会場の見分けが不確かな行は、一覧には出すが練習のおすすめに入れない
    ops = [o for o in _collect(cfg).values() if o.computable and not o.excluded]
    target = min(o.best(1000.0).apr_pct for o in ops)    # 全部を狙い以上にする
    sure, unsure = ops[0], ops[1]
    sure.venue_safety = {"level": "high", "uncertain_match": False}
    unsure.venue_safety = {"level": "high", "uncertain_match": True}
    assert sure.recommended(1000.0, target) and not unsure.recommended(1000.0, target)
    d = unsure.to_dict(1000.0, target)
    assert d["above_target"] is True and d["recommended"] is False and d["safety"]["uncertain_match"] is True
    ranked = opps.rank(ops, 1000.0, target)
    assert unsure in ranked
    flags = [o.recommended(1000.0, target) for o in ranked]
    assert flags == sorted(flags, reverse=True)


def test_cautious_move_uses_the_larger_of_7_and_30_days_and_counts_jumps():
    from farm_radar.opportunities import Move

    mv = Move(sigma=0.012, smooth=0.012, jumps=(0.004,), days=7.0, sigma30=0.027, smooth30=0.02,
              jumps30=(0.1, -0.016), days30=30.0)
    assert mv.case(False) == (0.012, 0.012, (0.004,), 7.0)
    total, smooth, jumps, days = mv.case(True)
    assert (total, smooth, jumps, days) == (0.027, 0.02, (0.1, -0.016), 30.0)
    assert Move(sigma=0.01, smooth=0.01, jumps=(), days=7.0).case(True)[1] == 0.01   # 30日が無ければ7日のまま


def test_narrow_range_pays_for_jumps_over_the_range():
    from farm_radar.config import load_config
    from farm_radar.opportunities import Move, _variant

    cfg = load_config()
    kw = dict(hedge=False, amount=1000.0, split={"pool": 0.95, "hedge_margin": 0.0, "reserve": 0.05},
              kind="pool_range", legs=[], campaigns=[], tvl=1e6, base_apr_pct=0.0, cautious=True,
              s=cfg.opportunities, config=cfg, gas=0.1, fee=0.0005, stay_days=7.0, r=0.005)
    calm = _variant(move=Move(sigma=0.012, smooth=0.012, jumps=(), days=7.0), **kw)
    jumpy = _variant(move=Move(sigma=0.012, smooth=0.012, jumps=(), days=7.0, sigma30=0.027, smooth30=0.012,
                               jumps30=(0.1, -0.016, 0.002), days30=30.0), **kw)
    assert jumpy.jumps_per_day == pytest.approx(2 / 30)          # 幅（±0.5%）を飛び越えた2回
    assert jumpy.gamma > calm.gamma and jumpy.rebalance > calm.rebalance
    assert jumpy.in_range_ratio < calm.in_range_ratio
