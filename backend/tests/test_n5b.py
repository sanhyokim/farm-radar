"""N5b（さかのぼりの計算）のテスト。外のサイトには行かず、仮の「今の版の写し」で確かめる。

- 同じ幅を値段の記録で動かす（置き直しの回数・幅の中の時間・値動きの損・費用）
- 保険の資金調達料（Lighter の記録）と預け金の余裕
- 早く出る決まり（段階1 プールのお金・段階2 報酬のコイン）を記録に当てはめる。10/1 の UP の例
"""

import json
import math
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from farm_radar import backtest as bt
from farm_radar.config import load_config
from farm_radar.db import database as db
from farm_radar.feeds import store

CFG = load_config()
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
UP = "0x57C0E45cB534413D1C20A4240955d6bB250BB4F1"
WETH = "0x" + "e" * 40
START = int(datetime(2026, 9, 28, 0, 0, tzinfo=UTC).timestamp())
STEP = 900
DAYS = 4


# --- 式 ---------------------------------------------------------------------------------------------

def test_segment_has_no_loss_at_entry_and_loses_when_price_moves():
    seg = bt.Segment.at(100.0, 0.05)
    assert seg.pnl(100.0) == pytest.approx(0.0, abs=1e-12)
    assert bt.lp_value(100.0, seg.pa, seg.pb, seg.liq) == pytest.approx(1.0)
    # 保険込みでは、上げても下げても損（値動きの損）。小さな動き ε では ε²/(4r)（見込みの式 σ²/(4r) と同じ形）
    for p in (99.0, 101.0, 90.0, 110.0):
        assert seg.pnl(p) < 0
    assert -seg.pnl(101.0) == pytest.approx(0.01 ** 2 / (4 * 0.05), rel=0.1)


def _pts(prices, start=START):
    return [(start + i * STEP, p, 1e18) for i, p in enumerate(prices)]


def test_replay_flat_price_never_rebalances():
    days = bt.replay(_pts([100.0] * (96 * 2 + 1)), 0.01, 900)
    for d in days.values():
        assert d.rebalances == 0 and d.gamma == pytest.approx(0.0, abs=1e-12)
        assert d.in_range_seconds == d.seconds


def test_replay_waits_before_rebalancing_and_counts_cost():
    # 2回目の記録で幅の外に出て、そのまま。待ち時間（15分）がたった次の記録で置き直す
    prices = [100.0, 103.0, 103.0, 103.0]
    calls = []
    days = bt.replay(_pts(prices), 0.02, 900, lambda p, liq: (calls.append(p), (0.001, 0.0002))[1])
    d = next(iter(days.values()))
    assert d.rebalances == 1 and calls == [103.0]
    assert d.rebalance_cost == pytest.approx(0.001) and d.rebalance_slips == [0.0002]
    assert d.in_range_seconds == 2 * STEP                # 外に出ていた15分は数えない（置き直したあとは幅の中）
    assert d.gamma > 0                                   # 外に出た分の損は、置き直したときに決まる
    # すぐに戻ったら置き直さない
    days = bt.replay(_pts([100.0, 103.0, 100.0, 100.0]), 0.02, 900)
    assert sum(x.rebalances for x in days.values()) == 0


def test_replay_matches_the_formulas_on_a_random_walk():
    """値動きの損の式 σ²/(4r) は、ランダムな値動きで動かした損とおおむね合う（30%以内）。"""
    random.seed(7)
    sigma, p, prices = 0.02, 100.0, []
    for _ in range(96 * 120):
        prices.append(p)
        p *= math.exp(random.gauss(0, sigma * math.sqrt(STEP / 86400)))
    days = sorted(bt.replay(_pts(prices), 0.05, 900).items())[1:-1]
    g = sum(d.gamma for _, d in days) / len(days)
    assert g == pytest.approx(sigma ** 2 / (4 * 0.05), rel=0.3)


# --- 仮の「今の版の写し」 -------------------------------------------------------------------------------

