"""N6「仮想のお金で渡る」（2026-10-07 指示書）のテスト。100% 仮想。外のサイトには行かない。

試験用の値（tests/test_opportunities.py の仮の一覧）で、練習のまとまり・上限・対・置き直し（新旧）・保険・
4つの段階・損の線（新旧）・ボーナスの売却・手動の「出る」・自分で選ぶ申し込みを確かめる。
"""

import dataclasses
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, opportunities as opps
from farm_radar.config import N6_NEW, N6_OLD, load_config
from farm_radar.db import database as db
from farm_radar.n6 import engine, sim
from farm_radar.n6.market import Market, Price, rules_config
from farm_radar.standard import _feeds_conn

from .test_opportunities import CFG, NOW, _feeds_db

PRICE = {"WETH": 2700.0}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "feeds.sqlite3"
    _feeds_db(path)
    c = dataclasses.replace(CFG, database_path=tmp_path / "main.sqlite3",
                            feeds=dataclasses.replace(CFG.feeds, database_path=path))
    # 試験用の一覧の会場は登録していない（見分けが不確か・危なさ「とても高い」）。練習の仕組みを確かめるため「中くらい」として扱う
    monkeypatch.setattr(opps.Opportunity, "uncertain_venue", property(lambda self: False))
    monkeypatch.setattr(engine.Danger, "of", lambda self, o: {"level": "mid", "label": "中くらい"})
    PRICE["WETH"] = 2700.0
    orig = Market.price

    def price(self, chain_id, address, symbol):
        if symbol in PRICE:
            return Price(PRICE[symbol], "test", self.now)
        return orig(self, chain_id, address, symbol)
    monkeypatch.setattr(Market, "price", price)
    return c


def _conn(cfg):
    return db.connect(cfg.database_path)


def _rows(conn, q, *a):
    return [dict(r) for r in conn.execute(q, a)]


# --- 設定（N5 の仮決定）-----------------------------------------------------------------------------

def test_n6_settings_and_explore_defaults_unchanged():
    c = load_config()
    assert c.n6.new == N6_NEW and c.n6.old == N6_OLD
    assert c.n6.new.rebalance_factor == 0.25 and c.n6.new.edge_buffer_frac == 0.15 and c.n6.new.edge_wait_minutes == 30
    assert c.n6.new.loss_day == {"caution": -3.0, "no_new": -8.5, "stop": -18.0}
    assert c.n6.old.loss_day == {"caution": -3.0, "no_new": -4.0, "stop": -5.0}
    # 探すは前のまま（N6 の練習だけ新しい決まり）
    assert c.opportunities.rebalance_factor == 1.0 and c.opportunities.merkl_denominator == "A"
    # 本番の上限（limits）は変えていない
    assert c.limits.get("per_venue_share") is not None


def test_rebalance_factor_scales_estimate(cfg):
    conn = _conn(cfg)
    new = {o.base.key: o for o in opps.collect(conn, rules_config(cfg, cfg.n6.new), NOW)}
    old = {o.base.key: o for o in opps.collect(conn, rules_config(cfg, cfg.n6.old), NOW)}
    vn = engine.variant(new["o-eth"], 1000.0, False, "normal")
    vo = engine.variant(old["o-eth"], 1000.0, False, "normal")
    if vn.range_pct == vo.range_pct:
        assert vn.rebalances_per_day == pytest.approx(vo.rebalances_per_day * 0.25)
    assert vn.rebalance <= vo.rebalance + 1e-12


# --- 練習のまとまり・上限・対 ---------------------------------------------------------------------

