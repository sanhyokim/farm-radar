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
