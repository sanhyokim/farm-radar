"""練習（ペーパートレード。M5a）のテスト。M2 のテストと同じ偽の記録を使い、建玉を作って記録を足していく。"""

import dataclasses
import math
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, views
from farm_radar.config import load_config
from farm_radar.db import database as db
from farm_radar.execution import views as pviews
from farm_radar.execution.base import PositionRef
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import CATS, PaperError, PaperExecutor, lp_amounts
from farm_radar.fx import FxRate, fill_ledger_jpy, rate_for
from farm_radar.scoring.prices import Q96
from farm_radar.scoring.run import score_venue

from .test_scoring_run import NOW, POOLS, TOKENS, UP, FakeGT, FakeLighter, _ctx, _fill

WETH_POOL = "up-robinhood:p-weth"


class FakeFx:
    """土日を聞かれたら金曜のレートを返す（frankfurter と同じ動き）。"""
    def __init__(self):
        self.calls = 0

    def usd_jpy(self, day):
        self.calls += 1
        from datetime import date
        d = date.fromisoformat(day)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return FxRate(150.0, d.isoformat())


def _config(path, mode="paper"):
    return dataclasses.replace(load_config(), database_path=path, mode=mode, snapshot_minutes=60)


def _extend(conn, hours, *, price_mult=None, start=None, step_minutes=60):
    """記録を足す（最後の記録の続きから）。price_mult があれば、その倍率の価格にする。"""
    last = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    from datetime import datetime
    t = start or datetime.fromisoformat(last)
    for h in range(1, hours + 1):
        ts = t + timedelta(minutes=step_minutes * h)
        run_id = db.start_run(conn, "up-robinhood", ts, ts)
        for pid, t0, t1, d0, d1, p, wig in POOLS:
            price = p * (price_mult if price_mult else math.exp(wig * (-1) ** h))
            sp = math.sqrt(price * 10 ** (d1 - d0))
            db.insert_snapshot(conn, {
                "pool_id": f"up-robinhood:{pid}", "ts": ts.isoformat(timespec="seconds"), "block_number": 5000 + h,
                "run_id": run_id, "price": price, "tick": 0, "sqrt_price_x96": str(int(sp * Q96)), "fee": 3000,
                "liquidity_total": str(10 ** 18), "liquidity_staked_inrange": str(5 * 10 ** 17),
                "reward_rate_raw": str(10 ** 18), "reward_rate_effective_raw": str(10 ** 18),
                "reward_token": UP, "gauge_alive": 1, "unstaked_fee": 100000, "epoch_just_flipped": 0,
                "block_time": ts.isoformat(timespec="seconds"), "source": "test",
            })
        db.finish_run(conn, run_id, now=ts, status="ok", block_number=5000 + h, pools_ok=3, pools_failed=0)
    conn.commit()
    return t + timedelta(minutes=step_minutes * hours)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    _fill(conn, 24 * 8)
    score_venue(conn, _ctx(FakeGT()), now=NOW)
    yield path, conn
    conn.close()


def _open(conn, path, pool=WETH_POOL, now=NOW):
    ex = PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=now)
    return ex, ex.open_position(pool, 1000.0)


# --- LP の計算 ------------------------------------------------------------------------------

def test_lp_amounts_edges():
    # レンジより下では token0 だけ、上では token1 だけになる
    x, y = lp_amounts(10 ** 18, 0.5, 0.9, 1.1, 18, 18)
    assert x > 0 and y == 0
    x, y = lp_amounts(10 ** 18, 2.0, 0.9, 1.1, 18, 18)
    assert x == 0 and y > 0


# --- 建玉を作る -----------------------------------------------------------------------------

def test_open_needs_paper_mode(world):
    path, conn = world
    ex = PaperExecutor(conn, _config(path, mode="observe"), TOKENS, fx=FakeFx(), now=NOW)
    with pytest.raises(PaperError, match="mode を paper"):
        ex.open_position(WETH_POOL, 1000.0)


