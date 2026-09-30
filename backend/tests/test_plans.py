"""「予定とメモ」「見送り中の会場」「配布終了まであと○日」（2026-09-30 オーナー追加）のテスト。"""

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from farm_radar import plans, views
from farm_radar.config import REPO_ROOT, load_venue
from farm_radar.db import database as db

EVAL_START = datetime(2026, 9, 29, 7, 5, 49, tzinfo=UTC)
EVAL_END = EVAL_START + timedelta(days=14)          # 2026-10-13 16:05 JST


def _conn(tmp_path, status="running"):
    conn = db.connect(tmp_path / "t.sqlite3")
    if status:
        conn.execute("INSERT INTO evaluations(started_at, ends_at, status) VALUES (?,?,?)",
                     (EVAL_START.isoformat(timespec="seconds"), EVAL_END.isoformat(timespec="seconds"), status))
        conn.commit()
    return conn


def _by_key(items):
    return {i["key"]: i for i in items}


def test_plans_file_has_the_owner_items_and_evaluation_end_comes_from_the_database(tmp_path):
    conn = _conn(tmp_path)
    items = _by_key(plans.plan_items(REPO_ROOT, conn, datetime(2026, 9, 30, 7, 0, tzinfo=UTC)))
    # PROGRESS.md の「今後の候補」「Phase 3a の準備」と、評価の終わり
    assert {"evaluation_end", "per_venue_share_review", "alchemy_key", "stonx_end"} <= set(items)
    ev = items["evaluation_end"]
    assert datetime.fromisoformat(ev["due"]) == EVAL_END
    assert ev["state"] == "later" and ev["days_left"] == pytest.approx(13.0, abs=0.01)
    # 上限0.7の見直しは評価の終わりが期限。Alchemy のカギは期限なし
    assert items["per_venue_share_review"]["due"] == ev["due"]
    assert items["alchemy_key"]["due"] is None and items["alchemy_key"]["state"] is None
    # STONX の配布終了は 2026-11-08 13:52 JST
    assert datetime.fromisoformat(items["stonx_end"]["due"]) == datetime(2026, 11, 8, 4, 52, tzinfo=UTC)
    assert items["stonx_end"]["group"] == "今後の候補"


def test_items_turn_soon_seven_days_before_and_past_after(tmp_path):
    conn = _conn(tmp_path)
    at = lambda now: _by_key(plans.plan_items(REPO_ROOT, conn, now))   # noqa: E731
    assert at(EVAL_END - timedelta(days=7, seconds=1))["evaluation_end"]["state"] == "later"
    assert at(EVAL_END - timedelta(days=7))["evaluation_end"]["state"] == "soon"      # ちょうど7日前から目立たせる
    assert at(EVAL_END - timedelta(minutes=1))["evaluation_end"]["state"] == "soon"
    assert at(EVAL_END)["evaluation_end"]["state"] == "past"
    stonx_end = datetime(2026, 11, 8, 4, 52, tzinfo=UTC)
    assert at(stonx_end - timedelta(days=3))["stonx_end"]["state"] == "soon"


def test_evaluation_end_is_empty_before_start_and_after_stopping(tmp_path):
    now = datetime(2026, 9, 30, tzinfo=UTC)
    for i, status in enumerate((None, "stopped")):
        (tmp_path / str(i)).mkdir()
        conn = _conn(tmp_path / str(i), status)
        ev = _by_key(plans.plan_items(REPO_ROOT, conn, now))["evaluation_end"]
        assert ev["due"] is None and ev["state"] is None and ev["text"]
    assert _by_key(plans.plan_items(REPO_ROOT, None, now))["evaluation_end"]["due"] is None


def test_skipped_venues_list_stonx_with_its_end_date():
    rows = {r["key"]: r for r in plans.skipped_venues(REPO_ROOT)}
    assert "2026-11-08 13:52" in rows["stonx-ekubo"]["reason"]
    assert all(r["name"] and r["reason"] and r["decided"] for r in rows.values())


def test_plans_file_dates_are_quoted_and_carry_a_timezone():
    # 引用符がないと yaml が日付の型に変えてしまい、タイムゾーンの扱いがぶれるため
    raw = yaml.safe_load((REPO_ROOT / "docs" / "plans.yaml").read_text(encoding="utf-8"))
    for p in raw["plans"]:
        if "due" in p:
            assert isinstance(p["due"], str) and datetime.fromisoformat(p["due"]).tzinfo is not None


def test_emission_end_shows_only_when_known_and_warns_within_seven_days():
    now = datetime(2026, 9, 30, tzinfo=UTC)
    # 終了日が分からない会場（up.・Alandale）は出さない（今の「⏰ 切り替え」のまま）
    for vid in ("up-robinhood", "alandale-robinhood"):
        assert views.emission_end_info(load_venue(vid, REPO_ROOT), now) is None
    venue = {"emission_end": {"at": "2026-11-08T13:52:00+09:00", "source": "chain"},
             "pool_emission_ends": [{"pool": "0xABC", "at": "2026-10-03T09:00:00+09:00"}]}
    far = views.emission_end_info(venue, now, "0xdef")
    assert far["days_left"] == pytest.approx(39.2, abs=0.1) and not far["soon"] and not far["ended"]
    near = views.emission_end_info(venue, now, "0xabc")          # プールごとの終了日が優先（大文字小文字は区別しない）
    assert near["soon"] and near["days_left"] == pytest.approx(3.0)
    assert views.emission_end_info(venue, datetime(2026, 11, 9, tzinfo=UTC))["ended"]
    with pytest.raises(ValueError):
        views.emission_end_info({"emission_end": {"at": "2026-11-08T13:52:00"}}, now)
