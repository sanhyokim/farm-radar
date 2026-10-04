"""2026-10-04 14:40 JST オーナー決定のテスト: ①A 値動きの控えめの見込みに直近24時間、②A Lighter の Robinhood Chain 版、
③A 預け金が減ったら「足したとしたら」の記録。"""

import math

import pytest

from farm_radar import opportunities as opps
from farm_radar.scoring import volatility as vol


# --- ①A 直近24時間の値動き ------------------------------------------------------------------------------

def _grid(hours, quiet, loud):
    """hours 時間の1時間ごとの値段。最後の24時間だけ ±loud、それまでは ±quiet で上下する。"""
    out, p = [], 100.0
    for h in range(hours):
        step = loud if h >= hours - 24 else quiet
        p *= math.exp(step if h % 2 else -step)
        out.append((h * 3600, p))
    return out


def test_recent_sigma_uses_only_the_last_24_hours():
    g = _grid(7 * 24, 0.005, 0.02)
    rets = vol.hourly_returns(g)
    s24 = vol.recent_sigma(rets, g[-1][0])
    s7 = vol.daily_sigma([r for _, r in rets])
    assert s24 == pytest.approx(0.02 * math.sqrt(24))
    assert s7 < s24 / 2
    assert vol.recent_sigma(rets[:5], g[4][0]) is None              # 12 区切りより少なければ出さない


def test_cautious_move_takes_the_largest_of_7_30_and_24_hours():
    mv = opps.Move(sigma=0.20, smooth=0.18, jumps=(), days=7.0, sigma30=0.25, smooth30=0.22, days30=30.0,
                   sigma24=0.40, smooth24=0.39)
    assert mv.case(False)[:2] == (0.20, 0.18)                       # ふつうの見込みは7日のまま
    assert mv.case(True)[:2] == (0.40, 0.39)                        # 控えめは大きいもの（例: WETH/MOO 19.3% → 39.5%）
    no30 = opps.Move(sigma=0.20, smooth=0.18, jumps=(), days=7.0, sigma24=0.30, smooth24=0.29)
    assert no30.case(True)[:2] == (0.30, 0.29)                      # 30日がなくても24時間は使う
    assert mv.jumped(1.5) and not no30.jumped(1.5)                   # 0.40 > 1.5 × 0.20、0.30 は 1.5 倍ちょうどで印なし


def test_vol_jump_flag_text_names_the_pair_and_the_coin():
    mv = opps.Move(sigma=0.193, smooth=0.19, jumps=(), days=7.0, sigma24=0.395, smooth24=0.39)
    stat = opps.TokenStat("c", "MOO", 1.0, 0.10, None, (), sigma24=0.30)
    leg = opps.Leg(symbol="MOO", stable=False, stat=stat, perp=None)
    f = opps.vol_jump_flag(mv, [leg], 1.5)
    assert f is not None and f.code == "VOL_JUMP" and f.level == opps.LEVEL_WARN
    assert "直近24時間 39.5%／7日 19.3%" in f.text and "MOO は直近24時間 30.0%／7日 10.0%" in f.text
    quiet = opps.Move(sigma=0.2, smooth=0.2, jumps=(), days=7.0, sigma24=0.25, smooth24=0.25)
    assert opps.vol_jump_flag(quiet, [], 1.5) is None


# --- ②A Lighter の Robinhood Chain 版を読む ---------------------------------------------------------------

import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from farm_radar import trial_records  # noqa: E402
from farm_radar.feeds import sources, store, trial  # noqa: E402

RH_BOOK_DETAILS = {"order_book_details": [
    {"market_id": 0, "symbol": "ETH", "market_type": "perp", "status": "active", "taker_fee": "0.0000",
     "maker_fee": "0.0000", "default_initial_margin_fraction": 5000, "min_initial_margin_fraction": 200,
     "maintenance_margin_fraction": 120, "mark_price": "2695.6", "open_interest": 1, "daily_quote_token_volume": 2},
    {"market_id": 32, "symbol": "SNDK", "market_type": "perp", "status": "active", "taker_fee": "0.0000",
     "maker_fee": "0.0000", "default_initial_margin_fraction": 5000, "min_initial_margin_fraction": 1000,
     "maintenance_margin_fraction": 600, "mark_price": "1719.7", "open_interest": 1, "daily_quote_token_volume": 2},
    {"market_id": 50, "symbol": "SOL", "market_type": "perp", "status": "active", "taker_fee": "0.0000",
     "maker_fee": "0.0000", "default_initial_margin_fraction": 5000, "min_initial_margin_fraction": 200,
     "maintenance_margin_fraction": 120, "mark_price": "200", "open_interest": 1, "daily_quote_token_volume": 2}]}


