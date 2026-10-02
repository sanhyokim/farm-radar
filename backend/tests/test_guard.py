"""N2c: 「守る」の数字（置いている額と上限・損失ライン）と、安全度（仮の3段階）のテスト。"""

import dataclasses
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, guard, safety
from farm_radar.config import load_config
from farm_radar.db import database as db

from .test_opportunities import _collect, cfg, feeds  # noqa: F401  （fixture）

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)


def test_venue_safety_from_merkl_trust_data():
    listed_old = int(NOW.timestamp()) - 400 * 86400
    assert safety.venue_from_merkl({"audits": 2, "hacks": 0, "listed_at": listed_old}, NOW)["level"] == "high"
    assert safety.venue_from_merkl({"audits": 2, "hacks": 1, "listed_at": listed_old}, NOW)["level"] == "low"
    v = safety.venue_from_merkl({"audits": 0, "hacks": 0, "listed_at": int(NOW.timestamp()) - 10 * 86400}, NOW)
    assert v["level"] == "mid" and "監査の記録がない" in v["reasons"] and v["provisional"] is True
    assert safety.venue_from_merkl(None, NOW)["level"] == "mid"
    small = safety.venue_from_merkl({"audits": 2, "hacks": 0, "listed_at": listed_old, "tvl": 78_672.0}, NOW)
    assert small["level"] == "mid" and "$79K" in small["reasons"][0]


def test_venue_safety_from_registry_uses_the_same_marks_as_the_venue_screen():
    assert safety.venue_from_registry({"contracts": {"a": {"unverified": True}}})["level"] == "low"
    assert safety.venue_from_registry({"contracts": {"a": {"unverified": False}}, "audited": True})["level"] == "high"
    v = safety.venue_from_registry({"contracts": {}, "audited": True,
                                    "warnings": [{"code": "C4", "title_ja": "手で決める配分"}]})
    assert v["level"] == "mid" and v["reasons"] == ["手で決める配分"]


def test_opportunity_safety_goes_down_with_marks():
    high = {"level": "high", "reasons": ["ok"]}
    assert safety.opportunity({"flags": [], "unprotected": [], "days_left": 20}, high)["level"] == "high"
    o = {"flags": [{"level": "exclude", "text": "小さすぎる"}], "unprotected": []}
    assert safety.opportunity(o, high)["level"] == "low"
    o = {"flags": [{"level": "warn", "text": "値下がり未計算"}], "unprotected": [], "days_left": 3}
    s = safety.opportunity(o, high)
    assert s["level"] == "mid" and s["reasons"][0] == "値下がり未計算" and "3.0 日" in s["reasons"][1]
    # 預かり証の金庫の損は、保険で守れない値動きとしては数えない（月 −3% で引いてある）
    o = {"flags": [], "unprotected": ["ボーナスの預かり証（X）の金庫の損・引き出しの待ち"], "days_left": 30}
    assert safety.opportunity(o, high)["level"] == "high"


def test_every_listed_opportunity_has_a_provisional_safety(cfg):  # noqa: F811
    for o in _collect(cfg).values():
        d = o.to_dict(1000.0, 30.0)
        assert d["safety"]["level"] in ("high", "mid", "low") and d["safety"]["provisional"] is True


def test_guard_summary_without_positions(tmp_path):
    config = dataclasses.replace(load_config(), database_path=tmp_path / "m.sqlite3")
    conn = db.connect(config.database_path)
    g = guard.summary(conn, config, NOW)
    lim = config.limits
    assert g["placed_usd"] == 0 and g["total_left_usd"] == lim["total_usd"]
    assert g["limits"]["venue_cap_usd"] == lim["total_usd"] * lim["per_venue_share"]
    up = next(v for v in g["venues"] if v["venue_id"] == "up-robinhood")
    assert up["left_usd"] == g["limits"]["venue_cap_usd"] and up["safety"]["provisional"] is True
    assert {c["chain"] for c in g["chains"]} == set(config.chains)
    assert g["loss_line"]["state"] == "none" and g["loss_line"]["pct"] == config.risk.emergency_daily_loss_pct


def test_guard_and_detail_api(cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    api._opps_cache.clear()
    c = TestClient(api.app)
    assert c.get("/api/guard").json()["positions"] == 0
    d = c.get("/api/opportunities/o-eth").json()
    assert d["item"]["key"] == "o-eth" and d["item"]["safety"]["provisional"] is True
    assert d["practice"]["available"] is False and "N6" in d["practice"]["note"]
    assert c.get("/api/opportunities/nope").status_code == 404
    assert c.get("/api/opportunities/o-eth?amount=7").status_code == 400


def test_merkl_venue_info_is_marked_as_matched_by_name_only():
    # オーナー依頼 2026-10-02 17:07 JST: 会場の情報が名前だけで結びついている行に「仮・会場の見分けが不確か」
    listed_old = int(NOW.timestamp()) - 400 * 86400
    v = safety.venue_from_merkl({"audits": 2, "hacks": 0, "listed_at": listed_old, "slug": "hyperdrive"}, NOW)
    assert v["uncertain_match"] is True and v["level"] == "high"
    assert v["reasons"][-1].startswith(safety.UNCERTAIN_MATCH) and "「hyperdrive」" in v["reasons"][-1]
    assert safety.venue_from_merkl({"hacks": 1}, NOW)["uncertain_match"] is True
    # 会場の情報がないとき・自分で読む会場（登録の住所）は、名前で結びついていないので印なし
    assert safety.venue_from_merkl(None, NOW)["uncertain_match"] is False
    assert safety.venue_from_registry({"contracts": {}, "audited": True})["uncertain_match"] is False
    # 入れる先にも引き継ぐが、安全度は下げない
    o = safety.opportunity({"flags": [], "unprotected": [], "days_left": 30}, v)
    assert o["uncertain_match"] is True and o["level"] == "high"


@pytest.mark.parametrize("total,share,position", [(100, 0.5, 100), (1000, 1.0, 1000), (10000, 0.5, 10000),
                                                   (100000, 0.5, 10000)])
def test_limits_can_be_set_from_100_to_100000(tmp_path, total, share, position):
    # オーナー依頼 2026-10-02 17:07 JST: 上限は設定で変えられること（はじめ $100〜$1,000、のちに $10,000、作りは $100,000）
    base = load_config()
    config = dataclasses.replace(base, database_path=tmp_path / "m.sqlite3",
                                 limits={**base.limits, "total_usd": total, "per_venue_share": share,
                                         "position_usd": position})
    g = guard.summary(db.connect(config.database_path), config, NOW)
    assert g["limits"]["total_usd"] == total and g["total_left_usd"] == total
    assert g["limits"]["venue_cap_usd"] == pytest.approx(total * share)
    assert all(v["cap_usd"] == pytest.approx(total * share) for v in g["venues"])


@pytest.mark.parametrize("bad", [{"total_usd": "10,000"}, {"total_usd": 0}, {"position_usd": -1},
                                 {"per_venue_share": 1.5}, {"per_venue_share": 0}])
def test_limits_with_a_wrong_shape_stop_with_a_clear_message(tmp_path, bad):
    import yaml

    from farm_radar.config import REPO_ROOT, ConfigError
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["limits"].update(bad)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="limits."):
        load_config(tmp_path / "config.yaml")