def test_observe_only_venue_refuses_practice(world, tmp_path):
    # 会場ファイルに practice: false と書いた会場（M6 の Alandale）では、練習を始めない
    path, conn = world
    import shutil
    root = tmp_path / "root"
    (root / "venues").mkdir(parents=True)
    src = load_config().root / "venues" / "up-robinhood.yaml"
    text = src.read_text(encoding="utf-8") + "\npractice: false\n"
    (root / "venues" / "up-robinhood.yaml").write_text(text, encoding="utf-8")
    shutil.copy(load_config().root / "venues" / "tokens-robinhood.yaml", root / "venues")
    shutil.copytree(load_config().root / "chains", root / "chains")   # N1: 会場はチェーンの登録を指す
    ex = PaperExecutor(conn, dataclasses.replace(_config(path), root=root), TOKENS, fx=FakeFx(), now=NOW)
    with pytest.raises(PaperError, match="観察だけ"):
        ex.open_position(WETH_POOL, 1000.0)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_same_pool_cannot_be_opened_twice(world):
    # 2026-09-30 オーナー指示: 上限に余裕があっても、練習中のプールで2つ目の練習は開けない（サーバー側で止める）
    path, conn = world
    big = dataclasses.replace(_config(path), limits={"position_usd": 1000, "total_usd": 10000,
                                                      "per_venue_share": 1.0, "trades_per_day": 20})
    ex = PaperExecutor(conn, big, TOKENS, fx=FakeFx(), now=NOW)
    first = ex.open_position(WETH_POOL, 1000.0)
    with pytest.raises(PaperError, match="すでに練習中"):
        ex.open_position(WETH_POOL, 1000.0)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE pool_id=?", (WETH_POOL,)).fetchone()[0] == 1
    # 別のプールは開ける。閉じたあとなら、同じプールでまた始められる
    ex.open_position("up-robinhood:p-nvda", 1000.0)
    ex.close_position(first)
    ex.open_position(WETH_POOL, 1000.0)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE pool_id=? AND status='open'",
                        (WETH_POOL,)).fetchone()[0] == 1


def test_open_records_position_ledger_and_red_start(world):
    path, conn = world
    ex, ref = _open(conn, path)
    p = conn.execute("SELECT * FROM positions WHERE id=?", (ref.position_id,)).fetchone()
    score = conn.execute("SELECT * FROM scores WHERE pool_id=? ORDER BY ts DESC LIMIT 1", (WETH_POOL,)).fetchone()
    assert p["status"] == "open" and p["is_paper"] == 1
    assert p["r"] * 100 == pytest.approx(score["best_r"])
    assert p["c_lp"] == pytest.approx(1000 * 0.55, rel=1e-9)           # LP に置くのは総資産の55%
    assert p["started_red"] == (1 if score["signal"] == "red" else 0)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM ledger WHERE position_id=?", (ref.position_id,))]
    assert kinds.count("deposit") == 2 and "hedge_open" in kinds and "cost" in kinds   # WETH は perp でヘッジ
    row = conn.execute("SELECT * FROM ledger WHERE kind='deposit' LIMIT 1").fetchone()
    assert row["fx_rate"] == 150.0 and row["price_jpy"] == pytest.approx(row["price_usd"] * 150.0)
    assert row["fx_date"]                                                  # レートの日付も記録する
    # 開いた時の行: 「その他」= 開く時の費用
    first = conn.execute("SELECT * FROM position_pnl WHERE position_id=?", (ref.position_id,)).fetchone()
    assert first["other"] < 0 and first["net"] == pytest.approx(first["other"])


def test_red_start_is_labeled(world):
    path, conn = world
    conn.execute("UPDATE scores SET signal='red'")
    conn.commit()
    ex, ref = _open(conn, path)
    p = conn.execute("SELECT * FROM positions WHERE id=?", (ref.position_id,)).fetchone()
    c = pviews.card(conn, p, NOW)
    assert c["started_red"] and c["red_label"] == "🔴で開始した練習"
    assert "当てはめません" in pviews.detail(conn, p, NOW)["red_note"]


def test_limits_come_from_config_and_are_enforced(world):
    path, conn = world
    _open(conn, path)
    # 1会場あたり合計の50%（$1,500）までなので、2つ目の $1,000 は入れられない
    ex = PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW)
    with pytest.raises(PaperError, match="1つの会場に置ける上限"):
        ex.open_position("up-robinhood:p-nvda", 1000.0)
    with pytest.raises(PaperError, match="1つの建玉の上限"):
        ex.open_position("up-robinhood:p-nvda", 5000.0)


# --- 毎回の計算 -----------------------------------------------------------------------------

def test_hourly_update_parts_add_up_and_match_value(world):
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 6)
    n = run_paper(conn, _config(path), TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=7))
    assert n == 6
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts", (ref.position_id,)).fetchall()
    for r in rows:
        assert sum(r[c] or 0 for c in CATS) == pytest.approx(r["net"], abs=1e-9)
    total = sum(r["net"] for r in rows)
    assert rows[-1]["value_usd"] == pytest.approx(1000.0 + total)
    # 報酬は受け取りの記録（台帳）と一致する
    claims = conn.execute("SELECT SUM(value_usd) FROM ledger WHERE position_id=? AND kind='claim'",
                          (ref.position_id,)).fetchone()[0] or 0.0
    assert sum(r["income"] for r in rows) == pytest.approx(claims)
    # ガンマ（LP の中身の入れかわり）は損にしかならない
    assert sum(r["gamma"] for r in rows) <= 1e-9
    # 資金調達の支払い（FakeLighter は1時間 0.001%）がヘッジに入る
    assert conn.execute("SELECT COUNT(*) FROM hedge_funding").fetchone()[0] > 0
    st = __import__("json").loads(conn.execute("SELECT state_json FROM positions").fetchone()[0])
    assert st["funding_paid"] > 0