def test_app_portfolios_start_and_pick_within_caps(cfg):
    conn = _conn(cfg)
    out = engine.tick(cfg, NOW, conn=conn)
    assert set(out["portfolios"]) == {"app_1000", "app_10000"}
    pf = {r["id"]: r for r in _rows(conn, "SELECT * FROM n6_portfolios")}
    mains = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of IS NULL")
    twins = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of IS NOT NULL")
    assert mains and len(twins) == len(mains)
    for m in mains:
        total = pf[m["portfolio_id"]]["total_usd"]
        assert m["pick_reason"] and "控えめの見込みの年利" in m["pick_reason"]
        if total >= 2000:
            assert m["amount_usd"] <= total * 0.15 + 1e-9          # 危なさ「中くらい」は資金の 15% まで
        entry = json.loads(m["entry_json"])
        o_cap = {"o-eth": 50_000.0, "o-new": 250.0}.get(m["opp_key"])
        if o_cap is not None:
            assert entry["split"]["pool"] <= o_cap + 1e-9            # プールの 5% まで
        tw = next(t for t in twins if t["twin_of"] == m["id"])
        assert tw["hedge"] != m["hedge"] and tw["amount_usd"] == m["amount_usd"] and tw["opened_at"] == m["opened_at"]
        st_m, st_t = json.loads(m["state_json"]), json.loads(tw["state_json"])
        if st_m.get("r"):
            assert st_t["r"] == st_m["r"]                           # 対は本体と同じ幅
    for pid, p in pf.items():
        placed = sum(m["amount_usd"] for m in mains if m["portfolio_id"] == pid)
        assert p["cash_usd"] == pytest.approx(p["total_usd"] - placed)   # 対はお金に数えない
        venue = {}
        for m in mains:
            if m["portfolio_id"] == pid:
                venue[m["venue"]] = venue.get(m["venue"], 0) + m["amount_usd"]
        if p["total_usd"] >= 2000:
            assert all(v <= p["total_usd"] * 0.5 + 1e-9 for v in venue.values())   # 1つの会場は 50% まで
    # 本物のお金は動かしていない: 送金・署名の記録の表は無い
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%tx%' AND name LIKE 'n6%'").fetchall()


def test_value_identity_and_bonus_sale(cfg):
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    for i in range(1, 100):                                       # 約1日（9時の売却をまたぐ）
        engine.tick(cfg, NOW + timedelta(minutes=15 * i), conn=conn)
    pos = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of IS NULL")[0]
    st = json.loads(pos["state_json"])
    v = sim.value(st)
    parts = (v["price_move"] + v["hedge_pnl"] - v["funding"] - v["hedge_cost"] - v["rebalance_cost"] - v["entry_cost"]
             + v["bonus"] + v["fees"])
    assert v["value"] - pos["amount_usd"] == pytest.approx(parts, abs=1e-6)
    sales = _rows(conn, "SELECT * FROM n6_bonus_sales WHERE position_id=?", pos["id"])
    assert len(sales) == 1 and sales[0]["usd"] > 0 and sales[0]["cost_usd"] > 0
    assert st["bonus"]["hold_units"]
    assert sim.hold_difference(st)["held_usd"] > 0


# --- 置き直し（新: 15%・30分 / 影の旧: 0%・15分）・保険 ---------------------------------------------

def _market(cfg, now):
    conn = _conn(cfg)
    c = rules_config(cfg, cfg.n6.new)
    ops = opps.collect(conn, c, now)
    return Market(_feeds_conn(cfg.feeds.database_path), c, now, ops, ops), {o.base.key: o for o in ops}


