"""候補の会場の一覧（週1回。SPEC 5.2.2章。オーナー依頼 E1）のテスト。外部のサイトは偽物を使う。"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from farm_radar import api, discovery
from farm_radar.config import DiscoveryChain, DiscoverySettings, load_config
from farm_radar.db import database as db
from farm_radar.external.http import ExternalError

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
DAY = 86400


def _protocols(now=NOW):
    t = now.timestamp()
    return [
        # 候補: ボーナスあり
        {"slug": "alandale-v3", "name": "Alandale V3", "category": "Dexs", "chains": ["Robinhood Chain"],
         "chainTvls": {"Robinhood Chain": 1_000_000}, "change_7d": -2.0, "listedAt": t - 47 * DAY,
         "audit_links": None, "url": "https://alandale.xyz"},
        # 候補: ボーナスはないが新しい（掲載30日）
        {"slug": "fables", "name": "Fables", "category": "Dexs", "chains": ["Robinhood Chain"],
         "chainTvls": {"Robinhood Chain": 5_000_000}, "listedAt": t - 30 * DAY},
        # 外す: いま見ている会場
        {"slug": "up-v3", "name": "up v3", "category": "Dexs", "chains": ["Robinhood Chain"],
         "chainTvls": {"Robinhood Chain": 6_000_000}, "listedAt": t - 70 * DAY},
        # 外す: 預かり額が小さい
        {"slug": "tiny", "name": "Tiny", "category": "Dexs", "chains": ["Robinhood Chain"],
         "chainTvls": {"Robinhood Chain": 100_000}, "listedAt": t - 5 * DAY},
        # 外す: 古くてボーナスもない
        {"slug": "old", "name": "Old", "category": "Dexs", "chains": ["Base"],
         "chainTvls": {"Base": 9_000_000}, "listedAt": t - 900 * DAY},
        # 外す: DEX ではない
        {"slug": "lend", "name": "Lend", "category": "Lending", "chains": ["Base"],
         "chainTvls": {"Base": 9_000_000}, "listedAt": t - 5 * DAY},
        # 候補: 別のチェーン（見るチェーンに入っていないチェーンの分は外す）
        {"slug": "nest-cl", "name": "nest CL", "category": "Dexs", "chains": ["Hyperliquid L1", "Solana"],
         "chainTvls": {"Hyperliquid L1": 20_000_000, "Solana": 50_000_000}, "listedAt": t - 250 * DAY,
         "audit_links": ["https://docs.usenest.xyz/security/audits"]},
    ]


def _pools():
    return [
        {"pool": "a1", "chain": "Robinhood Chain", "project": "alandale-v3", "symbol": "WETH-USDG", "tvlUsd": 300_000,
         "apyReward": 52.0, "apyBase": 3.0, "rewardTokens": ["0xLUTE"], "count": 40},
        {"pool": "a2", "chain": "Robinhood Chain", "project": "alandale-v3", "symbol": "SPY-USDG", "tvlUsd": 100_000,
         "apyReward": 104.0, "apyBase": 1.0, "rewardTokens": ["0xLUTE"], "count": 10},
        {"pool": "a3", "chain": "Robinhood Chain", "project": "alandale-v3", "symbol": "LUTE-USDG", "tvlUsd": 10_000,
         "apyReward": 900.0, "rewardTokens": ["0xLUTE"], "count": 5},          # 小さすぎるので外す
        {"pool": "n1", "chain": "Hyperliquid L1", "project": "nest-cl", "symbol": "WHYPE-USDC", "tvlUsd": 2_000_000,
         "apyReward": 26.0, "rewardTokens": ["0xNEST"], "count": 200, "outlier": False},
        {"pool": "n2", "chain": "Solana", "project": "nest-cl", "symbol": "SOL-USDC", "tvlUsd": 2_000_000,
         "apyReward": 26.0, "count": 5},                                                  # 見ないチェーン
        {"pool": "u1", "chain": "Robinhood Chain", "project": "up-v3", "symbol": "NVDA-USDG", "tvlUsd": 200_000,
         "apyReward": 50.0, "count": 3},                                                  # いま見ている会場
        {"pool": "l1", "chain": "Base", "project": "lend", "symbol": "USDC", "tvlUsd": 2_000_000,
         "apyReward": 5.0, "count": 3},                                                   # DEX ではない
    ]


class FakeLlama:
    def __init__(self, fail_yields=False):
        self.fail_yields = fail_yields
        self.coins = None

    def protocols(self):
        return _protocols()

    def yield_pools(self):
        if self.fail_yields:
            raise ExternalError("HTTP 503")
        return _pools()

    def token_info(self, coins):
        self.coins = set(coins)
        return {"robinhood:0xlute": {"symbol": "LUTE", "price": 0.0044, "change_7d_pct": -20.0},
                "hyperliquid:0xnest": {"symbol": "NEST", "price": 0.014, "change_7d_pct": -35.0}}


class FakeGT:
    def __init__(self, dexes):
        self._dexes = dexes

    def dexes(self):
        return list(self._dexes)

    def dex_pools(self, dex_id):
        return [{"address": "0xp", "name": "NVDA / USDG", "tvl_usd": 50_000.0, "volume_24h_usd": 1_000.0,
                 "created_at": "2026-09-20T00:00:00Z"}]


@pytest.fixture
def cfg(tmp_path):
    return dataclasses.replace(load_config(), database_path=tmp_path / "t.sqlite3")


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.database_path)
    c.execute("INSERT INTO hedge_fees(hedge_id, market_id, symbol, taker_pct, maker_pct, ts) VALUES "
              "('lighter', 1, 'ETH', 0, 0, 'x'), ('lighter', 2, 'SPY', 0, 0, 'x'), ('lighter', 3, 'NVDA', 0, 0, 'x'),"
              "('lighter', 4, 'HYPE', 0, 0, 'x')")
    c.commit()
    yield c
    c.close()


def test_hedge_match_by_symbol():
    perps = {"ETH": ["Lighter"], "NVDA": ["Lighter", "Hyperliquid"]}
    assert discovery.hedge_match("WETH-USDG", perps) == {"ok": True, "stable": False, "tokens": ["ETH"],
                                                        "venues": ["Lighter"]}
    assert discovery.hedge_match("NVDA / USDG 0.05%", perps)["ok"] is True     # 手数料の "0.05%" は記号ではない
    assert discovery.hedge_match("USDG-NVDA", perps)["venues"] == ["Hyperliquid", "Lighter"]
    assert discovery.hedge_match("USDC-USDT", perps) == {"ok": False, "stable": True, "tokens": [], "venues": []}
    assert discovery.hedge_match("PEPE-WETH", perps)["ok"] is False


def test_weighted_median_ignores_one_extreme_pool():
    rows = [{"tvl_usd": 900, "apy_reward": 20.0}, {"tvl_usd": 100, "apy_reward": 50_000.0}]
    assert discovery.weighted_median(rows) == 20.0
    assert discovery.weighted_median([]) is None


def test_candidates_filter_estimate_and_order():
    s = DiscoverySettings()
    perps = {"ETH": ["Lighter"], "SPY": ["Lighter"], "HYPE": ["Lighter"]}
    cands, new_pools = discovery.build_candidates(_protocols(), _pools(), s, {"up-v3", "up-v2"}, perps, NOW)
    keys = [c["key"] for c in cands]
    # ボーナスのある会場が、週のボーナスの割合の大きい順。ボーナスのない新しい会場はその後ろ
    assert keys == ["llama:alandale-v3@Robinhood Chain", "llama:nest-cl@Hyperliquid L1", "llama:fables@Robinhood Chain"]
    a = cands[0]
    # 真ん中の値: 預かり額の重みで 52%（30万ドル分）と 104%（10万ドル分）→ 52%。週 = 52% ÷ 52 × 40万ドル = $4,000
    assert a["reward_apr_median"] == 52.0 and a["weekly_reward_usd"] == pytest.approx(4000)
    assert a["weekly_ratio_pct"] == pytest.approx(0.4)                 # 預かり額 100万ドルに対して
    assert a["reward_pools_n"] == 2 and a["top_pools"][0]["symbol"] == "SPY-USDG"
    assert a["hedge_pools"] == ["SPY-USDG", "WETH-USDG"] and a["reward_tokens"] == ["robinhood:0xlute"]
    assert a["age_days"] == 47 and a["listed_at"] == "2026-08-13"
    n = cands[1]
    assert n["tvl_usd"] == 20_000_000 and n["multi_chain"] and n["audit_links"]
    assert cands[2]["weekly_reward_usd"] is None and cands[2]["reward_pools_n"] == 0
    # 新しくボーナスが出始めたプール（30日以内の記録）。いま見ている会場と小さいプールは入らない
    assert [p["pool"] for p in new_pools] == ["a2"]


def test_collect_saves_marks_new_on_later_runs_and_alerts(conn, cfg):
    llama = FakeLlama()
    gts = {"robinhood": FakeGT([("up-v3", "Up V3"), ("alandale-cl", "Alandale (CL)")])}
    r = discovery.collect(conn, cfg, llama=llama, gts=gts, now=NOW, trigger="button")
    assert r["status"] == "ok" and r["venues"] == 3 and r["new"] == 0       # 初回は「新着」にしない
    assert llama.coins == {"robinhood:0xlute", "hyperliquid:0xnest"}
    s = discovery.summary(conn, cfg, NOW)
    assert [v["name"] for v in s["venues"]] == ["Alandale V3", "nest CL", "Fables"]
    assert s["venues"][0]["reward_change_7d_pct"] == -20.0 and s["venues"][0]["reward_token_info"][0]["symbol"] == "LUTE"
    assert s["gt_dex_count"] == 2 and s["new_dexes"] == [] and [p["pool"] for p in s["new_pools"]] == ["a2"]
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='discovery'").fetchone()[0] == 0

    # 1週間後: 新しい DEX が1つ増えた → 新着として出て、通知の箱に1件入る
    gts = {"robinhood": FakeGT([("up-v3", "Up V3"), ("alandale-cl", "Alandale (CL)"), ("newdex", "New DEX")])}
    later = NOW + timedelta(days=7)
    r = discovery.collect(conn, cfg, llama=llama, gts=gts, now=later)
    assert r["new"] == 1
    s = discovery.summary(conn, cfg, later)
    assert [d["name"] for d in s["new_dexes"]] == ["New DEX"]
    assert s["new_dexes"][0]["pools"][0]["hedge"]["tokens"] == ["NVDA"] and s["new_dexes"][0]["tvl_top_usd"] == 50_000
    a = conn.execute("SELECT * FROM alerts WHERE kind='discovery'").fetchall()
    assert len(a) == 1 and "New DEX（新しいDEX）" in a[0]["message_ja"]


def test_collect_keeps_what_it_could_read(conn, cfg):
    r = discovery.collect(conn, cfg, llama=FakeLlama(fail_yields=True), gts={"robinhood": FakeGT([])}, now=NOW)
    assert r["status"] == "partial" and "利回り" in r["errors"][0]
    s = discovery.summary(conn, cfg, NOW)
    # ボーナスが読めないので、掲載90日以内の新しい会場だけが残る（預かり額の大きい順）
    assert [v["name"] for v in s["venues"]] == ["Fables", "Alandale V3"]


def test_decisions_survive_later_runs_and_can_be_cleared(conn, cfg):
    discovery.collect(conn, cfg, llama=FakeLlama(), gts={"robinhood": FakeGT([])}, now=NOW)
    key = "llama:alandale-v3@Robinhood Chain"
    discovery.decide(conn, key, "study", NOW)
    with pytest.raises(ValueError):
        discovery.decide(conn, key, "buy", NOW)
    with pytest.raises(KeyError):
        discovery.decide(conn, "llama:nope@Base", "skip", NOW)
    # 次の週: Alandale が一覧から外れても、判断は「今週の一覧にない会場」として見える
    class Gone(FakeLlama):
        def protocols(self):
            return [p for p in _protocols() if p["slug"] != "alandale-v3"]
    discovery.collect(conn, cfg, llama=Gone(), gts={"robinhood": FakeGT([])}, now=NOW + timedelta(days=7))
    s = discovery.summary(conn, cfg, NOW + timedelta(days=7))
    assert key not in [v["key"] for v in s["venues"]]
    assert [(v["key"], v["decision"], v["not_in_latest"]) for v in s["decided_elsewhere"]] == [(key, "study", True)]
    discovery.decide(conn, key, None, NOW)
    assert discovery.summary(conn, cfg, NOW)["decided_elsewhere"] == []


def test_schedule_startup_and_refresh_guard(conn, cfg):
    s = DiscoverySettings()
    # 2026-09-29 は火曜。次は 10月5日（月）9:00 JST = 0:00 UTC
    assert discovery.next_run(NOW, s) == datetime(2026, 10, 5, 0, tzinfo=UTC)
    assert discovery.due_at_startup(conn, NOW) is True
    assert discovery.refresh_block_reason(conn, s, NOW) is None
    discovery.collect(conn, cfg, llama=FakeLlama(), gts={"robinhood": FakeGT([])}, now=NOW)
    assert discovery.due_at_startup(conn, NOW + timedelta(days=6)) is False
    assert discovery.due_at_startup(conn, NOW + timedelta(days=7)) is True
    assert "30分" in discovery.refresh_block_reason(conn, s, NOW + timedelta(minutes=10))
    assert discovery.refresh_block_reason(conn, s, NOW + timedelta(minutes=31)) is None
    conn.execute("INSERT INTO discovery_runs(started_at, trigger, status) VALUES (?, 'button', 'running')",
                 ((NOW + timedelta(hours=1)).isoformat(),))
    assert "最中" in discovery.refresh_block_reason(conn, s, NOW + timedelta(hours=1, minutes=5))
    # 止まったまま30分より古い「最中」は、止まったとみなす
    assert discovery.refresh_block_reason(conn, s, NOW + timedelta(hours=1, minutes=45)) is None


def test_config_reads_discovery(tmp_path):
    cfg = load_config()
    d = cfg.discovery
    assert d.weekday == "mon" and (d.hour_jst, d.minute_jst) == (9, 0)
    assert d.chains[0] == DiscoveryChain("Robinhood Chain", "robinhood", "robinhood")
    assert [c.name for c in d.chains] == ["Robinhood Chain", "Base", "Arbitrum", "Hyperliquid L1", "Avalanche"]
    slugs, gt = discovery.watched_listings(cfg)
    assert slugs == {"up-v3", "up-v2"} and gt["robinhood"] == {"up-v3"}


def test_api_list_decide_and_refresh(cfg, conn, monkeypatch):
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW)
    client = TestClient(api.app)
    empty = client.get("/api/discovery").json()
    assert empty["venues"] == [] and empty["last_run"] is None and "月曜" in empty["schedule_ja"]
    discovery.collect(conn, cfg, llama=FakeLlama(), gts={"robinhood": FakeGT([])}, now=NOW)
    d = client.get("/api/discovery").json()
    assert len(d["venues"]) == 3 and d["refresh_block"] is not None
    key = d["venues"][0]["key"]
    r = client.post("/api/discovery/decision", json={"key": key, "status": "hold"})
    assert r.status_code == 200 and r.json()["message"] == "「保留」にしました。"
    assert client.get("/api/discovery").json()["venues"][0]["decision"] == "hold"
    assert client.post("/api/discovery/decision", json={"key": key, "status": "x"}).status_code == 400
    assert client.post("/api/discovery/decision", json={"key": "nope", "status": "hold"}).status_code == 404
    r = client.post("/api/discovery/refresh")
    assert r.status_code == 400 and "30分" in r.json()["detail"]
    started = []
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(discovery, "run_now", lambda config, trigger: started.append(trigger))
    r = client.post("/api/discovery/refresh")
    assert r.status_code == 200
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.01)
    assert started == ["button"]
