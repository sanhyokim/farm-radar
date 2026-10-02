"""保険の守り（N4b）のテスト: 預け金の上限・強制決済までの余裕と知らせ・試しのボタン・幅から出たときの保険。"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from farm_radar import api
from farm_radar.execution import hedge_guard
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import PaperExecutor

from .test_paper import FakeFx, _config, _extend, _open, world  # noqa: F401
from .test_risk import _events, _pos, _push_out, _set_score, calm  # noqa: F401
from .test_scoring_run import NOW, TOKENS


def test_edge_zone_uses_ten_percent_of_the_range():
    # ±2% の幅なら境目の余裕は 0.2%
    lo, up = 98.0, 102.0
    assert hedge_guard.edge_zone(102.1, lo, up, 0.02, 0.1) == "edge"        # 境目の外でも 0.2% 以内
    assert hedge_guard.edge_zone(102.3, lo, up, 0.02, 0.1) == "above"
    assert hedge_guard.edge_zone(97.7, lo, up, 0.02, 0.1) == "below"
    assert hedge_guard.edge_zone(101.9, lo, up, 0.02, 0.1) == "edge"        # 中でも境目から 0.2% 以内
    assert hedge_guard.edge_zone(100.0, lo, up, 0.02, 0.1) == "inside"


def test_margin_is_counted_and_buffer_shrinks_as_price_rises(world):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    pos = _pos(conn, ref.position_id)
    cfg = _config(path)
    assert json.loads(pos["state_json"])["hedge_margin"] == pytest.approx(400.0)   # $1,000 の 40%
    ms0 = hedge_guard.margin_status(conn, cfg, pos, mmf_table={})
    assert ms0["state"] == "ok" and ms0["buffer_frac"] == pytest.approx(1.0, abs=0.02)
    assert ms0["mmf_from_lighter"] is False                       # Lighter の値がなければ仮の値（5%）
    assert ms0["maintenance_usd"] == pytest.approx(ms0["notional_usd"] * 0.05)
    assert hedge_guard.mmf_for(cfg, 1, {1: 0.03}) == (0.03, True)
    # 強制決済の線までの上がり幅ちょうどで、余裕がほぼ0になる
    liq = ms0["to_liquidation_pct"]
    assert hedge_guard.margin_status(conn, cfg, pos, rise_pct=liq, mmf_table={})["buffer_usd"] == pytest.approx(0.0, abs=0.01)
    half = hedge_guard.margin_status(conn, cfg, pos, rise_pct=liq * 0.6, mmf_table={})
    assert half["state"] == "alert" and half["add_to_restore_usd"] > 0
    assert "お金を足す" in hedge_guard.message_ja("WETH/USDG", half)
    assert hedge_guard.margin_status(conn, cfg, pos, rise_pct=liq * 1.1, mmf_table={})["state"] == "liquidated"


def test_hedge_test_button_only_calculates(world, monkeypatch):  # noqa: F811
    path, conn = world
    monkeypatch.setattr(api, "load_config", lambda: _config(path))
    ex, ref = _open(conn, path)
    before = _pos(conn, ref.position_id)["state_json"]
    c = TestClient(api.app)
    r = c.get(f"/api/paper/positions/{ref.position_id}/hedge-test", params={"rise_pct": 150})
    assert r.status_code == 200
    d = r.json()
    assert d["status"]["state"] in ("alert", "liquidated") and d["would_ja"] and d["options"]["exit"]["close_cost_usd"] > 0
    assert _pos(conn, ref.position_id)["state_json"] == before                 # 建玉は変えない
    assert c.get(f"/api/paper/positions/{ref.position_id}").json()["hedge_margin"]["state"] == "ok"


def test_liquidation_is_stage_one_and_closes_the_position(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    _extend(conn, 1)
    orig = hedge_guard.margin_status

    def broke(conn, config, pos, rise_pct=0.0, mmf_table=None):
        ms = orig(conn, config, pos, rise_pct, mmf_table)
        return ms and {**ms, "state": "liquidated", "equity_usd": 1.0}

    import farm_radar.execution.risk_job as rj
    rj.hedge_guard.margin_status, saved = broke, rj.hedge_guard.margin_status
    try:
        run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    finally:
        rj.hedge_guard.margin_status = saved
    p = _pos(conn, ref.position_id)
    assert p["status"] == "closed" and p["close_reason"] == "emergency:hedge_liquidation"
    ev = [e for e in _events(conn) if e["kind"] == "hedge_liquidation"][0]
    assert json.loads(ev["data_json"])["stage"] == 1


def test_out_of_range_hedge_waits_30_minutes_then_matches_and_cools_down(world, calm, monkeypatch):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    pid = ref.position_id
    # ガス代が高くて置き直しを見送っている間に、値段が幅の上（境目の余裕より外）に出たまま
    monkeypatch.setattr(PaperExecutor, "gas_too_high", lambda self, pool_id: (True, 9.0))
    _push_out(conn, pid, 1)
    last = datetime.fromisoformat(conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0])
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=last + timedelta(minutes=1))
    assert not [e for e in _events(conn) if e["kind"] == "hedge_adjust"]       # まだ30分たっていない
    size_before = json.loads(_pos(conn, pid)["hedges_json"])[0]["size"]
    _push_out(conn, pid, 1)
    last2 = datetime.fromisoformat(conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0])
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=last2 + timedelta(minutes=1))
    ev = [e for e in _events(conn) if e["kind"] == "hedge_adjust"]
    assert len(ev) == 1 and ev[0]["action"] == "hedge_matched"
    h = json.loads(_pos(conn, pid)["hedges_json"])
    pool = conn.execute("SELECT * FROM pools WHERE id=?", (_pos(conn, pid)["pool_id"],)).fetchone()
    tok = h[0]["token"]
    side = json.loads(ev[0]["data_json"])["zone"]
    assert side == "above"
    # 幅の上では値動きするコインの中身が0 → 保険も0（WETH/USDG の WETH）
    assert pool["token0"].lower() == tok and h[0]["size"] == pytest.approx(0.0, abs=1e-12) and size_before > 0
    st = json.loads(_pos(conn, pid)["state_json"])
    assert st["hedge_matched_out"] == "above" and st["hedge_adjusts"] == 1
    # 合計は損益の行と合う（手数料は費用に入る）
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=?", (pid,)).fetchall()
    assert sum(r["net"] for r in rows) == pytest.approx(sum(st["cum"][c] for c in
                                                           ("income", "direction", "gamma", "hedge", "haircut", "other")))


def test_guard_screen_counts_lighter_and_shows_lines_and_stages(world):  # noqa: F811
    from farm_radar import guard
    path, conn = world
    _open(conn, path)
    g = guard.summary(conn, _config(path), NOW)
    up = next(v for v in g["venues"] if v["venue_id"] == "up-robinhood")
    lighter = next(v for v in g["venues"] if v["venue_id"] == "lighter")
    assert up["placed_usd"] == pytest.approx(600.0) and lighter["placed_usd"] == pytest.approx(400.0)
    assert lighter["cap_usd"] == g["limits"]["venue_cap_usd"]
    assert g["placed_usd"] == pytest.approx(1000.0)
    assert [p["period"] for p in g["loss_lines"]["periods"]] == ["day", "week", "since_start"]
    assert [s["stage"] for s in g["stages"]] == [1, 2, 3, 4]
    assert g["hedges"][0]["status"]["state"] == "ok"
    assert g["loss_line"]["pct"] == 5.0
