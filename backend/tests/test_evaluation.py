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


# --- 参考の練習（2026-10-01 オーナー提案【3】案B） --------------------------------------------------

def _strip(s):
    return {k: v for k, v in s.items() if k != "reference_tracks"}


def test_reference_practice_does_not_change_the_evaluation(world):  # noqa: F811
    """評価の間に参考の練習を開いても、合否の計算（合否用の建玉だけ）は1つも変わらない。"""
    from farm_radar.execution.paper import PaperExecutor

    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(minutes=30))
    ref = ex.open_position("up-robinhood:p-nvda", 1000.0, purpose="reference")
    _extend(conn, 30)
    run_paper(conn, cfg, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=30))
    at = NOW + timedelta(hours=30)
    with_ref = evaluation.summary(conn, cfg, at)
    assert with_ref["positions"] == 1 and with_ref["open_positions"] == 1
    tr = with_ref["reference_tracks"]["tracks"]
    assert [t["id"] for t in tr] == [ref.position_id] and tr[0]["pair"] == "USDG/NVDA" and tr[0]["done_days"] == 1
    assert tr[0]["days"][0]["predicted"] is not None and tr[0]["need_days"] == 10
    # 参考の練習を消して数え直しても、合否の側は同じ
    conn.execute("DELETE FROM position_pnl WHERE position_id=?", (ref.position_id,))
    conn.execute("DELETE FROM risk_events WHERE position_id=?", (ref.position_id,))
    conn.execute("DELETE FROM ledger WHERE position_id=?", (ref.position_id,))
    conn.execute("DELETE FROM positions WHERE id=?", (ref.position_id,))
    conn.commit()
    without = evaluation.summary(conn, cfg, at)
    assert _strip(with_ref) == _strip(without)
    assert without["reference_tracks"]["tracks"] == []


def test_reference_practice_limits_and_room(world):  # noqa: F811
    """参考の練習は上限（合計・会場ごと）の外。同時に持てる数と1つの金額は守る。設定で上限の中にも戻せる。"""
    import dataclasses

    from farm_radar.execution.paper import PaperError, PaperExecutor

    path, conn = world
    cfg = dataclasses.replace(_config(path), limits={"position_usd": 1000, "total_usd": 1000,
                                                      "per_venue_share": 1.0, "trades_per_day": 20})
    _open(conn, path)                                     # 合計の上限 $1,000 をこれで使い切る
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(minutes=5))
    with pytest.raises(PaperError, match="合計の上限"):
        ex.open_position("up-robinhood:p-nvda", 1000.0)
    ex.open_position("up-robinhood:p-nvda", 1000.0, purpose="reference")
    with pytest.raises(PaperError, match="1つの建玉の上限"):
        ex.open_position("up-robinhood:p-up", 2000.0, purpose="reference")
    one = dataclasses.replace(cfg, evaluation=dataclasses.replace(cfg.evaluation, reference_max_open=1))
    with pytest.raises(PaperError, match="同時に1つまで"):
        PaperExecutor(conn, one, TOKENS, fx=FakeFx(), now=NOW).open_position("up-robinhood:p-up", 1000.0, purpose="reference")
    inside = dataclasses.replace(cfg, evaluation=dataclasses.replace(cfg.evaluation, reference_outside_limits=False))
    with pytest.raises(PaperError, match="合計の上限"):
        PaperExecutor(conn, inside, TOKENS, fx=FakeFx(), now=NOW).open_position("up-robinhood:p-up", 1000.0, purpose="reference")
    with pytest.raises(PaperError, match="種類"):
        ex.open_position("up-robinhood:p-up", 1000.0, purpose="other")


def test_reference_practice_while_evaluating_and_interrupt(world):  # noqa: F811
    """評価の間でも参考の練習は始められる。評価の建玉が全部閉じたら、参考が残っていても評価は中断。"""
    from farm_radar.execution.base import PositionRef
    from farm_radar.execution.paper import PaperError, PaperExecutor

    path, conn = world
    cfg = _config(path)
    _, main = _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(minutes=5))
    with pytest.raises(PaperError, match="評価中のため"):
        ex.open_position("up-robinhood:p-nvda", 1000.0)
    ex.open_position("up-robinhood:p-nvda", 1000.0, purpose="reference")
    ex.close_position(PositionRef(main.position_id))
    s = evaluation.summary(conn, cfg, NOW + timedelta(minutes=10))
    assert s["state"] == "interrupted" and s["open_positions"] == 0
    assert len(s["reference_tracks"]["tracks"]) == 1


