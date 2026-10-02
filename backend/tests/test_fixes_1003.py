"""2026-10-03 オーナーの報告から直したことのテスト: 会場の見分けを1時間に1回にする、たまった数を出す、
保険に預けるお金を「探す」と同じ自動の計算にする（スコア）。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from farm_radar.db import database as db
from farm_radar.execution import hedge_guard
from farm_radar.feeds import status as feeds_status
from farm_radar.feeds import store
from farm_radar.feeds.run import every_due
from farm_radar.feeds.sources import BY_ID, SOURCES
from farm_radar.scoring.run import MarginBasis, score_venue

from .test_scoring_run import NOW, _ctx, _fill


def test_venue_checks_run_hourly_and_before_the_vault_watch():
    # 前は1日1回だったので、0件で「読めた」回のあと、その日はもう読まなかった（オーナーのパソコンで venue_checks 0）
    s = BY_ID["venue_checks"]
    assert s.cadence == "15min" and s.every_minutes == 55
    ids = [x.id for x in SOURCES]
    assert ids.index("venue_checks") < ids.index("vault_states")      # 工場が「作った」と答えた金庫を見張るため


def test_an_empty_ok_run_is_read_again_after_an_hour(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    now = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    rid = store.start_run(conn, "venue_checks", now - timedelta(hours=8))
    store.finish_run(conn, rid, now - timedelta(hours=8), "ok", items=0)
    assert every_due(conn, BY_ID["venue_checks"], now)                 # 同じ日でも読み直す
    rid = store.start_run(conn, "venue_checks", now - timedelta(minutes=10))
    store.finish_run(conn, rid, now - timedelta(minutes=10), "ok", items=3)
    assert not every_due(conn, BY_ID["venue_checks"], now)


def test_status_shows_how_many_are_stored(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    ts = "2026-10-03T00:00:00+00:00"
    for addr, ok in (("0x1", 1), ("0x2", 0), ("0x3", None)):          # None = 読み取り口の失敗（数えない）
        conn.execute("INSERT INTO venue_checks(chain_id, address, factory, venue_id, function, checked_at, verified) "
                     "VALUES (8453, ?, '0xf', 'morpho', 'isVaultV2(address)', ?, ?)", (addr, ts, ok))
    conn.execute("INSERT INTO vault_states(chain_id, address, venue_id, kind, checked_at) "
                 "VALUES (8453, '0x1', 'morpho', 'morpho_vault_v2', ?)", (ts,))
    conn.commit()
    assert feeds_status._stored(conn, "venue_checks") == {"checked": 2, "verified": 1}
    assert feeds_status._stored(conn, "vault_states") == {"vaults": 1, "errors": 0}
    assert feeds_status._stored(conn, "merkl_opportunities") is None


def test_margin_need_withstands_a_fifty_percent_rise():
    # ETH（Lighter: 最初に要る 5%・維持 1.2%）: 0.5 ＋ 1.5 × 0.012 = 0.518
    assert hedge_guard.margin_need(0.05, 0.012, 0.5) == pytest.approx(0.518)
    # ETH/USDG に $1,000（予備 $50 のとき）: プール 約 $755・保険 約 $195（SPEC 13.1 の「練習に入れること」1 の例と同じ）
    sp = hedge_guard.split(1000.0, 50.0, [(0.5, 0.518)])
    assert sp["pool"] == pytest.approx(754.6, abs=0.5) and sp["hedge_margin"] == pytest.approx(195.4, abs=0.5)
    assert sp["pool"] + sp["hedge_margin"] + sp["reserve"] == pytest.approx(1000.0)


def test_scores_use_the_automatic_split():
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    ctx = _ctx()
    ctx.margin = MarginBasis(withstand_rise=0.5, reserve_usd=20.0, mmf_fallback=0.05)
    rows = {r["pool_id"].split(":")[1]: r for r in score_venue(conn, ctx, now=NOW)}
    weth = json.loads(rows["p-weth"]["details_json"])["split"]
    # WETH だけ保険: 1ドルのプールに 0.5 × 0.575 を預ける。プール =（$1,000 − $20）÷ 1.2875
    assert weth["lp_usd"] == pytest.approx(980 / (1 + 0.5 * 0.575))
    assert weth["hedge_margin_usd"] == pytest.approx(weth["lp_usd"] * 0.2875)
    assert weth["lp"] + weth["hedge_margin"] + weth["reserve"] == pytest.approx(1.0)
    assert weth["needs"]["WETH"]["from_lighter"] is False
    # 保険の売り場がないコイン（UP）には預けない。UP/WETH は WETH の分だけ預ける
    up = json.loads(rows["p-up"]["details_json"])["split"]
    assert set(up["needs"]) == {"WETH"} and up["hedge_margin"] == pytest.approx(weth["hedge_margin"])
    # 保険がまったくなければ、予備のほかは全部プール
    none = hedge_guard.split(1000.0, 20.0, [])
    assert none["hedge_margin"] == 0.0 and none["pool"] == pytest.approx(980.0)
