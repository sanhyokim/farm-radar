"""N6 の監視・評価の準備（2026-10-08 指示書）のテスト。入る・出るの判断は変えず、記録と読み方だけを足した。

試験用の値（tests/test_opportunities.py の仮の一覧）を使う。
"""

import dataclasses
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from farm_radar import api, realmoney_scan
from farm_radar.n6 import engine, report
from farm_radar.n6.market import Market, rules_config

from .test_n6 import _conn, _rows, cfg  # noqa: F401  （fixture を使う）
from .test_opportunities import NOW


def _positions(conn):
    return sorted((r["portfolio_id"], r["opp_key"], r["amount_usd"], r["hedge"], r["twin_of"] is None)
                  for r in conn.execute("SELECT * FROM n6_positions"))


def test_funnel_does_not_change_decisions(cfg, tmp_path, monkeypatch):  # noqa: F811  （fixture）
    """内訳を記録しても、記録に失敗しても、入る先・額・保険は同じ（判断は変えない）。"""
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    with_funnel = _positions(conn)
    marks = {r["portfolio_id"]: json.loads(r["detail_json"]) for r in conn.execute("SELECT * FROM n6_portfolio_marks")}
    assert with_funnel
    assert all("funnel" in md and "error" not in md["funnel"] for md in marks.values())

    other = dataclasses.replace(cfg, database_path=tmp_path / "main2.sqlite3")
    conn2 = _conn(other)

    def boom(*a, **k):
        raise RuntimeError("試験: 内訳が壊れた")
    monkeypatch.setattr(engine, "funnel", boom)
    out = engine.tick(other, NOW, conn=conn2)
    assert out["ok"]
    assert _positions(conn2) == with_funnel
    md = json.loads(conn2.execute("SELECT detail_json FROM n6_portfolio_marks LIMIT 1").fetchone()[0])
    assert "試験" in md["funnel"]["error"]


def test_funnel_counts_match_candidates(cfg):  # noqa: F811  （fixture）
    """内訳の「入れられる」の数は、アプリが使う候補の数と同じ。すべての入れる先が1回ずつ数えられる。"""
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)          # 1回入ったあと（もう入っている先は held）
    target = 30.0
    cfg_new = rules_config(cfg, cfg.n6.new)
    from farm_radar import opportunities as opps
    ops = opps.collect(conn, cfg_new, NOW + timedelta(minutes=15))
    mk = Market(None, cfg_new, NOW + timedelta(minutes=15), ops, ops)
    danger = engine.Danger(target, cfg.n6.amounts_usd[0])
    for pid in ("app_1000", "app_10000"):
        pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
        mains = engine._open_mains(conn, pid)
        f = engine.funnel(conn, mk, cfg_new, pf, mains, danger, target, NOW + timedelta(minutes=15))
        c = engine.candidates(conn, mk, cfg_new, pf, mains, danger, target, NOW + timedelta(minutes=15))
        assert f["final"] == len(c)
        assert sum(f["counts"].values()) == f["total"] == len(mk.ops)
        assert f["counts"].get("held", 0) == len(mains)
        if f["best_miss"]:
            b = f["best_miss"]
            assert b["code"] != "ok" and b["reason"] and b["gap_pt"] == round(target - b["apr_pct"], 2)


def test_first_entry_and_daily_summary(cfg):  # noqa: F811  （fixture）
    conn = _conn(cfg)
    for i in range(3):
        engine.tick(cfg, NOW + timedelta(minutes=15 * i), conn=conn)
    fe = {r["portfolio_id"]: r for r in report.first_entries(conn, cfg)}
    assert fe["app_1000"]["entered"] and fe["app_1000"]["name"] and fe["app_1000"]["pick_reason"]
    assert fe["app_1000"]["amount_usd"] > 0 and fe["app_1000"]["danger_label"] == "中くらい"
    first = conn.execute("SELECT MIN(opened_at) FROM n6_positions WHERE portfolio_id='app_1000' AND twin_of IS NULL").fetchone()[0]
    assert fe["app_1000"]["opened_at"] == first

    days = report.daily(conn, cfg, now=NOW + timedelta(minutes=40))
    assert days and days[0]["ticks"] == 3 and days[0]["ticks_failed"] == 0
    p = {x["id"]: x for x in days[0]["portfolios"]}
    n_main = conn.execute("SELECT COUNT(*) FROM n6_positions WHERE portfolio_id='app_1000' AND twin_of IS NULL").fetchone()[0]
    assert p["app_1000"]["entered"] == n_main and p["app_1000"]["max_positions"] == n_main
    assert days[0]["hedge_vs"]["pairs"] >= 1
    assert set(days[0]["new_vs_old"]) == {"rebalances_new", "rebalances_old", "loss_day_new", "loss_day_old"}


