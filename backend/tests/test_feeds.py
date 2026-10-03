"""一覧の保存（N2a。作り直し SPEC 13.4）のテスト。外のサイトには行かず、偽の応答で確かめる。"""

import dataclasses
import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from farm_radar import api
from farm_radar.config import FeedSettings, chain_reads_enabled, load_config
from farm_radar.external.http import ExternalError, JsonGetter
from farm_radar.feeds import sources, store
from farm_radar.feeds import status as feeds_status
from farm_radar.feeds.run import FeedRunner, Fetcher, daily_due, run_source

NOW = datetime(2026, 10, 2, 3, 7, tzinfo=UTC)      # 日本時間 10/2 12:07

AERO_MD = """---
title: "The Latest from Aero"
---

# The Latest

[![Aero Launch Update](/images/a.webp)](/articles/aero-launch-update-all-systems-go/)

## [Aero Launch Update: All Systems Go](/articles/aero-launch-update-all-systems-go/)

[Sep 25, 2026](/articles/aero-launch-update-all-systems-go/)

### [Aero Lite is Live on Arc](/articles/aero-lite-is-live-on-arc/)

[Sep 16, 2026](/articles/aero-lite-is-live-on-arc/)
"""


def _opp(i, apr=50.0, campaigns=1):
    return {"id": f"o{i}", "chainId": 4663, "chain": {"id": 4663, "name": "Robinhood"}, "type": "UNISWAP_V4",
            "action": "POOL", "name": f"Provide liquidity to X{i}-USDG", "status": "LIVE", "apr": apr, "maxApr": 0.5,
            "nativeApr": 1.0, "tvl": 100000 + i, "dailyRewards": 300.0, "liveCampaigns": campaigns,
            "earliestCampaignStart": "1790294400", "latestCampaignEnd": "1790899200",
            "protocol": {"id": "uniswap", "name": "Uniswap"}, "tokens": [{"symbol": f"X{i}"}, {"symbol": "USDG"}],
            "campaigns": [{"id": f"m{i}-{k}", "campaignId": f"0xc{i}{k}", "opportunityId": f"o{i}",
                           "computeChainId": 4663, "distributionChainId": 4663, "type": "UNISWAP_V4", "subType": 0,
                           "distributionType": "DUTCH_AUCTION", "startTimestamp": 1790294400,
                           "endTimestamp": 1790899200, "amount": "512055000000000000000000",
                           "rewardToken": {"symbol": "ARB", "address": "0xabc", "decimals": 18, "price": 0.4},
                           "dailyRewards": 300.0, "apr": apr, "creatorAddress": "0xdef",
                           "campaignStatus": {"createdAt": 1790273916}} for k in range(campaigns)]}