def test_reference_daily_loss_closes_only_reference(world, monkeypatch):  # noqa: F811
    """参考の練習の今日の損が基準を超えても、閉じるのは参考の練習だけ。評価の建玉と再開の状態はそのまま。"""
    from farm_radar.execution import risk_job
    from farm_radar.execution.paper import PaperExecutor

    path, conn = world
    cfg = _config(path)
    _, main = _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(minutes=5))
    ref = ex.open_position("up-robinhood:p-nvda", 1000.0, purpose="reference")
    monkeypatch.setattr(risk_job, "today_net", lambda conn, now, reference=None: -500.0 if reference else 0.0)
    risk_job.run_risk(conn, cfg, TOKENS, PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1)),
                      NOW + timedelta(hours=1))
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM positions")}
    assert st[main.position_id] == "open" and st[ref.position_id] == "closed"
    assert not risk_job.paper_state(conn)["stopped"]
    ev = conn.execute("SELECT * FROM risk_events WHERE action='closed_reference'").fetchone()
    assert ev is not None and ev["message_ja"].startswith("参考の練習")
    assert evaluation.summary(conn, cfg, NOW + timedelta(hours=1))["state"] == "running"


def test_api_reference_practice(client):  # noqa: F811
    c, conn, path = client
    assert c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-weth"}).status_code == 200
    c.post("/api/paper/evaluation/start", json={"confirm": True})
    d = c.get("/api/paper").json()
    assert d["reference"]["can_open"] and d["reference"]["max"] == 3 and "合否に使いません" in d["reference"]["note"]
    r = c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-nvda", "purpose": "reference"})
    assert r.status_code == 200
    d = c.get("/api/paper").json()
    assert {p["pair"]: p["reference"] for p in d["open"]} == {"WETH/USDG": False, "USDG/NVDA": True}
    assert d["reference"]["open"] == 1 and d["open_total_usd"] == 1000
    e = c.get("/api/paper/evaluation").json()
    assert e["open_positions"] == 1 and len(e["reference_tracks"]["tracks"]) == 1


# --- 週ごとの見込み（2026-10-01 オーナー提案【2】3） -------------------------------------------------

def test_weekly_prediction_after_the_flip(world):  # noqa: F811
    """木曜の切り替えのあとは、落ち着いたあと（2時間後）の最初のスコアで比べる。始めたときの見込みの結果も並べる。"""
    import dataclasses
    from datetime import UTC, datetime

    from farm_radar.scoring.run import score_venue

    from .test_scoring_run import FakeGT, _ctx

    path, conn = world
    cfg = _config(path)
    flip = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)                   # NOW（月曜）のあとの最初の切り替え
    _open(conn, path)
    evaluation.start(conn, cfg, NOW)
    _extend(conn, 80)
    # 切り替えのあとはボーナスが0になった
    conn.execute("UPDATE pool_snapshots SET reward_rate_effective_raw='0', reward_rate_raw='0' WHERE ts>=?",
                 (flip.isoformat(),))
    conn.commit()
    score_venue(conn, _ctx(FakeGT()), now=flip + timedelta(hours=2, minutes=10))
    run_paper(conn, cfg, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=80))
    at = NOW + timedelta(hours=80)
    s = evaluation.summary(conn, cfg, at)
    assert s["criteria"]["prediction"] == "weekly" and s["weekly"]["enabled"]
    assert [u["flip_at"] for u in s["weekly"]["used"]] == [flip.isoformat()]
    days = s["days"]
    assert [d["week_hours"] > 0 for d in days[:4]] == [False, False, False, True]
    assert days[3]["predicted"] < days[3]["predicted_start"]          # ボーナスが消えたあとの見込みは下がる
    assert days[0]["predicted"] == pytest.approx(days[0]["predicted_start"])
    assert s["criteria"]["start_only"]["hold"]["ok_days"] == sum(1 for d in days if d["done"] and d["start_ok"])
    # 設定で切ると、今までどおり始めたときの見込みだけで比べる
    off = dataclasses.replace(cfg, evaluation=dataclasses.replace(cfg.evaluation, weekly_prediction=False))
    s2 = evaluation.summary(conn, off, at)
    assert s2["criteria"]["prediction"] == "start" and s2["criteria"]["start_only"] is None
    assert s2["days"][3]["predicted"] == pytest.approx(days[3]["predicted_start"])
    assert s2["weekly"]["used"] == []