def _sqrt_x96(price, d0, d1):
    return str(int(math.sqrt(price * 10 ** (d1 - d0)) * 2 ** 96))


@pytest.fixture
def old_copy(tmp_path):
    """4日分（15分ごと）の up. の2つのプール（WETH/USDG・UP/WETH）、毎時のスコア、3日目の昼にプールのお金が −40%、
    3〜4日目に UP が 0.42 → 0.33（24時間で約 −21%）。"""
    conn = db.connect(tmp_path / "old.sqlite3")
    conn.execute("INSERT INTO venues(id, name, chain) VALUES ('up-robinhood', 'up.', 'robinhood')")
    pools = {
        "up-robinhood:0xaaa": (WETH, USDG, "WETH", "USDG", 18, 6),
        "up-robinhood:0xbbb": (UP, WETH, "UP", "WETH", 18, 18),
    }
    for pid, (t0, t1, s0, s1, d0, d1) in pools.items():
        conn.execute("""INSERT INTO pools(id, venue_id, address, token0, token1, fee_tier, is_stock_pair, discovered_at,
                        token0_symbol, token1_symbol, token0_decimals, token1_decimals)
                        VALUES (?, 'up-robinhood', ?, ?, ?, 3000, 0, '2026-09-27', ?, ?, ?, ?)""",
                     (pid, pid.split(":")[1], t0, t1, s0, s1, d0, d1))
    random.seed(3)
    eth = 2700.0
    n = 96 * DAYS + 1
    for i in range(n):
        t = START + i * STEP
        ts = datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")
        run = conn.execute("INSERT INTO collection_runs(venue_id, slot, started_at, status) VALUES "
                           "('up-robinhood', ?, ?, 'ok')", (ts, ts)).lastrowid
        up_usd = 0.42 if i < 96 * 2 else 0.42 - 0.09 * min(1.0, (i - 96 * 2) / 96)
        # 3日目の 12:00 から、プールのお金（コインの量）が 40% 減る
        frac = 0.6 if i >= 96 * 2 + 48 else 1.0
        rows = [("up-robinhood:0xaaa", eth, 18, 6, int(100 * frac * 1e18), int(270000 * frac * 1e6)),
                ("up-robinhood:0xbbb", up_usd / eth, 18, 18, int(1e6 * 1e18), int(150 * 1e18))]
        for pid, price, d0, d1, b0, b1 in rows:
            conn.execute("""INSERT INTO pool_snapshots(pool_id, ts, block_number, run_id, price, sqrt_price_x96,
                            liquidity_total, balance0_raw, balance1_raw, block_time, source)
                            VALUES (?,?,?,?,?,?,?,?,?,?, 'test')""",
                         (pid, ts, i, run, price, _sqrt_x96(price, d0, d1), str(10 ** 15), str(b0), str(b1), ts))
        eth *= math.exp(random.gauss(0, 0.03 * math.sqrt(STEP / 86400)))
    det = {"split": {"lp": 0.78},
           "inputs": {"usd": {"WETH": 2700.0, "USDG": 1.0}, "fee": 0.003, "gas_usd_per_tx": 0.15, "slippage": 0.0005,
                      "hedge": {"WETH": {"hedge_id": "lighter", "symbol": "ETH", "market_id": 0, "funding_daily": 0.0003}}}}
    for h in range(DAYS * 24 + 1):
        ts = datetime.fromtimestamp(START + h * 3600, UTC).isoformat(timespec="seconds")
        conn.execute("INSERT INTO scores(pool_id, ts, best_r, sigma_pair, details_json) VALUES (?,?,?,?,?)",
                     ("up-robinhood:0xaaa", ts, 2.0, 0.03, json.dumps(det)))
    conn.commit()
    return conn


@pytest.fixture
def feeds(tmp_path):
    conn = store.connect(tmp_path / "feeds.sqlite3")
    for h in range(DAYS * 24 + 24):
        conn.execute("INSERT INTO lighter_funding_history(market_id, ts, symbol, rate, value, direction) "
                     "VALUES (0, ?, 'ETH', 0.0012, 0, 'short')", (START + h * 3600,))
    conn.commit()
    return conn


