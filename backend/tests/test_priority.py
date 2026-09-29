"""会場が2つ以上あるときの順番と、観察だけの会場を休む条件（M6。SPEC 5.1章。2026-09-30 オーナー条件）。"""

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from farm_radar import ratelimit
from farm_radar.collectors import priority
from farm_radar.collectors.snapshot import RunResult
from farm_radar.config import load_config
from farm_radar.db import database as db

SLOT = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
OK = {"up-robinhood": RunResult(1, "ok", 100, 87, 0)}


def _conn(tmp_path, hours=24, miss=0):
    c = db.connect(tmp_path / "p.sqlite3")
    for i in range(hours * 4, 0, -1):
        t = SLOT - timedelta(minutes=15 * i)
        rid = db.start_run(c, "up-robinhood", t, t)
        status = "failed" if i <= miss else "ok"
        db.finish_run(c, rid, now=t, status=status, block_number=1, pools_ok=87, pools_failed=0)
    c.commit()
    return c


def _cfg():
    return load_config()


def test_split_keeps_practice_venues_first():
    up = SimpleNamespace(venue={"id": "up-robinhood"})
    al = SimpleNamespace(venue={"id": "alandale-robinhood", "practice": False})
    main, observe = priority.split_venues([al, up])
    assert main == [up] and observe == [al]


def test_reads_observe_venue_when_everything_is_fine(tmp_path):
    c = _conn(tmp_path)
    assert priority.defer_reason(c, _cfg(), OK, SLOT, SLOT + timedelta(minutes=1)) is None


def test_defers_when_main_venue_failed_this_slot(tmp_path):
    c = _conn(tmp_path)
    bad = {"up-robinhood": RunResult(2, "partial", 100, 80, 7)}
    assert "成功していない" in priority.defer_reason(c, _cfg(), bad, SLOT, SLOT + timedelta(minutes=1))


def test_defers_after_rate_limit(tmp_path):
    c = _conn(tmp_path)
    ratelimit.set_sink(tmp_path / "p.sqlite3")
    ratelimit.record("api.geckoterminal.com", "external", now=SLOT - timedelta(minutes=10))
    r = priority.defer_reason(c, _cfg(), OK, SLOT, SLOT + timedelta(minutes=1))
    assert "429" in r and "api.geckoterminal.com" in r
    # 30分より前の 429 なら休まない
    assert priority.defer_reason(c, _cfg(), OK, SLOT + timedelta(minutes=30), SLOT + timedelta(minutes=31)) is None


def test_defers_when_main_coverage_is_low(tmp_path):
    c = _conn(tmp_path, miss=4)            # 96回中4回失敗 = 95.8% < 97%
    r = priority.defer_reason(c, _cfg(), OK, SLOT, SLOT + timedelta(minutes=1))
    assert "95.8%" in r and "直近24時間" in r
    cfg = dataclasses.replace(_cfg(), observe_venues=dataclasses.replace(_cfg().observe_venues,
                                                                         defer_below_coverage_pct=95))
    assert priority.defer_reason(c, cfg, OK, SLOT, SLOT + timedelta(minutes=1)) is None


def test_coverage_uses_evaluation_period_while_running(tmp_path):
    c = _conn(tmp_path, miss=0)
    c.execute("INSERT INTO evaluations(started_at, ends_at, status) VALUES (?,?, 'running')",
              ((SLOT - timedelta(hours=4)).isoformat(), (SLOT + timedelta(days=13)).isoformat()))
    c.commit()
    cov, label = priority.main_coverage(c, _cfg(), ["up-robinhood"], SLOT + timedelta(minutes=1))
    assert cov == 1.0 and label == "評価を始めてから"


def test_defers_when_slot_is_late(tmp_path):
    c = _conn(tmp_path)
    r = priority.defer_reason(c, _cfg(), OK, SLOT, SLOT + timedelta(minutes=7))
    assert "予定時刻から 7 分" in r


def test_deferred_run_is_recorded_and_not_a_gap(tmp_path):
    c = _conn(tmp_path)
    priority.record_deferred(c, "alandale-robinhood", SLOT, SLOT, "テスト")
    priority.record_deferred(c, "alandale-robinhood", SLOT + timedelta(minutes=15), SLOT, "テスト")
    rows = c.execute("SELECT status, error FROM collection_runs WHERE venue_id='alandale-robinhood'").fetchall()
    assert [tuple(r) for r in rows] == [("deferred", "テスト")] * 2
    assert c.execute("SELECT COUNT(*) FROM collection_gaps WHERE venue_id='alandale-robinhood'").fetchone()[0] == 0
