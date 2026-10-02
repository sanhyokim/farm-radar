"""損の線（N4b。SPEC 13.1 の追加の決定 10）のテスト。"""

import json
from datetime import timedelta

import pytest

from farm_radar.config import GuardSettings
from farm_radar.execution import loss_lines, risk_job
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import PaperError, PaperExecutor

from .test_paper import WETH_POOL, FakeFx, _config, _extend, _open, world  # noqa: F401
from .test_risk import _events, _set_score, calm  # noqa: F401
from .test_scoring_run import NOW, TOKENS

G = GuardSettings()


def _pnl(conn, pid, ts, **kw):
    row = {"income": 0.0, "direction": 0.0, "gamma": 0.0, "hedge": 0.0, "other": 0.0, "haircut": 0.0, **kw}
    conn.execute("INSERT INTO position_pnl(position_id, ts, income, direction, gamma, hedge, other, haircut, net, "
                 "is_estimated) VALUES (?,?,?,?,?,?,?,?,?,0)",
                 (pid, ts.isoformat(timespec="seconds"), row["income"], row["direction"], row["gamma"], row["hedge"],
                  row["other"], row["haircut"], sum(row.values())))
    conn.commit()


def test_owner_table_is_the_default():
    assert G.loss_lines["day"] == {"caution": -3.0, "no_new": -4.0, "stop": -5.0}
    assert G.loss_lines["week"] == {"caution": -7.0, "no_new": -10.0, "stop": -12.0}
    assert G.loss_lines["since_start"] == {"caution": -10.0, "no_new": -15.0, "stop": -20.0}


def test_levels_and_breakdown_include_unsold_bonus(world):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    pid = ref.position_id
    t = NOW + timedelta(minutes=30)
    # プールの値動き −$20、まだ売っていないボーナスの値下がり −$18、保険 +$5 → 開いた費用と合わせて −3% を越える
    _pnl(conn, pid, t, direction=-30.0, gamma=10.0, haircut=-18.0, hedge=5.0)
    st = loss_lines.status(conn, G, NOW + timedelta(hours=1))
    day = next(p for p in st["periods"] if p["period"] == "day")
    assert day["base_usd"] == 1000.0
    assert day["breakdown"]["pool"] == pytest.approx(-20.0) and day["breakdown"]["bonus"] == pytest.approx(-18.0)
    assert day["level"] == "caution" and st["level"] == "caution"
    assert day["main_cause"] == "pool"
    assert day["lines"]["stop"]["usd"] == pytest.approx(-50.0)
    assert loss_lines.no_new_reason(conn, G, NOW + timedelta(hours=1)) is None
    # さらにボーナスのコインが下がって −4% を越えたら、新しい練習は始めない
    _pnl(conn, pid, t + timedelta(minutes=15), haircut=-10.0)
    why = loss_lines.no_new_reason(conn, G, NOW + timedelta(hours=1))
    assert why and "新しく入らない" in why and "ボーナスのコイン" in why


def test_no_new_line_blocks_opening(world, monkeypatch):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    _pnl(conn, ref.position_id, NOW + timedelta(minutes=30), direction=-45.0)
    ex2 = PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=1))
    with pytest.raises(PaperError, match="損の線"):
        ex2.check_can_open("up-robinhood", 100.0)
    # 参考の練習は損の線の外（今までどおり）
    ex2.check_can_open("up-robinhood", 100.0, reference=True)


def test_resume_restarts_the_count(world):  # noqa: F811
    path, conn = world
    ex, ref = _open(conn, path)
    _pnl(conn, ref.position_id, NOW + timedelta(minutes=30), direction=-45.0)
    later = NOW + timedelta(hours=1)
    assert loss_lines.status(conn, G, later)["level"] == "no_new"
    risk_job.set_stopped(conn, True, "test", later)
    risk_job.owner_resume(conn, later, "画面")
    st = loss_lines.status(conn, G, later + timedelta(minutes=1))
    assert st["level"] is None
    assert all(p["since"] >= later.isoformat(timespec="seconds") for p in st["periods"])


def test_caution_is_recorded_once_with_breakdown(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    _pnl(conn, ref.position_id, NOW + timedelta(minutes=30), haircut=-32.0)
    for h in (2, 3):
        _extend(conn, 1)
        run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=h))
    ev = [e for e in _events(conn) if e["kind"].startswith("loss_line")]
    assert [e["kind"] for e in ev] == ["loss_line_caution"]
    data = json.loads(ev[0]["data_json"])
    assert data["main_cause"] == "bonus" and data["provisional"] is True
