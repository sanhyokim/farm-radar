"""早く出る4段階（N4b。SPEC 13.2・13.1 の追加の決定 9）のテスト。練習では4つの段階とも自動で出て、どの決まりかを記録する。"""

import dataclasses
import json
from datetime import timedelta

import pytest

from farm_radar.config import RiskSettings
from farm_radar.execution import bonus_drop, risk_job
from farm_radar.execution import views as pviews
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import PaperExecutor
from farm_radar.risk.rules import STAGE, PortfolioInput, PositionInput, check_position

from .test_paper import WETH_POOL, FakeFx, _config, _extend, _open, world  # noqa: F401
from .test_risk import _events, _pos, calm  # noqa: F401
from .test_scoring_run import NOW, TOKENS

S = RiskSettings()


def _p(**kw):
    base = dict(pair="WETH/USDG", lower=90.0, upper=110.0, price=100.0, started_red=False)
    return PositionInput(**{**base, **kw})


def test_every_exit_rule_has_a_stage():
    assert STAGE["pool_funds_drop"] == STAGE["contract_change"] == STAGE["usdg_depeg"] == 1
    assert STAGE["reward_token_drop"] == STAGE["below_target"] == 2
    assert STAGE["bonus_drop"] == 3 and STAGE["better_place"] == 4


def test_below_target_needs_three_in_a_row_and_skips_low_starts():
    kw = dict(net_apr_pct=12.0, target_apr_pct=30.0, below_target_needed=3)
    assert check_position(_p(**kw, below_target_count=2), PortfolioInput(), S) == []
    f = check_position(_p(**kw, below_target_count=3), PortfolioInput(), S)
    assert f[0].kind == "below_target" and f[0].level == "exit"
    # 狙いより低いと分かって始めた練習には当てはめない（🔴で始めた練習と同じ考え）
    assert check_position(_p(**kw, below_target_count=5, started_below_target=True), PortfolioInput(), S) == []


def _score(conn, net, ts):
    conn.execute("UPDATE scores SET signal='green', net_daily_pct=?, ts=?", (net, ts.isoformat(timespec="seconds")))
    conn.commit()


def test_below_target_counts_each_quarter_hour_then_exits(world, calm):  # noqa: F811
    path, conn = world
    _score(conn, 1.0, NOW)                     # 年365%: 狙い（年30%）より上で始める
    ex, ref = _open(conn, path)
    pid = ref.position_id
    for i, minutes in enumerate((60, 65, 75, 90)):
        t = NOW + timedelta(minutes=minutes)
        _score(conn, 0.05, t)                  # 年18%: 狙いより下
        _extend(conn, 1, step_minutes=15) if i else None
        run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=t + timedelta(minutes=1))
        st = json.loads(_pos(conn, pid)["state_json"])
        if minutes == 65:
            assert st["below_target_n"] == 1   # 5分しかたっていない回は数えない
    p = _pos(conn, pid)
    assert p["status"] == "closed" and p["close_reason"] == "risk:below_target"
    ev = [e for e in _events(conn) if e["kind"] == "below_target"][0]
    assert json.loads(ev["data_json"])["stage"] == 2
    assert pviews.close_reason_ja(p["close_reason"]) == "段階2 利益が消えた・狙い利回りを続けて下回った"


@pytest.mark.stage4
def test_better_place_exits_when_gain_beats_twice_the_move_cost(world, calm):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    _extend(conn, 1)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    p = _pos(conn, ref.position_id)
    assert p["status"] == "closed" and p["close_reason"] == "risk:better_place"
    ev = [e for e in _events(conn) if e["kind"] == "better_place"][0]
    d = json.loads(ev["data_json"])
    assert d["stage"] == 4 and d["gain_usd"] > d["multiple"] * d["move_cost_usd"] > 0
    assert d["to_pool"] != WETH_POOL and "探す" in ev["message_ja"]


@pytest.mark.stage4
def test_better_place_stays_when_the_gain_is_small(world, calm):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    conn.execute("UPDATE scores SET net_daily_pct=1.0")      # どのプールも同じ利回り → 差がない
    conn.commit()
    pos = _pos(conn, ref.position_id)
    ex2 = PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1))
    assert risk_job.better_place(conn, _config(path), ex2, pos, NOW + timedelta(hours=1), 30.0) is None


def test_bonus_drop_exit_closes_when_leaving_is_best(world, monkeypatch):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, bonus_drop_action="exit"))
    ex, ref = _open(conn, path)
    r = {"state": "drop", "epoch_start": NOW.isoformat(), "prev_rate": "100", "cur_rate": "0", "ratio": 0.0,
         "hours_left": 10.0, "next_flip": (NOW + timedelta(hours=10)).isoformat(), "score_ts": NOW.isoformat(),
         "options": {"stay": {"usd": -5.0}, "exit": {"usd": -1.0}}, "best": "exit"}
    monkeypatch.setattr(bonus_drop, "compare", lambda *a, **k: dict(r))
    ex2 = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1))
    ids = bonus_drop.run(conn, cfg, ex2, NOW + timedelta(hours=1))
    ev = conn.execute("SELECT * FROM risk_events WHERE id=?", (ids[0],)).fetchone()
    assert ev["action"] == "closed" and json.loads(ev["data_json"])["stage"] == 3
    assert "建玉はそのまま" not in ev["message_ja"]
    assert _pos(conn, ref.position_id)["close_reason"] == "risk:bonus_drop"