def test_rh_markets_and_history_go_to_their_own_tables(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    items = sources.lighter_markets(RH_BOOK_DETAILS)
    assert store.write_lighter_rh_markets(conn, "2026-10-04T06:00:00+00:00", items) == 3
    row = conn.execute("SELECT * FROM lighter_rh_markets WHERE symbol='SNDK'").fetchone()
    assert row["maintenance_margin_fraction"] == 600 and row["min_initial_margin_fraction"] == 1000
    # 読む銘柄は up. の保険の市場（本体の番号と記号）のうち、この版にあるものだけ。番号はこの版のもの
    ctx = trial.TrialContext(lighter_markets=((0, "ETH"), (139, "SNDK"), (150, "NBIS")))
    assert trial.rh_targets(conn, ctx) == [(0, "ETH"), (32, "SNDK")]
    now = datetime(2026, 10, 4, 6, tzinfo=UTC)
    urls = []

    def text(url, params):
        urls.append(url)
        end = int(params["end_timestamp"])
        if end < int(now.timestamp()):                  # 2回目（古い方へさかのぼる）は空
            return json.dumps({"c": [], "fundings": []})
        if url.endswith("/candles"):
            return json.dumps({"c": [{"t": (end - 3600) * 1000, "o": 1, "h": 2, "l": 1, "c": 2}]})
        return json.dumps({"fundings": [{"timestamp": end - 3600, "rate": "0.0012", "value": "0.1", "direction": "long"}]})

    data, _ = trial.read_lighter_prices(conn, text, ctx, now, trial.RH_BOOK)
    assert trial.write_lighter_prices(conn, data, trial.RH_BOOK) == 2
    data, _ = trial.read_lighter_history(conn, text, ctx, now, trial.RH_BOOK)
    assert trial.write_lighter_history(conn, data, trial.RH_BOOK) == 2
    assert all(u.startswith(trial.LIGHTER_RH) for u in urls)
    assert conn.execute("SELECT COUNT(*) FROM lighter_price_history").fetchone()[0] == 0     # 本体の表は触らない
    assert conn.execute("SELECT COUNT(*) FROM lighter_rh_price_history WHERE market_id=32").fetchone()[0] == 1
    # 本体の銘柄（比べる相手）と資金調達率
    store.write_lighter_markets(conn, "2026-10-04T06:00:00+00:00", [
        sources.Item("139", "SNDK", None, {"status": "active", "imf": 666, "mmf": 300, "mark_price": 1717.7})])
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    store.write_lighter_funding(conn, now_iso, [sources.Item("32", "SNDK", None, {"rate_8h": 0.0001})],
                                "lighter_rh_funding_snaps")
    conn.commit()
    s = trial_records.lighter_rh(conn)
    assert s["markets"] == 3 and s["hedge_markets"] == 2
    sndk = next(x for x in s["rows"] if x["symbol"] == "SNDK")
    assert sndk["rh_market_id"] == 32 and sndk["main_market_id"] == 139
    assert sndk["mmf_rh_pct"] == 6.0 and sndk["mmf_main_pct"] == 3.0
    assert sndk["funding_daily_rh"] == pytest.approx(-0.0003) and sndk["funding_daily_main"] is None


# --- ③A 預け金を「足したとしたら」の記録 ---------------------------------------------------------------------

from farm_radar.execution import hedge_guard, risk_job  # noqa: E402
from farm_radar.execution.jobs import run_paper  # noqa: E402

from .test_paper import FakeFx, _config, _extend, _open, world  # noqa: E402, F401
from .test_risk import _events, _pos, _set_score, calm  # noqa: E402, F401
from .test_scoring_run import NOW, TOKENS  # noqa: E402


def test_topup_is_recorded_once_below_half_and_counts_earlier_topups(world):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    pos = _pos(conn, ref.position_id)
    cfg = _config(path)
    assert cfg.guard.hedge_topup_buffer_frac == 0.5
    ms0 = hedge_guard.margin_status(conn, cfg, pos, mmf_table={})
    t = datetime(2026, 10, 4, 6, tzinfo=UTC)
    assert risk_job.record_topup(conn, ex, pos, ms0, 0.5, t) is None                 # 余裕が十分なら記録しない
    liq = ms0["to_liquidation_pct"]
    low = hedge_guard.margin_status(conn, cfg, pos, rise_pct=liq * 0.6, mmf_table={})
    assert low["buffer_frac"] < 0.5
    r = risk_job.record_topup(conn, ex, pos, low, 0.5, t)
    assert r["topup_usd"] == pytest.approx(low["buffer_initial_usd"] - low["buffer_usd"])
    # 足すお金はプールから同じ割合で出すので、値動きするコインが減り、売りも同じだけ減らす
    take = r["topup_usd"] / r["pool_usd"]
    assert r["volatile_after_usd"] == pytest.approx(r["volatile_before_usd"] * (1 - take))
    assert r["short_after_usd"] == pytest.approx(min(r["short_before_usd"], r["volatile_after_usd"]))
    assert r["short_after_usd"] < r["short_before_usd"]
    p = r["parts"]
    assert r["cost_usd"] == pytest.approx(p["gas_withdraw"] + p["swap"] + p["gas_swap"] + p["gas_deposit"] + p["hedge_fee"])
    assert r["cost_usd"] > 0
    # 同じ余裕のまま次の見張り: 前に足したとしたらの分を入れると、はじめの余裕に戻っているので2回目は数えない
    assert risk_job.record_topup(conn, ex, pos, low, 0.5, t + timedelta(minutes=15)) is None
    # 足したとしたらの分を入れてもまた半分を切ったら、2回目を数える（足りない分だけ）
    lower = {**low, "buffer_usd": low["buffer_usd"] - 0.6 * low["buffer_initial_usd"]}
    r2 = risk_job.record_topup(conn, ex, pos, lower, 0.5, t + timedelta(minutes=30))
    assert r2["topup_usd"] == pytest.approx(lower["buffer_initial_usd"] - lower["buffer_usd"] - r["topup_usd"])
    rows = risk_job.topup_rows(conn)
    assert rows[0]["count"] == 2 and rows[0]["total_usd"] == pytest.approx(r["topup_usd"] + r2["topup_usd"])
    assert rows[0]["last"]["pool_usd"] > 0
    # 練習の建玉そのものは変えない（記録だけ）
    assert _pos(conn, ref.position_id)["hedges_json"] == pos["hedges_json"]
    assert _pos(conn, ref.position_id)["liquidity"] == pos["liquidity"]


def test_risk_run_writes_a_topup_event_and_guard_summary_shows_it(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    _extend(conn, 1)
    orig = hedge_guard.margin_status

    def low(conn, config, pos, rise_pct=0.0, mmf_table=None):
        ms = orig(conn, config, pos, rise_pct, mmf_table)
        return ms and {**ms, "buffer_usd": ms["buffer_initial_usd"] * 0.4, "buffer_frac": 0.4}

    saved = risk_job.hedge_guard.margin_status
    risk_job.hedge_guard.margin_status = low
    try:
        run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    finally:
        risk_job.hedge_guard.margin_status = saved
    ev = [e for e in _events(conn) if e["kind"] == "hedge_topup_if"]
    assert len(ev) == 1 and ev[0]["level"] == "caution" and "練習では足さず" in ev[0]["message_ja"]
    n = conn.execute("SELECT COUNT(*) FROM hedge_topup_log WHERE position_id=?", (ref.position_id,)).fetchone()[0]
    assert n == 1
    assert _pos(conn, ref.position_id)["status"] == "open"
    from farm_radar import guard
    s = guard.summary(conn, _config(path), NOW + timedelta(hours=2))
    assert s["topups"][0]["count"] == 1 and s["topup_line_frac"] == 0.5


# --- ②A RH版の数字で見込む -------------------------------------------------------------------------------

def _two_books(tmp_path):
    from farm_radar.config import load_config
    conn = store.connect(tmp_path / "f.sqlite3")
    ts = "2026-10-04T06:00:00+00:00"
    store.write_lighter_markets(conn, ts, [
        sources.Item("0", "ETH", None, {"status": "active", "imf": 200, "mmf": 120, "taker_pct": 0.0}),
        sources.Item("139", "SNDK", None, {"status": "active", "imf": 666, "mmf": 300, "taker_pct": 0.0}),
        sources.Item("150", "NBIS", None, {"status": "active", "imf": 1000, "mmf": 600, "taker_pct": 0.0})])
    store.write_lighter_rh_markets(conn, ts, sources.lighter_markets(RH_BOOK_DETAILS))
    store.write_lighter_funding(conn, ts, [sources.Item("0", "ETH", None, {"rate_8h": -0.0001}),
                                           sources.Item("139", "SNDK", None, {"rate_8h": -0.0001}),
                                           sources.Item("150", "NBIS", None, {"rate_8h": -0.0001})])
    store.write_lighter_funding(conn, ts, [sources.Item("0", "ETH", None, {"rate_8h": -0.0004}),
                                           sources.Item("32", "SNDK", None, {"rate_8h": -0.0004})],
                                "lighter_rh_funding_snaps")
    # 値段の過去: 本体の SNDK は14日で +60%、RH版の SNDK は +80%（RH版のほうが大きい）
    t0 = int(datetime(2026, 9, 25, tzinfo=UTC).timestamp())
    for table, mid, top in (("lighter_price_history", 139, 160.0), ("lighter_rh_price_history", 32, 180.0)):
        for h in range(48):
            px = 100.0 if h < 24 else top
            conn.execute(f"INSERT INTO {table}(market_id, ts, symbol, open, high, low, close) VALUES (?,?,?,?,?,?,?)",
                         (mid, t0 + h * 3600, "SNDK", px, px, px, px))
    conn.commit()
    return conn, load_config()


def test_robinhood_pools_use_the_rh_lighter_numbers_and_base_pools_use_main(tmp_path):
    conn, cfg = _two_books(tmp_path)
    assert cfg.opportunities.lighter_rh_chain_ids == (4663,)
    data = opps.FeedData(conn, cfg, datetime(2026, 10, 4, 7, tzinfo=UTC))
    rh = data.perp("SNDK", chain_id=4663)
    main = data.perp("SNDK", chain_id=8453)
    assert rh["book"] == "rh" and rh["rh_market_id"] == 32 and rh["market_id"] == 139
    assert rh["mmf"] == 0.06 and main["mmf"] == 0.03                    # 維持の割合は RH版 6%、本体 3%
    assert rh["funding_daily"] == pytest.approx(0.0012) and main["funding_daily"] == pytest.approx(0.0003)
    assert main["book"] == "main"
    # 耐える上げ幅: RH版の過去（+80%）と本体の過去（+60%）の大きい方
    assert data.withstand(rh) == pytest.approx(0.8) and data.withstand(main) == pytest.approx(0.6)
    # RH版にない市場（NBIS）は、Robinhood Chain のプールでも本体
    assert data.perp("NBIS", chain_id=4663)["book"] == "main"
    assert data.perp("WETH", chain_id=4663)["book"] == "rh"               # 別名（WETH → ETH）でも RH版
    assert data.perp("WETH")["book"] == "main"                            # チェーンが分からなければ本体


_REAL_MARGIN_TABLE = hedge_guard.lighter_margin_table      # conftest が作業場所の値を読まないように差し替える前のもの


def test_practice_margin_tables_switch_to_rh_for_robinhood(tmp_path, monkeypatch):
    from farm_radar.execution import hedge_guard as hg
    monkeypatch.setattr(hg, "lighter_margin_table", _REAL_MARGIN_TABLE)
    conn, cfg = _two_books(tmp_path)
    conn.close()
    path = tmp_path / "f.sqlite3"
    assert hg._mmf_table(path)[139] == 0.03 and hg._mmf_table(path, True)[139] == 0.06
    assert hg._mmf_table(path, True)[150] == 0.06                       # NBIS は本体のまま（本体も 6%）
    assert hg.lighter_margin_table(path, True)[139] == (0.5, 0.06)
    assert hg.withstand_table(path, 14, True)[139] == pytest.approx(0.8)
    assert hg.withstand_table(path, 14)[139] == pytest.approx(0.6)
    assert hg.use_rh(cfg) and hg.use_rh(cfg, 4663) and not hg.use_rh(cfg, 8453)


# --- ②A 練習の資金調達料も RH版の記録で積み上げる --------------------------------------------------------------

class _RhFunding:
    hedge_id = "lighter_rh"

    def __init__(self):
        self.asked = []

    def short_funding_hourly(self, market_id, start, end):
        self.asked.append(market_id)
        return [(t, 0.0001) for t in range(start - start % 3600, end + 1, 3600)]   # 1時間 0.01%（本体の10倍）


def test_practice_funding_uses_the_rh_records_for_robinhood_pools(world, monkeypatch):  # noqa: F811
    import json as _json
    from farm_radar.execution import jobs
    from .test_paper import FakeLighter
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 6)
    mid = _json.loads(_pos(conn, ref.position_id)["hedges_json"])[0]["market_id"]
    monkeypatch.setattr(jobs, "rh_pairs", lambda config, ids: {m: 77 for m in ids})   # 本体の番号 → RH版 77
    rh = _RhFunding()
    lighter = FakeLighter()
    hedges = {"lighter": jobs.hedge_mod.wrap(lighter), "lighter_rh": rh}
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=7), hedges=hedges)
    assert rh.asked and set(rh.asked) == {77}                                  # RH版の番号で読む
    rows = conn.execute("SELECT DISTINCT market_id FROM hedge_funding WHERE hedge_id='lighter_rh'").fetchall()
    assert [r[0] for r in rows] == [mid]                                       # 表には本体の番号で入れる
    assert _json.loads(_pos(conn, ref.position_id)["state_json"])["funding_paid"] > 0
    ex2 = jobs.PaperExecutor(conn, _config(path), TOKENS, now=NOW + timedelta(hours=7))
    at = NOW + timedelta(hours=6)
    assert ex2._funding(mid, at) == pytest.approx(0.0001)                      # RH版の記録があればそちら
    conn.execute("DELETE FROM hedge_funding WHERE hedge_id='lighter_rh'")
    assert ex2._funding(mid, at) == pytest.approx(0.00001)                     # なければ本体の記録（1時間 0.001%）