def test_rebalance_new_vs_old_and_hedge_above(cfg):
    mk, ops = _market(cfg, NOW)
    o = ops["o-eth"]
    v = engine.variant(o, 1000.0, True)
    st = sim.open_state(mk=mk, op=o, variant=v, r=0.15, hedge_markets=engine._hedge_markets(mk, o), now=NOW, est={})
    assert st["geo"] == "synthetic" and st["hedge"]["legs"]
    p0 = st["P"]
    # 幅の上（+18%）: 旧は境目を出て15分で置き直す。新は境目（+15%）から幅の 15%（+4.5%）外に出ていないので置き直さない
    PRICE["WETH"] = p0 * 1.18
    t = NOW
    for _ in range(3):
        t += timedelta(minutes=15)
        mk.now = t
        sim.step(st, mk, "o-eth", t, cfg.n6.new, cfg.n6.old, cfg.guard)
    assert st["old"]["count"] == 1 and st["reb"]["count"] == 0
    # 保険: 幅の上に 10% の余裕をこえて 30 分 → 売りを閉じる（中身はステーブルだけ）
    legs = st["hedge"]["legs"]
    assert legs[0]["size"] == pytest.approx(0.0, abs=1e-12)
    # さらに上（+25%）に 30 分 → 新も置き直す
    PRICE["WETH"] = p0 * 1.25
    for _ in range(3):
        t += timedelta(minutes=15)
        sim.step(st, mk, "o-eth", t, cfg.n6.new, cfg.n6.old, cfg.guard)
    assert st["reb"]["count"] == 1
    assert st["lower"] < PRICE["WETH"] < st["upper"]
    assert st["hedge"]["legs"][0]["size"] > 0                     # 置き直したら保険も中身に合わせる
    assert st["costs"]["reb_swap"] > 0 and st["costs"]["reb_gas"] > 0


def test_liquidation_closes_position_stage1(cfg, monkeypatch):
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    m = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of IS NULL AND hedge=1 AND portfolio_id='app_1000'")[0]
    PRICE["WETH"] = 2700.0 * 3.0                                  # 保険の売りが耐えられない上げ
    engine.tick(cfg, NOW + timedelta(minutes=15), conn=conn)
    r = _rows(conn, "SELECT * FROM n6_positions WHERE id=?", m["id"])[0]
    assert r["status"] == "closed" and r["exit_rule"] == "liquidation" and r["exit_stage"] == 1
    tw = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of=?", m["id"])[0]
    assert tw["status"] == "closed" and tw["exit_rule"] == "main_closed"
    assert _rows(conn, "SELECT * FROM n6_events WHERE position_id=? AND action='liquidation'", m["id"])


def test_dump_rule_exits_stage2(cfg):
    conn = _conn(cfg)
    for i in range(4):
        engine.tick(cfg, NOW + timedelta(minutes=15 * i), conn=conn)
    PRICE["WETH"] = 2700.0 * 0.82                                 # 1時間で −18%（投げ売りの線 −15%）
    engine.tick(cfg, NOW + timedelta(minutes=60), conn=conn)
    rules = {r["exit_rule"] for r in _rows(conn, "SELECT exit_rule FROM n6_positions WHERE twin_of IS NULL "
                                                 "AND opp_key='o-eth' AND status='closed'")}
    assert rules and rules <= {"dump", "own_value_drop"}
    assert "dump" in rules
    # 出たところには 24 時間入り直さない
    open_eth = _rows(conn, "SELECT * FROM n6_positions WHERE twin_of IS NULL AND opp_key='o-eth' AND status='open'")
    assert not open_eth


# --- 損の線（1日は新しい線。旧の線は影で記録）------------------------------------------------------

def test_loss_lines_new_and_shadow_old(cfg):
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id='app_1000'").fetchone()
    # 今日の始まりの値打ちを $1,000 にして、置いている建玉の値打ちを −6% にする
    conn.execute("UPDATE n6_positions SET value_usd = amount_usd * 0.94 WHERE portfolio_id='app_1000' AND twin_of IS NULL")
    ls = engine.apply_loss_lines(conn, cfg, pf, NOW + timedelta(minutes=1))
    assert ls["periods"]["day"]["level"] == "caution"             # 新: −3 / −8.5 / −18
    assert ls["old_day_level"] == "stop"                          # 旧: −3 / −4 / −5 なら止まっていた
    ev = _rows(conn, "SELECT * FROM n6_events WHERE portfolio_id='app_1000' AND rule LIKE 'loss_day%'")
    assert {(e["action"], e["shadow"]) for e in ev} >= {("caution", 0), ("stop", 1)}
    assert conn.execute("SELECT status FROM n6_portfolios WHERE id='app_1000'").fetchone()[0] == "running"


# --- 手動の「出る」・自分で選ぶ・画面 ----------------------------------------------------------------

