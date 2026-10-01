"""2週間の評価（M5d）のテスト。"""

from datetime import timedelta

import pytest

from farm_radar.execution import evaluation
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import CATS

from .test_paper import FakeFx, _config, _extend, _open, client, world  # noqa: F401
from .test_scoring_run import NOW, TOKENS, FakeLighter


def test_not_started_and_needs_paper_mode(world):  # noqa: F811
    path, conn = world
    assert evaluation.summary(conn, _config(path), NOW)["state"] == "not_started"
    with pytest.raises(ValueError, match="練習モード"):
        evaluation.start(conn, _config(path, mode="observe"), NOW)


def test_running_summary_compares_predicted_and_actual(world):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    _, ref = _open(conn, path)                  # 建玉を開いてから評価を始める（評価の間は新しく開けない）
    r = evaluation.start(conn, cfg, NOW)
    assert r["ends_at"] > r["started_at"]
    with pytest.raises(ValueError, match="もう始まって"):
        evaluation.start(conn, cfg, NOW + timedelta(hours=1))
    _extend(conn, 6)
    run_paper(conn, cfg, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=7))
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=7))
    assert s["state"] == "running" and s["positions"] == 1
    # コマンドで確かめる用の日本時間の文字（2026-10-01 オーナー依頼3）
    j = NOW.astimezone(evaluation.views.JST)
    assert s["started_jst"] == f"{j:%Y-%m-%d %H:%M}（日本時間）"
    assert s["ends_jst"].endswith("（日本時間）") and s["ends_jst"] > s["started_jst"]
    assert 5 < s["observed_hours"] <= 6.0 and s["estimated_hours"] == 0
    assert s["left_hours"] == pytest.approx(cfg.evaluation.days * 24 - 7)
    # 実績は1時間ごとの行の合計（開いた時の1回きりの費用は入らない）を1日あたりにしたもの
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts", (ref.position_id,)).fetchall()[1:]
    act = sum(r["net"] for r in rows) / (s["observed_hours"] / 24)
    assert s["actual_net_day"] == pytest.approx(act)
    assert sum(c["actual"] for c in s["compare"]) == pytest.approx(act)
    assert [c["key"] for c in s["compare"]] == list(CATS)
    assert s["predicted_net_day"] is not None and s["closer"] in ("hold", "sell")
    assert s["coverage"] and s["coverage"][0]["expected"] > 0


def test_stop_keeps_record(world):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    evaluation.stop(conn, NOW + timedelta(hours=2))
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=5))
    assert s["state"] == "stopped" and s["left_hours"] == 0
    assert s["elapsed_hours"] == pytest.approx(2.0)
    evaluation.start(conn, cfg, NOW + timedelta(hours=5))          # やめた後はまた始められる
    assert evaluation.summary(conn, cfg, NOW + timedelta(hours=6))["state"] == "running"


def test_api_needs_confirm(client):  # noqa: F811
    c, conn, path = client
    assert c.get("/api/paper/evaluation").json()["state"] == "not_started"
    assert c.post("/api/paper/evaluation/start", json={}).status_code == 400
    # 練習の建玉がないときは始められない（2026-10-01 の決まり）
    r = c.post("/api/paper/evaluation/start", json={"confirm": True})
    assert r.status_code == 400 and "練習の建玉が1つもない" in r.json()["detail"]
    assert c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-weth"}).status_code == 200
    d = c.post("/api/paper/evaluation/start", json={"confirm": True}).json()
    assert "14日間" in d["message"]
    assert c.get("/api/paper/evaluation").json()["state"] == "running"
    assert c.post("/api/paper/evaluation/stop", json={"confirm": True}).status_code == 200
    assert c.get("/api/paper/evaluation").json()["state"] == "stopped"


def test_daily_judgement_and_verdict(world):  # noqa: F811
    import dataclasses

    path, conn = world
    base = _config(path)
    # 1日だけの評価。差の許容を総資産の100%にすると、記録のある日は必ず「満たす日」になる
    loose = dataclasses.replace(base, evaluation=dataclasses.replace(base.evaluation, days=1, day_gap_capital_pct=100))
    _open(conn, path)
    evaluation.start(conn, loose, NOW)
    _extend(conn, 26)
    run_paper(conn, loose, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=26))
    s = evaluation.summary(conn, loose, NOW + timedelta(hours=26))
    assert s["state"] == "finished" and len(s["days"]) == 1
    d = s["days"][0]
    assert d["done"] and d["hold_ok"] and d["sell_ok"] and d["capital"] == pytest.approx(1000.0)
    c = s["criteria"]
    assert c["coverage_ok"] and c["hold"] == {"ok_days": 1, "need_days": 1, "result": "pass"}
    assert c["sell"]["result"] == "pass"
    assert "儲かるかの判定ではありません" in s["disclaimer"]
    # 許容を0にすると、差がある日は満たさない → 不合格
    strict = dataclasses.replace(loose, evaluation=dataclasses.replace(loose.evaluation, day_gap_pct=0, day_gap_capital_pct=0))
    s2 = evaluation.summary(conn, strict, NOW + timedelta(hours=26))
    assert s2["criteria"]["hold"]["result"] == "fail"