# --- N5c Merkl の答え合わせ ----------------------------------------------------------------------------------

def test_position_reason_and_info_are_parsed():
    from farm_radar.feeds import positions
    pid = "0x" + "ab" * 32
    assert positions.parse_reason(f"UNISWAP_V4_{pid}_3508794") == (pid, 3508794)
    assert positions.parse_reason("UNISWAP_V3_0xabc_1") is None
    info = (int("ab" * 25, 16) << 56) | ((14109 & 0xFFFFFF) << 32) | ((-60 & 0xFFFFFF) << 8) | 1
    assert positions.parse_info(info) == ("0x" + "ab" * 25, 14109, -60)


def _merkl_world(tmp_path):
    from farm_radar import merkl_check
    conn = store.connect(tmp_path / "f.sqlite3")
    pid = "0x" + "cd" * 32
    st = {"poolId": pid, "weightFees": 7000, "weightToken0": 1500, "weightToken1": 1500, "isOutOfRangeIncentivized": False,
          "decimalsCurrency0": 18, "decimalsCurrency1": 6, "currency1": "0xUSD", "symbolCurrency0": "ETH",
          "symbolCurrency1": "USDG"}
    conn.execute("INSERT INTO merkl_campaigns(campaign_id, chain_id, type, settings_json, opportunity_id, first_seen, "
                 "last_seen) VALUES ('c1', 4663, 'UNISWAP_V4', ?, 'o1', 'x', 'x')", (json.dumps(st),))
    t = ["2026-10-04T00:00:00+00:00", "2026-10-04T02:00:00+00:00"]
    conn.execute("INSERT INTO merkl_reward_totals(ts, campaign_id, amount_raw) VALUES (?, 'c1', '1000000')", (t[0],))
    conn.execute("INSERT INTO merkl_reward_totals(ts, campaign_id, amount_raw) VALUES (?, 'c1', '2000000')", (t[1],))
    # 値段 ETH = 2500 USDG（sqrtPrice は raw の比: 2500 × 10^6 / 10^18）
    sq = math.sqrt(2500 * 1e6 / 1e18)
    tick = int(math.log(sq * sq) / math.log(1.0001))
    l_act = 10 ** 15
    for h in (1, 2):
        conn.execute("INSERT INTO pool_state_snaps(chain_id, pool_id, checked_at, kind, sqrt_price_x96, tick, liquidity) "
                     "VALUES (4663, ?, ?, 'v4', ?, ?, ?)",
                     (pid, f"2026-10-04T0{h}:00:00+00:00", str(int(sq * 2 ** 96)), tick, str(l_act)))
    conn.execute("INSERT INTO merkl_opportunity_snaps(ts, opportunity_id, tvl) VALUES ('2026-10-04T01:00:00+00:00', 'o1', 100000)")
    conn.execute("INSERT INTO token_prices(coin, ts, price) VALUES ('robinhood:0xusd', ?, 1.0)",
                 (int(datetime(2026, 10, 4, 1, tzinfo=UTC).timestamp()),))
    # 預け方 7（幅の中、流動性は全体の 10%）と 8（ずっと幅の外）
    for tid, lo, hi in ((7, tick - 600, tick + 600), (8, tick + 1200, tick + 2400)):
        for ts in t:
            conn.execute("INSERT INTO merkl_position_snaps(chain_id, token_id, ts, pool_id, tick_lower, tick_upper, liquidity) "
                         "VALUES (4663, ?, ?, ?, ?, ?, ?)", (tid, ts, pid, lo, hi, str(10 ** 14)))
    a0, a1 = merkl_check.amounts(1e14, sq, tick - 600, tick + 600)
    usd = a0 / 1e18 * 2500 + a1 / 1e6
    pred = 0.7 * 0.1 + 0.3 * usd / 100000
    got = int(round(pred * 1.5 * 1000000))           # 実際は見込みの 1.5 倍
    for tid, amt in ((7, got), (8, 0)):
        conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw) VALUES (?, 'c1', 'r', ?, ?)",
                     (t[1], f"UNISWAP_V4_{pid}_{tid}", str(amt)))
    conn.commit()
    return conn