def test_hedge_cancels_direction(world):
    # WETH/USDG は WETH を開いた時の量だけ売っている → 値動き + ヘッジ = −資金調達の支払い（ヘッジのずれ）
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 2, price_mult=1.003)
    run_paper(conn, _config(path), TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=3))
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=?", (ref.position_id,)).fetchall()
    b = views.signed_breakdown({c: sum(r[c] or 0 for r in rows) for c in CATS})
    st = __import__("json").loads(conn.execute("SELECT state_json FROM positions").fetchone()[0])
    assert b["direction"] != 0
    assert b["hedge_gap"] == pytest.approx(-st["funding_paid"], abs=1e-9)


def test_no_reward_while_out_of_range(world):
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 3, price_mult=3.0)          # 価格が3倍 = レンジの外
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=4))
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts", (ref.position_id,)).fetchall()
    # 記録ごとの行（置き直し・閉じるの行は除く。M5b ではレンジの外に出ると見張りが動くため）
    rows = [r for r in rows if "dt_s" in __import__("json").loads(r["detail_json"])]
    assert rows[-1]["in_range"] == 0 and rows[-1]["reward_amount"] == 0
    assert rows[-1]["income"] == 0


def test_gap_rows_are_marked_estimated(world):
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 1, step_minutes=180)        # 3時間あいた（収集が止まっていた）
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=4))
    last = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts DESC LIMIT 1",
                        (ref.position_id,)).fetchone()
    assert last["is_estimated"] == 1


def test_sell_now_variant_is_recorded(world):
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 6)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=7))
    p = conn.execute("SELECT * FROM positions WHERE id=?", (ref.position_id,)).fetchone()
    d = pviews.detail(conn, p, NOW + timedelta(hours=7))
    sn = d["sell_now"]
    assert sn["sell_net"] == pytest.approx(sn["hold_net"] - sn["hold_haircut"] + sn["sell_haircut"])


def test_close_realizes_everything(world):
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 3)
    later = PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=4))
    res = later.close_position(ref)
    p = conn.execute("SELECT * FROM positions WHERE id=?", (ref.position_id,)).fetchone()
    assert p["status"] == "closed" and p["close_reason"] == "manual"
    d = pviews.detail(conn, p, NOW + timedelta(hours=4))
    assert d["unrealized"] == 0 and d["realized"] == pytest.approx(res.net_usd)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM ledger WHERE position_id=?", (ref.position_id,))}
    assert {"withdraw", "hedge_close"} <= kinds
    with pytest.raises(PaperError, match="もう閉じています"):
        later.close_position(PositionRef(ref.position_id))


# --- 円のレート -----------------------------------------------------------------------------

def test_fx_uses_previous_business_day_and_caches(world):
    path, conn = world
    fx = FakeFx()
    from datetime import datetime, UTC
    sunday = datetime(2026, 9, 27, 12, tzinfo=UTC)
    got = rate_for(conn, sunday, fx, now=sunday + timedelta(days=2))
    assert got.rate_date == "2026-09-25" and got.jpy_per_usd == 150.0
    rate_for(conn, sunday, fx, now=sunday + timedelta(days=2))
    assert fx.calls == 1                                   # 1日1回だけ聞く


def test_fx_backfills_ledger_when_rate_was_missing(world):
    path, conn = world

    class Down:
        def usd_jpy(self, day):
            raise RuntimeError("offline")
    ex = PaperExecutor(conn, _config(path), TOKENS, fx=Down(), now=NOW)
    ex.open_position(WETH_POOL, 1000.0)
    assert conn.execute("SELECT COUNT(*) FROM ledger WHERE fx_rate IS NULL").fetchone()[0] > 0
    fill_ledger_jpy(conn, FakeFx())
    assert conn.execute("SELECT COUNT(*) FROM ledger WHERE fx_rate IS NULL").fetchone()[0] == 0


# --- 画面用API -----------------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    path, conn = world
    cfg = _config(path)
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(minutes=5))
    monkeypatch.setattr(api, "_paper_tokens", lambda config, venue: TOKENS)
    import farm_radar.execution.paper as paper_mod
    monkeypatch.setattr(paper_mod, "rate_for", lambda conn, when, fx=None, now=None: FxRate(150.0, "2026-09-25"))
    return TestClient(api.app), conn, path