def test_daily_keeps_waiting_reason_without_positions(cfg):  # noqa: F811  （fixture）
    """建玉がない日も、待った理由を残す（内訳がない古い記録は、その回の待ちの文をそのまま使う）。"""
    conn = _conn(cfg)
    engine.ensure_portfolio(conn, "app", 1000.0, NOW)
    for i, md in enumerate([{"waiting": "狙い利回り以上で、上限に収まる入れる先がない", "status": "running"},
                            {"waiting": "x", "status": "running",
                             "funnel": {"reach": 2, "target": 30.0, "reach_dropped": {"pool_5pct": 2}, "state": "running"}}]):
        ts = engine._iso(NOW + timedelta(minutes=15 * i))
        conn.execute("INSERT INTO n6_ticks(ts, ok, detail_json) VALUES (?,1,'{}')", (ts,))
        conn.execute("INSERT INTO n6_portfolio_marks(portfolio_id, ts, equity_usd, placed_usd, cash_usd, detail_json) "
                     "VALUES ('app_1000', ?, 1000, 0, 1000, ?)", (ts, json.dumps(md, ensure_ascii=False)))
    conn.commit()
    d = report.daily(conn, cfg, now=NOW + timedelta(minutes=20))[0]
    texts = {w["text"] for w in d["portfolios"][0]["wait_reasons"]}
    assert "狙い利回り以上で、上限に収まる入れる先がない" in texts
    assert "年30%以上はあるが「プールの5%上限」で入れない" in texts
    assert d["portfolios"][0]["waiting_ticks"] == 2


def test_cap_diff_between_1000_and_10000(cfg):  # noqa: F811  （fixture）
    conn = _conn(cfg)
    engine.ensure_portfolio(conn, "app", 1000.0, NOW)
    engine.ensure_portfolio(conn, "app", 10000.0, NOW)
    ts = engine._iso(NOW)
    rows = {"app_1000": [{"key": "a", "name": "A", "code": "ok", "apr_pct": 40}],
            "app_10000": [{"key": "a", "name": "A", "code": "danger_cap", "apr_pct": 40}]}
    for pid, rr in rows.items():
        conn.execute("INSERT INTO n6_portfolio_marks(portfolio_id, ts, equity_usd, placed_usd, cash_usd, detail_json) "
                     "VALUES (?, ?, 0, 0, 0, ?)", (pid, ts, json.dumps({"funnel": {"reach_rows": rr}})))
    conn.commit()
    d = report.cap_diff(conn)
    assert d["ticks"] == 1 and d["latest"][0]["small"] == "ok" and d["latest"][0]["big"] == "danger_cap"
    assert d["tally"][0]["big_ja"] == "危なさごとの1か所の上限"


def test_health_finds_problems(cfg):  # noqa: F811  （fixture）
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    h = report.health(conn, cfg, NOW + timedelta(minutes=5))
    assert h["ok"], h["problems"]
    assert h["serious"] == [] and h["realmoney"] == []
    # 30分より長く回がない（見回りの回の問題。更新の1行は止めない = serious に入れない）
    late = report.health(conn, cfg, NOW + timedelta(minutes=45))
    assert not late["ok"] and late["serious"] == []
    # 失敗した回
    conn.execute("INSERT INTO n6_ticks(ts, ok, detail_json) VALUES (?, 0, ?)",
                 (engine._iso(NOW + timedelta(minutes=15)), json.dumps({"error": "試験"})))
    m = conn.execute("SELECT * FROM n6_positions WHERE twin_of IS NULL AND portfolio_id='app_1000' LIMIT 1").fetchone()
    # 対が欠けた・本体が閉じたのに対が開いたまま・同じ建玉の重複・額が総額をこえる
    other = conn.execute("SELECT * FROM n6_positions WHERE twin_of IS NULL AND id != ? LIMIT 1", (m["id"],)).fetchone()
    conn.execute("UPDATE n6_positions SET status='closed', closed_at=? WHERE id=?", (engine._iso(NOW), other["id"]))
    conn.execute("UPDATE n6_positions SET twin_of=NULL WHERE twin_of=?", (m["id"],))
    conn.execute("UPDATE n6_positions SET amount_usd=5000 WHERE id=?", (m["id"],))
    conn.commit()
    probs = " / ".join(report.health(conn, cfg, NOW + timedelta(minutes=16))["problems"])
    assert "失敗" in probs and "対が欠けている" in probs and "対が開いたまま" in probs
    assert "総額" in probs and "同じ入れる先" in probs
    h2 = report.health(conn, cfg, NOW + timedelta(minutes=16))
    ser = " / ".join(h2["serious"])                          # 記録の食い違いは重大（更新の1行が止める）
    assert "失敗" not in ser and "対が欠けている" in ser and "総額" in ser and "同じ入れる先" in ser


def test_health_real_money_hit_is_serious(cfg, monkeypatch):  # noqa: F811  （fixture）
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    monkeypatch.setattr(realmoney_scan, "scan", lambda *a, **k: ["engine.py: private_key"])
    h = report.health(conn, cfg, NOW + timedelta(minutes=5))
    assert not h["ok"] and h["realmoney"] == ["engine.py: private_key"] and len(h["serious"]) == 1


def test_no_real_money_code_scan():
    assert realmoney_scan.scan() == []


def test_api_daily_and_health(cfg, monkeypatch):  # noqa: F811  （fixture）
    conn = _conn(cfg)
    engine.tick(cfg, NOW, conn=conn)
    conn.close()
    monkeypatch.setattr(api, "load_config", lambda *a, **k: cfg)
    c = TestClient(api.app)
    h = c.get("/api/n6/health").json()
    assert "problems" in h and h["ticks"] == 1
    d = c.get("/api/n6/daily").json()
    assert "days" in d
    o = c.get("/api/n6").json()
    assert o["first_entries"] and o["health"] and o["portfolios"][0]["funnel"] is not None
