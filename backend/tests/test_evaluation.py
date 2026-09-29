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
    r = evaluation.start(conn, cfg, NOW)
    assert r["ends_at"] > r["started_at"]
    with pytest.raises(ValueError, match="もう始まって"):
        evaluation.start(conn, cfg, NOW + timedelta(hours=1))
    _, ref = _open(conn, path)
    _extend(conn, 6)
    run_paper(conn, cfg, TOKENS, lighter=FakeLighter(), fx=FakeFx(), now=NOW + timedelta(hours=7))
    s = evaluation.summary(conn, cfg, NOW + timedelta(hours=7))
    assert s["state"] == "running" and s["positions"] == 1
    assert 5 < s["observed_hours"] <= 6.0 and s["estimated_hours"] == 0
    assert s["left_hours"] == pytest.approx(cfg.review.evaluation_days * 24 - 7)
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
    d = c.post("/api/paper/evaluation/start", json={"confirm": True}).json()
    assert "14日間" in d["message"]
    assert c.get("/api/paper/evaluation").json()["state"] == "running"
    assert c.post("/api/paper/evaluation/stop", json={"confirm": True}).status_code == 200
    assert c.get("/api/paper/evaluation").json()["state"] == "stopped"