class FakeFetcher:
    """URL ごとに本文（または例外）を返す偽の読み手。ページの番号も見る。"""

    def __init__(self, pages=None, texts=None, fail=None):
        self.pages = pages or {}
        self.texts = texts or {}
        self.fail = fail or {}
        self.calls = []

    def text(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if url in self.fail:
            raise self.fail[url]
        if url in self.pages:
            page = int((params or {}).get("page", 0))
            rows = self.pages[url]
            size = int((params or {}).get("items", 100))
            return json.dumps(rows[page * size:(page + 1) * size])
        return self.texts[url]


@pytest.fixture
def settings(tmp_path):
    return FeedSettings(database_path=tmp_path / "feeds.sqlite3", raw_dir=tmp_path / "feeds")


def _src(sid, **kw):
    return dataclasses.replace(sources.BY_ID[sid], **kw)


def test_aero_articles_reads_titles_slugs_and_dates():
    items = sources.aero_articles(AERO_MD)
    assert [i.key for i in items] == ["aero-launch-update-all-systems-go", "aero-lite-is-live-on-arc"]
    assert items[0].name == "Aero Launch Update: All Systems Go"
    assert items[0].info == {"url": "https://aero.xyz/articles/aero-launch-update-all-systems-go/",
                             "date": "Sep 25, 2026"}
    assert items[1].info["date"] == "Sep 16, 2026"


def test_merkl_pages_until_short_page_and_saves_snaps_and_campaigns(settings):
    src = _src("merkl_opportunities", page_size=2)
    f = FakeFetcher(pages={src.url: [_opp(1), _opp(2), _opp(3, campaigns=2)]})
    conn = store.connect(settings.database_path)
    out = run_source(conn, src, f, settings, NOW)
    assert out == {"source": "merkl_opportunities", "status": "ok", "items": 3, "new": 0, "gone": 0}
    # 2件ずつ読んで、少ないページ（1件）で止まる。並べ方（預かり額の多い順）を毎回つける
    assert [c[1]["page"] for c in f.calls] == ["0", "1"]
    assert all(c[1]["sort"] == "tvl" and c[1]["status"] == "LIVE,SOON" for c in f.calls)
    assert conn.execute("SELECT COUNT(*) FROM merkl_opportunity_snaps").fetchone()[0] == 3
    c = conn.execute("SELECT * FROM merkl_campaigns WHERE campaign_id='0xc31'").fetchone()
    assert c["opportunity_id"] == "o3" and c["amount_raw"] == "512055000000000000000000"
    assert c["reward_symbol"] == "ARB" and c["start_ts"] == 1790294400 and c["first_seen"] == store.iso(NOW)
    info = json.loads(conn.execute("SELECT info_json FROM feed_items WHERE key='o1'").fetchone()[0])
    assert info["protocol"] == "Uniswap" and info["tokens"] == ["X1", "USDG"] and info["apr"] == 50.0
    run = conn.execute("SELECT * FROM feed_runs").fetchone()
    assert run["status"] == "ok" and run["pages"] == 2 and run["raw_path"]
    with gzip.open(settings.raw_dir / run["raw_path"]) as fh:
        assert [o["id"] for o in json.load(fh)] == ["o1", "o2", "o3"]


def test_first_run_is_baseline_then_new_and_gone_are_counted(settings):
    src = _src("merkl_opportunities", page_size=100)
    conn = store.connect(settings.database_path)
    run_source(conn, src, FakeFetcher(pages={src.url: [_opp(1), _opp(2)]}), settings, NOW)
    out = run_source(conn, src, FakeFetcher(pages={src.url: [_opp(2), _opp(3)]}), settings,
                     NOW + timedelta(minutes=15))
    # o3 は新しく出てきた、o1 は終わった（最初の回の o1・o2 は「新しい」に数えない）
    assert out["new"] == 1 and out["gone"] == 1
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM feed_items")}
    assert rows["o1"]["baseline"] == 1 and rows["o3"]["baseline"] == 0
    assert rows["o3"]["first_seen"] == store.iso(NOW + timedelta(minutes=15))
    # 元の応答は1時間に1回だけ残す（15分後の回は残さない）
    raws = [r[0] for r in conn.execute("SELECT raw_path FROM feed_runs ORDER BY id")]
    assert raws[0] and raws[1] is None


def test_rate_limit_is_recorded_as_unknown_and_error_as_error(settings):
    conn = store.connect(settings.database_path)
    src = _src("llama_chains")
    out = run_source(conn, src, FakeFetcher(fail={src.url: ExternalError("x: HTTP 429", 429)}), settings, NOW)
    assert out["status"] == "rate_limited"
    out = run_source(conn, src, FakeFetcher(fail={src.url: ExternalError("x: HTTP 500", 500)}), settings, NOW)
    assert out["status"] == "error"
    out = run_source(conn, src, FakeFetcher(texts={src.url: "<html>not json"}), settings, NOW)
    assert out["status"] == "error"
    st = [r[0] for r in conn.execute("SELECT status FROM feed_runs ORDER BY id")]
    assert st == ["rate_limited", "error", "error"]
    assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 0


def test_json_getter_keeps_the_http_status_for_429():
    def handle(request):
        return httpx.Response(429)
    g = JsonGetter("https://example.org", max_retries=0, client=httpx.Client(transport=httpx.MockTransport(handle)),
                   sleep=lambda s: None)
    with pytest.raises(ExternalError) as e:
        g.get_text("https://example.org/x")
    assert e.value.status == 429


def test_daily_sources_run_once_a_day_after_the_hour(settings):
    conn = store.connect(settings.database_path)
    src = _src("llama_chains")
    # 一度も読めていなければ、時刻にかかわらずすぐ読む
    assert daily_due(conn, src, settings, NOW)
    run_source(conn, src, FakeFetcher(texts={src.url: json.dumps([{"name": "Base", "tvl": 1}])}), settings, NOW)
    assert not daily_due(conn, src, settings, NOW + timedelta(hours=1))
    # 次の日（日本時間）の 4 時より前はまだ、4 時を過ぎたら読む
    next_day_3am = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)     # 日本時間 10/3 3:30
    assert not daily_due(conn, src, settings, next_day_3am)
    assert daily_due(conn, src, settings, next_day_3am + timedelta(hours=1))


def test_runner_reads_only_the_cadence_asked(settings):
    texts = {s.url: "[]" for s in sources.SOURCES if s.cadence == "daily" and not s.page_size}
    pages = {s.url: [] for s in sources.SOURCES if s.page_size}
    texts["https://aero.xyz/articles/index.md"] = AERO_MD
    texts[sources.BY_ID["lighter_funding"].url] = "{}"
    texts["https://api.github.com/repos/dromos-labs/metadex-public/contents/deployment-addresses"] = "[]"
    r = FeedRunner(settings, FakeFetcher(pages=pages, texts=texts))
    out = r.run("hourly", NOW)
    # N5a: Aero の公式の住所と影の記録も1時間に1回（影の記録は設定が渡っていなければ何も作らない）
    assert [o["source"] for o in out] == ["aero_articles", "lighter_funding", "aero_addresses",
                                          "shadow_predictions"] and out[0]["items"] == 2
    out = r.run("daily", NOW)
    assert {o["source"] for o in out} == {s.id for s in sources.SOURCES if s.cadence == "daily"}
    assert r.run("daily", NOW + timedelta(minutes=30)) == []        # 今日はもう読んだ


