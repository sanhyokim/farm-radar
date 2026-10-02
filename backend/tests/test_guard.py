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


def test_every_listed_opportunity_has_a_provisional_danger_score(cfg):  # noqa: F811
    for o in _collect(cfg).values():
        d = o.to_dict(1000.0, 30.0)
        sf = d["safety"]
        assert sf["level"] in ("low", "mid", "high", "very_high") and sf["provisional"] is True
        assert sf["score"] == pytest.approx(sum(p["points"] for p in sf["parts"]))
        assert sf["recommend"]["note"]


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
    assert g["loss_line"]["state"] == "none" and g["loss_line"]["pct"] == -config.guard.loss_lines["day"]["stop"]


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