def test_merkl_check_compares_actual_with_the_whole_pool_estimate(tmp_path):
    from farm_radar import merkl_check
    conn = _merkl_world(tmp_path)
    res = merkl_check.check(conn, {4663: "robinhood"})
    c = res["campaigns"][0]
    assert c["pair"] == "ETH/USDG" and c["intervals"] == 1 and c["pairs"] == 2 and c["pairs_in_range"] == 1
    assert c["ratio_median"] == pytest.approx(1.5, rel=1e-3)        # 今の見込みは実際の 1/1.5（控えめ）
    assert c["out_of_range_paid_share"] == 0.0                       # 幅の外の預け方は何ももらっていない
    assert res["ratio_median"] == pytest.approx(1.5, rel=1e-3)
    # コインの分の分母が預かり額の何割に見えるか: 実際のコインの分 = (実際 − 手数料の分の見込み) ÷ コインの重み
    pid = "0x" + "cd" * 32
    tok = conn.execute("SELECT amount_raw FROM merkl_reward_snaps WHERE reason=?", (f"UNISWAP_V4_{pid}_7",)).fetchone()[0]
    a0, a1 = merkl_check.amounts(1e14, math.sqrt(2500 * 1e6 / 1e18), *conn.execute(
        "SELECT tick_lower, tick_upper FROM merkl_position_snaps WHERE token_id=7").fetchone())
    tok_m = (a0 / 1e18 * 2500 + a1 / 1e6) / 100000
    assert c["denominator_share_median"] == pytest.approx(tok_m / ((int(tok) / 1e6 - 0.07) / 0.3), rel=1e-3)
    assert c["denominator_share_median"] < 1                          # 全員より少ない額が分母に見える
    # 区切りの間に量が変わった預け方は使わない
    conn.execute("UPDATE merkl_position_snaps SET liquidity='200000000000000' WHERE token_id=7 AND ts LIKE '%T02:%'")
    c = merkl_check.check(conn, {4663: "robinhood"})["campaigns"][0]
    assert c["pairs_in_range"] == 0 and c["skipped"]["区切りの間に預け方の量が変わった"] == 1