def test_status_shows_sources_new_items_and_aero(settings):
    assert feeds_status.status(settings, NOW)["enabled"] is False     # まだ動いていない
    conn = store.connect(settings.database_path)
    src = _src("merkl_opportunities")
    run_source(conn, src, FakeFetcher(pages={src.url: [_opp(1)]}), settings, NOW)
    run_source(conn, src, FakeFetcher(pages={src.url: [_opp(1), _opp(2, apr=120.0)]}), settings,
               NOW + timedelta(minutes=60))
    aero = _src("aero_articles")
    run_source(conn, aero, FakeFetcher(texts={aero.url: AERO_MD}), settings, NOW)
    run_source(conn, aero, FakeFetcher(texts={aero.url: AERO_MD.replace(
        "# The Latest\n", "# The Latest\n\n## [Aero is Live](/articles/aero-is-live/)\n\n[Oct 22, 2026](/articles/aero-is-live/)\n")}),
        settings, NOW + timedelta(hours=1))
    conn.close()
    d = feeds_status.status(settings, NOW + timedelta(hours=1, minutes=5))
    assert d["enabled"] is True and d["aero"]["start"]["jst"] == "2026-10-22 09:00"
    by = {s["id"]: s for s in d["sources"]}
    assert by["merkl_opportunities"]["status"] == "ok" and by["merkl_opportunities"]["items"] == 2
    assert by["merkl_opportunities"]["new_today"] == 1 and not by["merkl_opportunities"]["late"]
    assert by["llama_yields"]["status"] == "none" and by["llama_yields"]["late"]
    assert d["problem"] is True          # まだ読んでいない一覧がある
    names = [(n["source"], n["key"]) for n in d["new"]]
    assert ("merkl_opportunities", "o2") in names and ("aero_articles", "aero-is-live") in names
    assert ("merkl_opportunities", "o1") not in names
    assert d["aero"]["articles"][0]["slug"] == "aero-is-live" and d["aero"]["articles"][0]["new"]
    assert d["disk_bytes"] > 0


def test_status_marks_a_run_stuck_after_30_minutes(settings):
    conn = store.connect(settings.database_path)
    store.start_run(conn, "aero_articles", NOW)
    conn.close()
    d = feeds_status.status(settings, NOW + timedelta(minutes=31))
    s = next(x for x in d["sources"] if x["id"] == "aero_articles")
    assert s["status"] == "stuck" and s["late"]


def test_feeds_settings_and_chain_switch(tmp_path):
    cfg = load_config()
    assert cfg.feeds.merkl_minutes == (7, 22, 37, 52)
    # 今の版の仕事の分（0・5・15・30・45）とは重ならない
    used = set(cfg.feeds.merkl_minutes) | {cfg.feeds.hourly_minute, cfg.feeds.daily_minute}
    assert not used & {0, 5, 15, 30, 45}
    assert cfg.feeds.database_path.name == "feeds.sqlite3" and cfg.feeds.database_path.is_absolute()
    assert chain_reads_enabled({}) and chain_reads_enabled({"FARM_RADAR_CHAIN_READS": "on"})
    assert not chain_reads_enabled({"FARM_RADAR_CHAIN_READS": "off"})
    assert not chain_reads_enabled({"FARM_RADAR_CHAIN_READS": " OFF "})


def test_api_feeds_status_and_pulse_flag(tmp_path, monkeypatch, settings):
    conn = store.connect(settings.database_path)
    aero = _src("aero_articles")
    run_source(conn, aero, FakeFetcher(texts={aero.url: AERO_MD}), settings, NOW)
    conn.close()
    cfg = dataclasses.replace(load_config(), database_path=tmp_path / "main.sqlite3", feeds=settings)
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(minutes=5))
    monkeypatch.setenv("FARM_RADAR_CHAIN_READS", "off")
    c = TestClient(api.app)
    d = c.get("/api/feeds/status").json()
    assert d["enabled"] and d["chain_reads"] is False
    assert len(d["aero"]["articles"]) == 2
    p = c.get("/api/pulse").json()
    # チェーンを読まない版では「収集が止まっています」を出さない
    assert p["chain_reads"] is False and p["stale"] is False


def test_fetcher_uses_one_second_spacing_and_one_retry():
    f = Fetcher(FeedSettings())
    assert f.http.min_interval == 1.0 and f.http.max_retries == 1
