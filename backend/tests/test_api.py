"""画面用API（M3）のテスト。M2 のテストと同じ偽データで、計算した結果が画面の形で返ることを確かめる。"""

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, views
from farm_radar.config import load_config
from farm_radar.db import database as db
from farm_radar.scoring.run import score_venue

from .test_scoring_run import NOW, FakeGT, _ctx, _fill


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    _fill(conn, 24 * 8)
    for h in range(30, -1, -1):      # 30時間分のスコア（1時間ごと）
        score_venue(conn, _ctx(FakeGT()), now=NOW - timedelta(hours=h, minutes=-1))
    conn.close()
    cfg = dataclasses.replace(load_config(), database_path=path)
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(minutes=5))
    return TestClient(api.app)


def test_home_has_mode_summary_counts_and_market(client):
    d = client.get("/api/home").json()
    assert d["mode"] in ("observe", "paper")
    assert sum(d["counts"].values()) == 3
    assert d["summary"].startswith("攻め候補")
    assert d["judge_basis"] == "総資産あたりの純日利（%）"
    assert "us_open" in d["market"] and d["market"]["gas_usd_per_tx"] is not None
    assert len(d["near"]) <= 3


def test_pool_detail_has_six_parts_that_add_up(client):
    d = client.get("/api/pools/up-robinhood:p-weth").json()
    b = d["daily"]["breakdown"]
    parts = sum(b[k] for k in views.CATEGORIES)
    assert parts == pytest.approx(b["net"])
    # 純損益（ドル）÷ 総資産 = 判定に使う日利
    assert b["net"] / d["capital"]["total"] * 100 == pytest.approx(d["daily"]["net_daily_pct"])
    assert b["hedge_gap"] == pytest.approx(b["direction"] + b["hedge"])
    assert b["core"] == pytest.approx(b["income"] + b["gamma"])
    assert d["daily"]["apy_note"] == views.APY_NOTE
    assert d["daily"]["net_daily_pct_lp"] and d["daily"]["judge_basis"]
    # 1時間ごとの棒グラフ（48本）、24時間移動平均、いちばん良い時間・悪い時間
    bars = d["hourly"]["bars"]
    assert len(bars) == 48 and any(x["net_usd"] is not None for x in bars)
    assert d["hourly"]["best"]["net_usd"] >= d["hourly"]["worst"]["net_usd"]
    assert d["since_start"]["days"] >= 1 and d["today"] is not None
    assert d["assets"]["estimated"] and d["assets"]["series"]
    assert d["history"] and "us_open" in d["history"][0]
    # 参考値「報酬をすぐ売る前提」: 判定の日利と並べて出す（判定には使わない）
    sn = d["sell_now"]
    assert sn["hours"] == 1 and "判定には使いません" in sn["note"]
    assert sn["hold_net_daily_pct"] == pytest.approx(d["daily"]["net_daily_pct"])
    assert sn["diff_pct"] == pytest.approx(sn["net_daily_pct"] - sn["hold_net_daily_pct"])


def test_unknown_pool_is_404_and_ui_fallback(client, tmp_path, monkeypatch):
    assert client.get("/api/pools/nope").status_code == 404
    ui = tmp_path / "dist"
    ui.mkdir()
    (ui / "index.html").write_text("<html>farm radar</html>", encoding="utf-8")
    monkeypatch.setenv("FARM_RADAR_UI_DIR", str(ui))
    assert "farm radar" in client.get("/pools/abc").text
    assert client.get("/api/nothing").status_code == 404


def test_venues_have_condition_lamps_and_trends(client):
    v = client.get("/api/venues").json()["venues"][0]
    assert [c["code"] for c in v["conditions"]] == ["C1", "C2", "C3", "C4"]
    assert all(c["state"] in ("ok", "warn", "bad", "unknown") for c in v["conditions"])
    assert v["age_days"] > 0 and v["tvl"]


def test_breakdown_luck_ratio():
    b = views.breakdown(income=10, gamma=2, rebalance=1, hedge=0, haircut=0, direction_risk=0)
    assert b["core"] == 8 and b["net"] == 7
    assert b["luck_ratio"] == pytest.approx(1 / 9) and not b["lucky"]
    b = views.breakdown(income=10, gamma=2, rebalance=1, hedge=0, haircut=6, direction_risk=3)
    assert b["lucky"]   # 本業以外（−10）が本業（8）より大きい