def test_merkl_check_sums_recipients_and_shows_in_trial_records(tmp_path):
    from farm_radar import merkl_check
    conn = _merkl_world(tmp_path)
    pid = "0x" + "cd" * 32
    base = merkl_check.check(conn, {4663: "robinhood"})["campaigns"][0]["ratio_median"]
    # 預け方 7 を人に渡した: 新しい受け取る人の分も足す（同じ区切りの中で、もらった額は 2 倍）
    got = conn.execute("SELECT amount_raw FROM merkl_reward_snaps WHERE reason LIKE '%_7'").fetchone()[0]
    conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw) VALUES "
                 "('2026-10-04T02:00:00+00:00', 'c1', 'r2', ?, ?)", (f"UNISWAP_V4_{pid}_7", got))
    conn.commit()
    assert merkl_check.check(conn, {4663: "robinhood"})["campaigns"][0]["ratio_median"] == pytest.approx(2 * base, rel=1e-6)
    conn.close()
    f = trial_records.feeds(tmp_path / "f.sqlite3", {4663: "robinhood"})
    assert f["merkl_positions"]["positions"] == 2 and f["merkl_positions"]["errors"] == 0
    assert f["merkl_check"]["pairs_in_range"] == 1
    # チェーンの値段の名前が分からないチェーンは比べない
    assert trial_records.feeds(tmp_path / "f.sqlite3")["merkl_check"]["campaigns"] == []