def test_api_open_detail_close(client):
    c, conn, path = client
    d = c.get("/api/paper").json()
    assert d["enabled"] and d["open"] == [] and d["venue_cap_usd"] == 1500
    r = c.post("/api/paper/positions", json={"pool_id": WETH_POOL})
    assert r.status_code == 200
    pid = r.json()["position_id"]
    # 2つ目は上限で断られ、理由が日本語で返る
    r2 = c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-nvda"})
    assert r2.status_code == 400 and "上限" in r2.json()["detail"]
    # 同じプールは「すでに練習中」で断られる（上限より先に調べる）
    r3 = c.post("/api/paper/positions", json={"pool_id": WETH_POOL})
    assert r3.status_code == 400 and "すでに練習中" in r3.json()["detail"]
    _extend(conn, 4)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=5))
    det = c.get(f"/api/paper/positions/{pid}").json()
    assert set(views.CATEGORIES) <= set(det["total"])
    assert det["compare"] and len(det["compare"]["rows"]) == 6
    assert det["sell_now"]["hours"] == 1
    assert det["hourly"]["bars"] and det["ledger"]
    assert det["apy_note"] == views.APY_NOTE
    lst = c.get("/api/paper").json()
    assert lst["open"][0]["id"] == pid and "to_lower_pct" in lst["open"][0]
    assert c.post(f"/api/paper/positions/{pid}/close").status_code == 200
    assert c.get("/api/paper").json()["closed"][0]["status"] == "closed"


def test_api_refuses_in_observe_mode(client, monkeypatch):
    c, conn, path = client
    cfg = _config(path, mode="observe")
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    r = c.post("/api/paper/positions", json={"pool_id": WETH_POOL})
    assert r.status_code == 400 and "mode を paper" in r.json()["detail"]
    assert c.get("/api/paper").json()["enabled"] is False


def test_rewards_count_only_from_opening_time(world):
    # 最新の記録が50分前のものでも、開く前の50分の報酬は数えない
    path, conn = world
    ex, ref = _open(conn, path, now=NOW + timedelta(minutes=40))
    _extend(conn, 1)                          # 最後の記録の1時間後 = 開いてから10分後
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    last = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts DESC LIMIT 1",
                        (ref.position_id,)).fetchone()
    assert __import__("json").loads(last["detail_json"])["dt_s"] == pytest.approx(600)
    p = conn.execute("SELECT * FROM positions WHERE id=?", (ref.position_id,)).fetchone()
    c = pviews.card(conn, p, NOW + timedelta(hours=2))
    # 実績の日利 = (純損益 + 開く時の費用) ÷ 日数 ÷ 入れた額
    run = c["change_usd"] + c["open_cost_usd"]
    assert c["actual_daily_pct"] == pytest.approx(run / c["days"] / 1000 * 100)


def test_reward_price_uses_the_positions_own_venue(world):
    """2026-10-01 の早めの手じまいで見つけた問題（N1 で直した）: 同じ回に別の会場（Alandale）を後から読むと、
    その会場の報酬トークンを UP とまちがえ、UP の値段が古いまま使われていた。"""
    path, conn = world
    ex, ref = _open(conn, path)
    t = _extend(conn, 2, price_mult=1.2)
    # 同じ時刻のすぐあとに、別の会場の記録（報酬トークンがちがう）を足す
    other = "0x" + "55" * 20
    conn.execute("INSERT INTO venues(id, name, chain) VALUES ('alandale-robinhood', 'Alandale', 'robinhood')")
    conn.execute("""INSERT INTO pools(id, venue_id, address, token0, token1, discovered_at, token0_symbol, token1_symbol,
                     token0_decimals, token1_decimals) VALUES ('alandale-robinhood:a1','alandale-robinhood','a1',?,?,?,
                     'WETH','USDG',18,6)""", (POOLS[0][1], POOLS[0][2], t.isoformat()))
    ts = (t + timedelta(minutes=1)).isoformat(timespec="seconds")
    run_id = db.start_run(conn, "alandale-robinhood", t, t)
    db.insert_snapshot(conn, {"pool_id": "alandale-robinhood:a1", "ts": ts, "block_number": 9000, "run_id": run_id,
                              "price": 2500.0, "reward_token": other, "source": "test"})
    conn.commit()
    assert ex._reward_token("alandale-robinhood") == other
    assert ex._reward_token("up-robinhood") == UP.lower()
    pos = ex._open_pos(ref.position_id)
    ex.update(pos)
    last = conn.execute("SELECT detail_json FROM position_pnl WHERE position_id=? ORDER BY ts DESC LIMIT 1",
                        (ref.position_id,)).fetchone()
    snap = ex.market.latest(WETH_POOL)
    fresh = ex.market.prices(snap["run_id"])[UP.lower()]
    import json
    assert json.loads(last[0])["reward_usd"] == pytest.approx(fresh)