def test_backtest_on_a_copy(old_copy, feeds):
    res = bt.run(old_copy, CFG, feeds)
    assert res["pools"] == 2
    # ②③④: 置き始めた日を除く3日。スコアがあるのは WETH/USDG だけ
    assert res["pool_days"] == 3
    rows = {x["r_pct"]: x for x in res["ranges"]["all"]}
    assert set(rows) == set(CFG.scoring.ranges_pct)
    r2 = rows[2.0]
    assert r2["days"] == 3
    assert r2["rebalances"]["pred"] == pytest.approx((0.03 / 0.02) ** 2)
    assert r2["gamma"]["pred"] == pytest.approx(0.03 ** 2 / (4 * 0.02))
    assert 0 <= r2["in_range"]["real"] <= 1 and r2["sigma"]["real"] > 0
    assert r2["cost"]["slip_pred"] == pytest.approx(0.0005)
    assert res["per_pool_best"][0]["pair"] == "WETH/USDG"
    # ⑤ 保険: 見込み 0.03%/日、記録 0.0012%/時 × 24 = 0.0288%/日（売りが払う）
    f = res["funding"][0]
    assert f["perp"] == "ETH" and f["days"] == 4
    assert f["pred"] == pytest.approx(0.0003) and f["real"] == pytest.approx(0.000288)
    assert f["ok_share"] == 1.0 and f["ok_share_loose"] == 1.0
    m = {x["symbol"]: x for x in res["margins"]}
    assert m["WETH"]["enough"] and m["WETH"]["withstand_pct"] == 50
    # ⑥ 段階1: −40% は 30% の線では出て、50% の線では出ない（4つの線の回数を並べる）
    assert res["stage1"]["counts"]["30"] == 1 and res["stage1"]["counts"]["40"] == 1
    assert res["stage1"]["counts"]["50"] == 0 and res["stage1"]["counts"]["20"] == 1
    e = res["stage1"]["events"][0]
    assert e["pair"] == "WETH/USDG" and e["change_pct"] == pytest.approx(-40, abs=0.5)
    # 段階1 の中身（オーナーの質問2）: 大きいプール（約 $54万）で、戻らず、コインの大きな値下がりもない → 「どれでもない」
    assert e["funds_before_usd"] > 500_000 and not e["small"] and not e["recovered_6h"] and not e["big_drop"]
    assert res["stage1"]["breakdown"] == {"total": 1, "small": 0, "recovered_6h": 0, "big_drop": 0, "none": 1}
    assert res["stage1"]["counts_big_pools"]["30"] == 1
    # 5万ドル以上のプールだけの中身と1回ずつ（2026-10-04 オーナーの追加2）
    assert res["stage1"]["breakdown_big_pools"] == {"total": 1, "small": 0, "recovered_6h": 0, "big_drop": 0, "none": 1}
    assert [x["pair"] for x in res["stage1"]["big_pool_events"]] == ["WETH/USDG"]
    # 種類ごと（オーナーの質問3）: WETH/USDG = ふつうのコイン、UP/WETH = ボーナスのコイン。中央値も出す
    assert res["kinds"] == {"stock": 0, "stable": 0, "bonus": 1, "coin": 1}
    k2 = {x["r_pct"]: x for x in res["ranges"]["kind_coin"]}[2.0]
    assert k2["days"] == 3 and k2["rebalances"]["pred_median"] == pytest.approx(2.25)
    assert k2["gamma"]["real_median"] is not None and res["ranges"]["kind_stock"] == []
    assert res["misses"][0]["pair"] == "WETH/USDG" and res["spy"] == []
    mi = res["misses"][0]
    assert mi["gap_usd_day"] == pytest.approx(abs(mi["loss_pred_usd_day"] - mi["loss_real_usd_day"]))
    assert [x["gap_usd_day"] for x in res["misses"]] == sorted([x["gap_usd_day"] for x in res["misses"]], reverse=True)
    # 段階2: UP は24時間で約 −21%。−15% と −20% の線では出て、−25% では出ない
    up = res["stage2_reward"]["tokens"][0]
    assert up["symbol"] == "UP"
    assert len(up["events"]["-15"]) == 1 and len(up["events"]["-20"]) == 1 and up["events"]["-25"] == []
    assert up["events"]["-15"][0]["at"] < up["events"]["-20"][0]["at"]     # 線が浅いほど早く出る


