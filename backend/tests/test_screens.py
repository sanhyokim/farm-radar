"""画面の見直し（2026-09-30 オーナー依頼 7〜33）で足した表示用のデータのテスト。判定・練習・評価の計算は変えない。"""

from datetime import timedelta

from farm_radar import home_view, views
from farm_radar.execution import risk_job

from .test_api import client as api_client  # noqa: F401
from .test_paper import client, world  # noqa: F401
from .test_scoring_run import NOW


def test_scores_slim_and_the_short_reasons(api_client):  # noqa: F811
    full = api_client.get("/api/scores").json()["scores"]
    slim = api_client.get("/api/scores?slim=1").json()["scores"]
    assert len(full) == len(slim) and "details" in full[0] and "details" not in slim[0]
    for d in slim:
        # 結論のあとの「いちばん大事な理由」、「なぜこの判定か」の短い項目、$550 を両替したときのずれ
        assert d["key_reason"]
        assert d["checks"] and {c["state"] for c in d["checks"]} <= {"ok", "warn", "bad", "info"}
        assert d["slippage_pct"] is None or d["slippage_pct"] >= 0
        assert d["reason_parts"]["main"]
    # 判定の数と中身は、slim でも変わらない
    assert [(d["pool_id"], d["signal"], d["net_daily_pct"]) for d in full] == \
           [(d["pool_id"], d["signal"], d["net_daily_pct"]) for d in slim]


def test_detail_and_home_show_the_same_score(api_client):  # noqa: F811
    # オーナー依頼 21: ホームと詳しい画面で、計算の時刻と数字を同じにする
    home = api_client.get("/api/home").json()
    for p in home["near"] + home["greens"]:
        d = api_client.get(f"/api/pools/{p['pool_id']}").json()["score"]
        assert d["ts"] == p["ts"] and d["net_daily_pct"] == p["net_daily_pct"] and d["key_reason"] == p["key_reason"]
        assert p["spark"] and p["spark"][-1] == p["net_daily_pct"]


def test_pulse_is_light_and_says_when_collection_stopped(api_client, monkeypatch):  # noqa: F811
    from farm_radar import api
    d = api_client.get("/api/pulse").json()
    assert d["scored_at"] and d["snapshot_minutes"] > 0
    assert d["stale"] is False and d["last_ok_at"]
    # 最後に集めてから時間がたつと「止まっている」
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(hours=3))
    assert api_client.get("/api/pulse").json()["stale"] is True


def test_checks_and_key_reason_follow_the_numbers():
    d = {"net_daily_pct": 0.1, "has_perp": 1, "tvl_usd": 5e5, "warnings": [{"level": "minor", "message_ja": "x"}],
         "best_r": 3.0, "details": {"ranges": [{"r_pct": 3.0, "mode": "staked", "income": 10.0, "income_unstaked": 1.0,
                                                "gamma": 2.0, "rebalance": 1.0, "rebalances_per_day": 1.2}]},
         "reason_ja": "純日利 0.10%（総資産あたり）＝ …\n収入のほとんどがボーナス\n補足"}
    states = [c["state"] for c in views.judge_checks(d, 0.3, 0.0, 2e5)]
    assert states == ["warn", "ok", "info", "warn"]
    assert "ほとんどがボーナス" in views.key_reason(d) and "$1.00" in views.key_reason(d)
    assert views.reason_parts(d["reason_ja"]) == {"formula": "純日利 0.10%（総資産あたり）＝ …",
                                                   "main": "収入のほとんどがボーナス", "notes": ["補足"]}
    # 判定できないときは、データが足りない理由だけ
    assert views.judge_checks({"net_daily_pct": None, "reason_ja": "取引の記録が少ない"}, 0.3, 0.0, 2e5) == \
        [{"state": "bad", "text": "取引の記録が少ない"}]
    # 危険な警告は、理由のいちばん上に出す
    d["warnings"] = [{"level": "major", "message_ja": "報酬トークンが7日で -42%"}]
    assert views.key_reason(d) == "危険な警告があります: 報酬トークンが7日で -42%"


def test_home_has_todo_practice_summary_and_evaluation(client):  # noqa: F811
    c, conn, path = client
    h = c.get("/api/home").json()
    assert h["paper"]["positions"] == [] and h["paper"]["text"] == "持っている建玉はありません。"
    assert h["evaluation"] is None
    assert c.post("/api/paper/positions", json={"pool_id": "up-robinhood:p-weth"}).status_code == 200
    c.post("/api/paper/evaluation/start", json={"confirm": True})
    h = c.get("/api/home").json()
    p = h["paper"]
    assert len(p["positions"]) == 1 and p["total"]["capital"] == 1000
    pos = p["positions"][0]
    assert pos["pair"] and pos["spark"] and pos["status"] and pos["tone"] in ("ok", "attention")
    assert p["total"]["value"] == pos["value"] and p["total"]["today_usd"] == pos["today_usd"]
    assert len(p["hourly"]["bars"]) == 24
    assert p["spark"] and p["spark"][-1] == pos["value"]
    ev = h["evaluation"]
    assert ev["state"] == "running" and ev["day"] == 1 and len(ev["marks"]) == ev["days"] == 14
    assert ev["marks"][0]["state"] == "running" and ev["marks"][1]["state"] == "none"
    # 今日やること: 危険 → 注意 → お知らせ の順
    order = [home_view.LEVEL_ORDER[t["level"]] for t in h["todo"]]
    assert order == sorted(order)
    # 練習を止めると「練習は停止中です」が出る
    risk_job.owner_stop(conn, NOW, "テスト")
    h = c.get("/api/home").json()
    assert any(t["title"] == "練習は停止中です" and t["level"] == "attention" for t in h["todo"])
    assert h["paper"]["stopped"] and h["paper"]["text"].startswith("停止中")


def test_todo_puts_danger_first_and_flip_only_when_near(world):  # noqa: F811
    path, conn = world
    from .test_paper import _config
    cfg = _config(path)
    coll = [{"name": "up.", "observe": False, "stale": True, "last_ok_at": None},
            {"name": "Alandale", "observe": True, "stale": True, "last_ok_at": None}]
    paper = {"enabled": True, "stopped": True}
    flip = (NOW + timedelta(hours=10)).isoformat(timespec="seconds")
    items = home_view.todo(conn, cfg, NOW, collection=coll, paper=paper, cards=[], evaluation=None,
                           plans_soon=[], flip_at=flip)
    assert [t["level"] for t in items] == ["danger", "attention", "attention", "info"]
    assert items[0]["title"].startswith("up. のデータの収集が止まっています")
    assert "切り替え" in items[-1]["title"]
    far = (NOW + timedelta(days=5)).isoformat(timespec="seconds")
    items = home_view.todo(conn, cfg, NOW, collection=[], paper={"enabled": True, "stopped": False}, cards=[],
                           evaluation=None, plans_soon=[], flip_at=far)
    assert items == []