def test_manual_exit_owner_request_and_views(cfg, monkeypatch):
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    conn.close()
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    client = TestClient(api.app)
    ov = client.get("/api/n6").json()
    assert ov["virtual"] and {p["id"] for p in ov["portfolios"]} == {"app_1000", "app_10000"}
    m = next(p for p in ov["portfolios"] if p["id"] == "app_1000")["open"][0]
    cash0 = next(p for p in ov["portfolios"] if p["id"] == "app_1000")["cash_usd"]
    assert client.post(f"/api/n6/positions/{m['id']}/exit", json={"reason": "bad"}).status_code == 400
    r = client.post(f"/api/n6/positions/{m['id']}/exit", json={"reason": "test", "note": "ためし"})
    assert r.status_code == 200 and r.json()["reason_ja"] == "テスト"
    d = client.get(f"/api/n6/positions/{m['id']}").json()
    assert d["status"] == "closed" and d["exit_rule"] == "manual" and d["manual_exit"]["note"] == "ためし"
    assert d["manual_exit"]["estimate"]["start"] is not None
    conn = _conn(cfg)
    cash1 = conn.execute("SELECT cash_usd FROM n6_portfolios WHERE id='app_1000'").fetchone()[0]
    assert cash1 == pytest.approx(cash0 + d["value_usd"])
    assert conn.execute("SELECT status FROM n6_positions WHERE twin_of=?", (m["id"],)).fetchone()[0] == "closed"
    assert client.post(f"/api/n6/positions/{m['id']}/exit", json={"reason": "test"}).status_code == 400
    # 自分で選ぶ: 申し込み → 次の回で入る
    r = client.post("/api/n6/owner", json={"opp_key": "o-eth", "size": 1000, "hedge": "no"})
    assert r.status_code == 200 and r.json()["status"] == "waiting"
    assert client.post("/api/n6/owner", json={"opp_key": "o-eth", "size": 1000}).status_code == 400   # 同じ申し込み
    assert client.post("/api/n6/owner", json={"opp_key": "o-eth", "size": 5000}).status_code == 400
    engine.tick(cfg, NOW + timedelta(minutes=15), conn=conn)
    own = _rows(conn, "SELECT * FROM n6_positions WHERE portfolio_id='own_1000' AND twin_of IS NULL")
    assert len(own) == 1 and own[0]["hedge"] == 0 and "自分で選んだ" in own[0]["pick_reason"]
    rq = _rows(conn, "SELECT * FROM n6_requests")[0]
    assert rq["status"] == "done" and rq["position_id"] == own[0]["id"]
    ov = client.get("/api/n6").json()
    assert any(c["size"] == 1000 for c in ov["app_vs_own"])
    assert ov["shadow"]["positions"] >= 1
    assert client.get("/api/n6/positions/999999").status_code == 404


def test_feed_source_runs_n6(cfg, tmp_path):
    from farm_radar.feeds import sources, store, trial
    from farm_radar.feeds.run import Fetcher, run_source

    fconn = store.connect(cfg.feeds.database_path)
    ctx = trial.TrialContext(n6=engine.make_runner(cfg))
    res = run_source(fconn, sources.BY_ID["n6_practice"], Fetcher(cfg.feeds), cfg.feeds, NOW, trial_ctx=ctx)
    assert res["status"] == "ok" and res["items"] == 2
    assert sources.BY_ID["n6_practice"].cadence == "15min" and sources.SOURCES[-1].id == "n6_practice"


def test_no_real_money_code():
    """N6 のコードに、送金・署名・秘密鍵・ウォレット接続を扱うものがない（指示書 27）。"""
    root = Path(engine.__file__).parent
    bad = re.compile(r"private_?key|mnemonic|seed phrase|sign_transaction|send_raw|sendRawTransaction|eth_sendTransaction|"
                     r"web3\.eth\.account|Account\.from_key|LiveExecutor", re.I)
    for p in root.glob("*.py"):
        assert not bad.search(p.read_text(encoding="utf-8")), p.name