def test_stage1_detail_splits_small_recovered_and_big_drop():
    t = START + 10 * 3600
    funds = [(t, 30_000.0, 50_000.0), (t + 3600, 48_000.0, 30_000.0)]       # $5万 → $3万、1時間後に $4.8万（90%以上）
    coin = [(t - 900, 10.0), (t + 3600, 7.5)]                                # そのあと24時間でコインが −25%
    d = bt.stage1_detail(t, funds, [coin])
    assert d["small"] is False and d["funds_before_usd"] == 50_000
    assert d["recovered_6h"] and d["big_drop"] and d["coin_min_24h_pct"] == pytest.approx(-25)
    assert bt.stage1_detail(t, [(t, 1.0, 40_000.0)], [])["small"]


def test_median():
    assert bt._median([3, 1, 2]) == 2 and bt._median([1, 2, 3, 100]) == 2.5 and bt._median([]) is None


def test_cached_reuses_result_until_the_copy_changes(tmp_path, old_copy, monkeypatch):
    data = tmp_path / "data"
    (data / "import").mkdir(parents=True)
    old_copy.close()
    (tmp_path / "old.sqlite3").replace(data / "import" / "old-18000.sqlite3")
    (data / "import" / "old-18000.json").write_text(json.dumps({"copied_at": "2026-10-03T08:31:00+00:00",
                                                                "integrity": "ok", "health_match": True}))
    calls = []
    real = bt.run
    monkeypatch.setattr(bt, "run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    a = bt.cached(data, CFG)
    b = bt.cached(data, CFG)
    assert a["present"] and a["result"]["pools"] == 2 and len(calls) == 1 and b["key"] == a["key"]
    (data / "import" / "old-18000.json").write_text(json.dumps({"copied_at": "2026-10-04T00:00:00+00:00"}))
    bt.cached(data, CFG)
    assert len(calls) == 2
    assert not bt.cached(tmp_path / "none", CFG)["present"]


def test_up_early_exit_case_with_the_new_line():
    """10/1 の UP の例（docs/cases/early-exit-up-2026-10-01.md。チェーンの記録）: −20% の線では 12:45 UTC に出たが、
    今の線（−15%。2026-10-03 オーナー決定①A）なら 12:30 UTC（−16.5%）に出て、UP は $0.3526 で売れた（12:45 は $0.3286）。"""
    def t(s):
        return int(datetime.fromisoformat(s + "+00:00").timestamp())
    pts = [(t("2026-09-30T12:00:00"), 0.413131), (t("2026-09-30T12:15:00"), 0.413613),
           (t("2026-09-30T12:30:00"), 0.422202), (t("2026-09-30T12:45:00"), 0.425782),
           (t("2026-10-01T12:00:00"), 0.352607), (t("2026-10-01T12:15:00"), 0.352932),
           (t("2026-10-01T12:30:00"), 0.352617), (t("2026-10-01T12:45:00"), 0.328572)]
    chs = bt.changes(pts, 24)
    assert [round(c * 100, 2) for _, _, c in chs] == [-14.65, -14.67, -16.48, -22.83]
    e15 = bt.events(chs, lambda c: c * 100 <= -15, pts)
    e20 = bt.events(chs, lambda c: c * 100 <= -20, pts)
    assert e15[0]["at"] == t("2026-10-01T12:30:00") and e15[0]["price"] == pytest.approx(0.352617)
    assert e20[0]["at"] == t("2026-10-01T12:45:00") and e20[0]["price"] == pytest.approx(0.328572)
    assert e15[0]["min_24h_pct"] == pytest.approx((0.328572 / 0.352617 - 1) * 100)


def test_funding_actual_uses_direction(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    conn.executemany("INSERT INTO lighter_funding_history(market_id, ts, symbol, rate, direction) VALUES (?,?,?,?,?)",
                     [(1, 0, "NVDA", 0.0004, "long"), (1, 3600, "NVDA", 0.0004, "long")])
    assert bt.funding_actual(conn, 1, 0, 7200) == pytest.approx(-0.0004 / 100 * 24)   # 買いが払う = 売りは受け取る
    assert bt.funding_actual(conn, 2, 0, 7200) is None
    assert bt.funding_actual(sqlite3.connect(":memory:"), 1, 0, 7200) is None


def test_api_trial_backtest(tmp_path, old_copy, monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    from farm_radar import api

    data = tmp_path / "data"
    (data / "import").mkdir(parents=True)
    old_copy.close()
    (tmp_path / "old.sqlite3").replace(data / "import" / "old-18000.sqlite3")
    cfg = dataclasses.replace(CFG, database_path=data / "farm_radar.sqlite3",
                              feeds=dataclasses.replace(CFG.feeds, database_path=data / "feeds.sqlite3"))
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    body = TestClient(api.app).get("/api/trial/backtest").json()
    assert body["present"] and body["result"]["pools"] == 2
    assert body["result"]["funding"] == []          # 新しい版の Lighter の記録がまだない
    (data / "import" / "backtest.json").write_text(json.dumps(body), encoding="utf-8")
    json.dumps(body["result"])                       # 画面と1行のまとめにそのまま渡せる


def test_lighter_history_also_reads_the_hedge_markets_of_up(tmp_path):
    """今の版の保険の銘柄（株など）は、Merkl の一覧や取引の多い10銘柄に入らないことがあるので、別に読む（N5b）。"""
    from farm_radar.feeds import trial

    conn = store.connect(tmp_path / "f.sqlite3")
    conn.executemany("INSERT INTO lighter_markets(market_id, symbol, status, daily_quote_volume, updated_at) "
                     "VALUES (?,?,?,?, 'x')", [(0, "ETH", "active", 1e9), (110, "NVDA", "active", 1.0),
                                              (999, "OLD", "inactive", 0.0)])
    ctx = trial.TrialContext(lighter_markets=((110, "NVDA"), (999, "OLD")))
    assert (110, "NVDA") in trial.lighter_targets(conn, ctx)
    assert all(mid != 999 for mid, _ in trial.lighter_targets(conn, ctx))      # 止まった市場は読まない


def test_funding_match_uses_the_rate_not_a_dollar_floor():
    # 2026-10-04 オーナーの質問3: QQQ の見込み −0.0122 / 実際 −0.0309 %/日 は、前の数え方（資金の0.1% = $1/日まで）だと
    # いつも「合」になっていた。割合（30%）とごく小さい率の余裕（1日 0.002%）で見る
    s = bt.BacktestSettings.from_config(load_config())
    rows = [{"perp": "QQQ", "symbol": "QQQ", "day": f"2026-10-0{i}", "pred": -0.000122, "real": -0.000309,
             "notional": 390.0} for i in (1, 2, 3)]
    rows.append({"perp": "ETH", "symbol": "WETH", "day": "2026-10-01", "pred": 0.0003, "real": 0.00025, "notional": 390.0})
    rows.append({"perp": "TINY", "symbol": "TINY", "day": "2026-10-01", "pred": 0.000001, "real": 0.000015, "notional": 390.0})
    out = {x["perp"]: x for x in bt._summarize_funding(rows, s)}
    assert out["QQQ"]["ok_share"] == 0.0 and out["QQQ"]["ok_share_loose"] == 1.0
    assert out["ETH"]["ok_share"] == 1.0                     # 差 17%
    assert out["TINY"]["ok_share"] == 1.0                    # 差 0.0014%/日（余裕の中）


def test_miss_is_the_dollar_gap_of_daily_loss():
    x = {"gamma_pred": 0.001, "gamma_real": 0.0004, "cost_pred": 0.002, "cost_real": 0.0, "c_lp": 780.0}
    m = bt.miss(x)
    assert m["loss_pred_usd_day"] == pytest.approx(2.34) and m["loss_real_usd_day"] == pytest.approx(0.312)
    assert m["gap_usd_day"] == pytest.approx(2.028)


def test_max_rise_on_hourly_candles_matches_a_plain_search():
    rnd = random.Random(7)
    c, p, t = [], 100.0, 0
    for _ in range(600):
        o = p
        p *= math.exp(rnd.gauss(0, 0.01))
        c.append((t, max(o, p) * 1.002, min(o, p) * 0.998, p))
        t += 3600
    t_gap = c[-1][0] + 10 * 86400                         # 窓より長くあいた記録も止まらない
    c.append((t_gap, 130.0, 120.0, 125.0))
    w = 14 * 86400
    got = bt.max_rise_hl(c, w)
    best = 0.0
    for j in range(1, len(c)):
        lows = [x[2] for x in c[:j] if c[j][0] - x[0] <= w]
        if lows:
            best = max(best, c[j][1] / min(lows) - 1)
    assert got["rise_pct"] == pytest.approx(best * 100)
    assert got["jump_up_pct"] == pytest.approx(max(b[1] / a[3] - 1 for a, b in zip(c[:-1], c[1:-1])) * 100)


def test_lighter_price_history_feed_and_long_margins(tmp_path):
    from farm_radar.feeds import trial
    conn = store.connect(tmp_path / "feeds.sqlite3")
    conn.execute("INSERT INTO lighter_markets(market_id, symbol, status, daily_quote_volume, updated_at) "
                 "VALUES (0, 'ETH', 'active', 1, '2026-10-04T00:00:00+00:00')")
    conn.commit()
    now = datetime(2026, 10, 4, tzinfo=UTC)
    start = int(now.timestamp()) - 90 * 86400
    calls = []

    def text(url, params):
        calls.append((url, dict(params)))
        end = int(params["end_timestamp"])
        lo = max(int(params["start_timestamp"]), end - 500 * 3600 + 1)
        first = (lo + 3599) // 3600 * 3600
        cs = [{"t": tt * 1000, "o": 100.0, "h": 101.0 + (tt == start + 3600 * 100) * 60, "l": 99.0, "c": 100.0}
              for tt in range(first, end + 1, 3600)]
        return json.dumps({"code": 200, "r": "1h", "c": cs})

    ctx = trial.TrialContext(lighter_markets=((0, "ETH"),))
    data, pages = trial.read_lighter_prices(conn, text, ctx, now)
    assert calls[0][0].endswith("/candles") and pages == 5               # 2161点を500点ずつ
    n = trial.write_lighter_prices(conn, data)
    assert n == 90 * 24 + 1                                              # 90日前の足から今の足まで
    data, _ = trial.read_lighter_prices(conn, text, ctx, now + timedelta(hours=3))
    assert len(data["0"]["points"]) == 4                                  # 最後の足を読み直して、そのあとの分だけ
    s = bt.BacktestSettings.from_config(load_config())
    m = bt.margins_long(conn, {0: "ETH"}, s)
    assert m[0]["symbol"] == "ETH" and m[0]["rise_pct"] == pytest.approx(161 / 99 * 100 - 100)
    assert not m[0]["enough"] and m[0]["span_days"] > 89


def test_lighter_price_history_keeps_points_when_the_oldest_page_is_refused(tmp_path):
    from farm_radar.external.http import ExternalError
    from farm_radar.feeds import trial
    conn = store.connect(tmp_path / "feeds.sqlite3")
    conn.execute("INSERT INTO lighter_markets(market_id, symbol, status, daily_quote_volume, updated_at) "
                 "VALUES (191, 'NOW', 'active', 1, '2026-10-04T00:00:00+00:00')")
    conn.commit()
    now = datetime(2026, 10, 4, tzinfo=UTC)
    n = []

    def text(url, params):
        n.append(1)
        if len(n) > 1:                       # 新しい市場で、いちばん古い足まで読んだあと
            raise ExternalError("400 invalid timestamps", status=400)
        end = int(params["end_timestamp"])
        return json.dumps({"c": [{"t": (end - 3600 * i) * 1000, "o": 1, "h": 1, "l": 1, "c": 1} for i in range(500)]})

    data, _ = trial.read_lighter_prices(conn, text, trial.TrialContext(lighter_markets=((191, "NOW"),)), now)
    assert len(data["191"]["points"]) == 500 and "error" not in data["191"]


# --- 2026-10-04 オーナーの質問3・4 ------------------------------------------------------------------------

def test_still_share_counts_unchanged_neighbours():
    assert bt.still_share([(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.1)]) == pytest.approx(2 / 3)
    assert bt.still_share([(0, 5.0)]) == 1.0


def test_pool_with_a_still_price_is_left_out_of_the_comparison(old_copy, feeds):
    """値段が動いていない（取引がない）プールは、②③④ と「ずれの大きいプール」から外して、別に並べる。"""
    pid = "up-robinhood:0xccc"
    old_copy.execute("""INSERT INTO pools(id, venue_id, address, token0, token1, fee_tier, is_stock_pair, discovered_at,
                        token0_symbol, token1_symbol, token0_decimals, token1_decimals)
                        VALUES (?, 'up-robinhood', '0xccc', ?, ?, 3000, 0, '2026-09-27', 'WETH', 'WOOD', 18, 18)""",
                     (pid, WETH, "0x" + "d" * 40))
    for i in range(96 * DAYS + 1):
        ts = datetime.fromtimestamp(START + i * STEP, UTC).isoformat(timespec="seconds")
        old_copy.execute("""INSERT INTO pool_snapshots(pool_id, ts, block_number, run_id, price, sqrt_price_x96,
                            liquidity_total, balance0_raw, balance1_raw, block_time, source)
                            VALUES (?,?,?,1,?,?,?,?,?,?, 'test')""",
                         (pid, ts, i, 1234.5, _sqrt_x96(1234.5, 18, 18), str(10 ** 15), str(10 ** 18), str(10 ** 21), ts))
    det = {"split": {"lp": 0.78}, "inputs": {"usd": {"WETH": 2700.0}, "fee": 0.003, "gas_usd_per_tx": 0.15}}
    for h in range(DAYS * 24 + 1):
        ts = datetime.fromtimestamp(START + h * 3600, UTC).isoformat(timespec="seconds")
        old_copy.execute("INSERT INTO scores(pool_id, ts, best_r, sigma_pair, details_json) VALUES (?,?,?,?,?)",
                         (pid, ts, 2.0, 0.63, json.dumps(det)))
    old_copy.commit()
    res = bt.run(old_copy, CFG, feeds)
    assert [x["pair"] for x in res["still_pools"]] == ["WETH/WOOD"]
    assert res["still_pools"][0]["still_share"] == 1.0
    assert "WETH/WOOD" not in {x["pair"] for x in res["per_pool_best"]}
    assert "WETH/WOOD" not in {x["pair"] for x in res["misses"]}
    assert res["pool_days"] == 3                                   # WETH/USDG の3日だけ（前と同じ）


def test_long_margins_cover_every_hedge_market_of_up():
    """預け金（90日）は、写しで資金調達料を比べた市場だけでなく、up. の保険に使う市場すべて（perps.map）を見る。"""
    from farm_radar.tokens import load_tokens
    book = load_tokens(root=CFG.root)
    lighter = {r.market_id for refs in book.perp_alts.values() for r in refs if r.venue == "lighter"}
    got = bt.hedge_markets(book, [{"market_id": 0, "perp": "ETH", "symbol": "WETH"}])
    assert set(got) == lighter | {0}
    assert len(got) >= 29 and got[0] == "ETH"
    assert got[139] == "SNDK" and got[189] == "NBIS"