def test_day_without_records_does_not_count(world):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=49))
    assert [d["hold_ok"] for d in s["days"]][:2] == [False, False]
    assert s["criteria"]["hold"]["ok_days"] == 0 and s["criteria"]["hold"]["result"] == "running"
    assert s["criteria"]["hold"]["need_days"] == 10              # 14日 × 70% = 9.8 → 10日


def test_coverage_counts_only_the_evaluated_venues(world):  # noqa: F811
    # 途中から増えた観察だけの会場（M6 の Alandale）の収集は、評価の集まり具合に入れない（2026-09-30 オーナー条件）
    from farm_radar.db import database as db
    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    _extend(conn, 6)
    conn.execute("INSERT INTO venues(id, name, chain) VALUES ('alandale-robinhood', 'Alandale', 'robinhood')")
    db.start_run(conn, "alandale-robinhood", NOW + timedelta(hours=6), NOW + timedelta(hours=6))
    conn.commit()
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=7))
    assert [c["venue_id"] for c in s["coverage"]] == ["up-robinhood"]
    # 建玉がまだないときは、練習のできる会場（config の venues のうち practice: false でないもの）で数える
    assert evaluation.evaluation_venues(conn, cfg, NOW + timedelta(days=30), NOW + timedelta(days=31)) == ["up-robinhood"]


def test_new_practice_is_blocked_while_the_evaluation_runs(world):  # noqa: F811
    # 2026-09-30 オーナー決定①: 評価の間は新しい練習を始めない（上限に余裕があっても）。持っている建玉はそのまま続く
    import dataclasses

    from farm_radar.execution.paper import PaperError, PaperExecutor

    path, conn = world
    big = dataclasses.replace(_config(path), limits={"position_usd": 1000, "total_usd": 10000,
                                                      "per_venue_share": 1.0, "trades_per_day": 20})
    _open(conn, path)
    evaluation.start(conn, big, NOW)
    ex = PaperExecutor(conn, big, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1))
    with pytest.raises(PaperError, match="評価中のため") as e:
        ex.open_position("up-robinhood:p-nvda", 1000.0)
    assert "まで" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0] == 1
    # 設定で外せる。評価が終わったあと・途中でやめたあとは始められる
    off = dataclasses.replace(big, evaluation=dataclasses.replace(big.evaluation, block_new_practice=False))
    PaperExecutor(conn, off, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1)).open_position("up-robinhood:p-nvda", 1000.0)
    after = NOW + timedelta(days=big.evaluation.days, minutes=1)
    PaperExecutor(conn, big, TOKENS, fx=FakeFx(), now=after).open_position("up-robinhood:p-up", 1000.0)


def test_paper_api_shows_the_evaluation_block(client):  # noqa: F811
    c, conn, path = client
    assert c.get("/api/paper").json()["evaluation_block"] is None
    assert c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-weth"}).status_code == 200
    c.post("/api/paper/evaluation/start", json={"confirm": True})
    block = c.get("/api/paper").json()["evaluation_block"]
    assert block["until"] and "評価中のため" in block["message"]
    r = c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-nvda"})
    assert r.status_code == 400 and "評価中のため" in r.json()["detail"]
    c.post("/api/paper/evaluation/stop", json={"confirm": True})
    assert c.get("/api/paper").json()["evaluation_block"] is None


def test_reference_uses_the_score_at_that_time(world):  # noqa: F811
    # 2026-09-30 オーナー決定②: 始めたときの見込み（合否に使う）と並べて、その時点の最新のスコアで比べた参考を出す
    from farm_radar.scoring.run import score_venue

    from .test_scoring_run import FakeGT, _ctx

    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    _extend(conn, 12)
    s0 = evaluation.summary(conn, cfg, NOW + timedelta(hours=12))
    # スコアが変わらなければ、参考は始めたときの見込みとほぼ同じ
    assert s0["reference"]["net_day"] == pytest.approx(s0["predicted_net_day"], rel=1e-6)
    # 途中でボーナスが0になり、スコアを計算し直した（木曜の切り替えのあとに近い形）
    last = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    _extend(conn, 14)
    conn.execute("UPDATE pool_snapshots SET reward_rate_effective_raw='0', reward_rate_raw='0' WHERE ts>?", (last,))
    conn.commit()
    score_venue(conn, _ctx(FakeGT()), now=NOW + timedelta(hours=13, minutes=10))
    run_paper(conn, cfg, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=26))
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=26))
    ref = s["reference"]
    assert ref["net_day"] < s["predicted_net_day"]           # ボーナスが消えたあとの見込みは下がる
    d = s["days"][0]
    assert d["done"] and d["reference"] is not None and d["reference"] < d["predicted"]
    assert isinstance(d["reference_ok"], bool) and isinstance(d["flip"], bool)
    assert ref["need_days"] == s["criteria"]["hold"]["need_days"] and "合否" in ref["note"]
    # 合否の決め方は変わらない（持ち続ける前提の判定は、始めたときの見込みとの比較のまま）
    assert s["criteria"]["hold"]["ok_days"] == sum(1 for x in s["days"] if x["done"] and x["hold_ok"])
