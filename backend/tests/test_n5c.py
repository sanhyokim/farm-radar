"""2026-10-04 オーナー決定のテスト: 預け金は市場ごとの上げ（A）、資金調達料の控えめの見込みは 7日と30日の悪い方（B）。"""

import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from farm_radar import opportunities as opps
from farm_radar.execution import hedge_guard
from farm_radar.feeds import store

from .test_opportunities import CFG, NOW, _collect, cfg, feeds  # noqa: F401  （fixture を使う）


def _candles(conn, market_id, rise):
    """30日分の1時間の足。10日目から4日かけて 100 → 100 × (1 + rise)、そのあと元に戻る。"""
    start = int((NOW - timedelta(days=30)).timestamp())
    for h in range(30 * 24):
        t = start + h * 3600
        k = h - 10 * 24
        p = 100.0 * (1 + rise * min(1.0, max(0.0, k / 96))) if k < 200 else 100.0
        conn.execute("INSERT INTO lighter_price_history(market_id, ts, symbol, open, high, low, close) "
                     "VALUES (?,?,?,?,?,?,?)", (market_id, t, "ETH", p, p, p, p))


def test_withstand_is_the_larger_of_50_and_the_14_day_rise(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    _candles(conn, 0, 0.8)
    _candles(conn, 110, 0.2)
    conn.commit()
    table = hedge_guard.withstand_from_conn(conn, 14 * 86400)
    assert table[0] == pytest.approx(0.8) and table[110] == pytest.approx(0.2)
    assert hedge_guard.withstand_for(CFG, 0, table) == pytest.approx(0.8)
    assert hedge_guard.withstand_for(CFG, 110, table) == pytest.approx(0.5)      # 50% より下にしない
    assert hedge_guard.withstand_for(CFG, 999, table) == pytest.approx(0.5)      # 過去がない市場は 50%
    fixed = dataclasses.replace(CFG, opportunities=dataclasses.replace(CFG.opportunities, hedge_withstand_mode="fixed"))
    assert hedge_guard.withstand_for(fixed, 0, table) == pytest.approx(0.5)


def test_opportunity_margin_uses_the_market_rise_and_cautious_funding_uses_the_worse(cfg):
    conn = sqlite3.connect(cfg.feeds.database_path)
    _candles(conn, 0, 0.8)
    # 30日の売りの支払い: 1日 0.1%（1時間 0.0041667%。direction = short は売りが払う）。7日の見込みは 1日 0.03%
    start = int((NOW - timedelta(days=30)).timestamp())
    for h in range(30 * 24):
        conn.execute("INSERT INTO lighter_funding_history(market_id, ts, symbol, rate, value, direction) "
                     "VALUES (0, ?, 'ETH', ?, 0, 'short')", (start + h * 3600, 0.1 / 24))
    conn.commit()
    conn.close()
    o = _collect(cfg)["o-eth"]
    need = max(0.05, 0.8 + 1.8 * 0.03)                  # ETH: 最初に要る割合 5%、維持 3%
    for case in ("normal", "cautious"):
        v = o.calc["1000"][case]["hedge"]
        assert v.split["hedge_margin"] == pytest.approx(v.split["pool"] * 0.5 * need)
    normal, cautious = o.calc["1000"]["normal"]["hedge"], o.calc["1000"]["cautious"]["hedge"]
    exp_n, exp_c = normal.split["pool"] * 0.5, cautious.split["pool"] * 0.5
    assert normal.hedge_cost == pytest.approx(exp_n * 0.0003)
    assert cautious.hedge_cost == pytest.approx(exp_c * 0.001)


def test_funding_long_needs_half_of_the_days(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    start = int((NOW - timedelta(days=30)).timestamp())
    for h in range(10 * 24):                            # 10日分だけ（30日の半分より少ない）
        conn.execute("INSERT INTO lighter_funding_history(market_id, ts, symbol, rate, value, direction) "
                     "VALUES (5, ?, 'X', 0.01, 0, 'long')", (start + h * 3600,))
    for h in range(30 * 24):
        conn.execute("INSERT INTO lighter_funding_history(market_id, ts, symbol, rate, value, direction) "
                     "VALUES (6, ?, 'Y', 0.01, 0, 'long')", (start + h * 3600,))
    conn.commit()
    got = opps.funding_long(conn, NOW, 30)
    assert 5 not in got
    assert got[6] == pytest.approx(-0.0001 * 24)        # 買いが払う = 売りは受け取る（マイナス）
    assert opps.funding_long(conn, NOW, 0) == {}
