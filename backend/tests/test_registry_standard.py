"""N1: 系統 ＞ チェーン ＞ 会場 の登録、読み方の対応表、決まった形の数字のテスト。"""

import dataclasses
import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from farm_radar import api, standard
from farm_radar.adapters.registry import READERS, build_adapter, reader_name
from farm_radar.adapters.up_robinhood import UpRobinhoodAdapter
from farm_radar.config import ConfigError, load_config, load_venue
from farm_radar.db import database as db
from farm_radar.feeds import store
from farm_radar.registry import chain_by_evm_id, chain_problems, list_chains, load_chain, overview
from farm_radar.scoring.run import score_venue

from .test_scoring_run import NOW, FakeGT, _ctx, _fill

ROOT = load_config().root


def _copy_root(tmp_path):
    """設定のファイル（config.yaml・chains・venues・tokens）だけを仮の場所に写す。"""
    for d in ("chains", "venues"):
        shutil.copytree(ROOT / d, tmp_path / d)
    for f in ROOT.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


def test_registered_chains_are_valid():
    rh, base = load_chain("robinhood"), load_chain("base")
    assert (rh["family"], rh["chain_id"]) == ("evm", 4663)
    assert (base["family"], base["chain_id"]) == ("evm", 8453)
    assert base["public_rpc"] == "https://mainnet.base.org" and base["source_url"].startswith("https://docs.base.org")
    assert {c["id"] for c in list_chains()} >= {"robinhood", "base"}
    assert load_config().chains == ("robinhood", "base")


def test_chain_problems_are_listed():
    assert chain_problems({"id": "x", "name": "X", "family": "evm", "chain_id": 1, "public_rpc": "https://x",
                           "source_url": "https://x", "checked_at": "2026-10-02"}) == []
    p = chain_problems({"id": "x", "name": "X", "family": "evm", "chain_id": "1", "source_url": "u", "checked_at": "d"})
    assert any("chain_id" in s for s in p) and any("public_rpc" in s for s in p)
    # 確かめられないチェーンは unverified: true なら RPC が無くてもよい
    assert chain_problems({"id": "x", "name": "X", "family": "evm", "chain_id": 1, "unverified": True,
                           "source_url": "u", "checked_at": "d"}) == []
    assert any("family" in s for s in chain_problems({"id": "x", "name": "X", "family": "cosmos",
                                                      "source_url": "u", "checked_at": "d"}))
    with pytest.raises(ConfigError):
        load_chain("no-such-chain")


def test_venue_points_to_the_chain_registry():
    v = load_venue("up-robinhood")
    assert v["chain"]["id"] == "robinhood" and v["chain"]["chain_id"] == 4663 and v["chain"]["family"] == "evm"
    assert v["chain"]["public_rpc"]
    assert v["mechanisms"] == ["range", "vote_weekly"] and v["reader"] == "ve33_cl_gauge"
    a = load_venue("alandale-robinhood")
    assert a["chain"]["id"] == "robinhood" and a["reader"] == "algebra_weekly_rewarder"


def test_bad_chains_setting_is_refused(tmp_path):
    root = _copy_root(tmp_path)
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    raw["chains"] = "robinhood"
    (root / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(root / "config.yaml")


def test_a_chain_is_added_by_files_only(tmp_path):
    """チェーンを足すときは、chains/<id>.yaml を置いて config.yaml の chains に1行足すだけ（コードは変えない）。"""
    root = _copy_root(tmp_path)
    (root / "chains" / "examplechain.yaml").write_text(yaml.safe_dump({
        "id": "examplechain", "name": "Example", "family": "evm", "chain_id": 999999,
        "public_rpc": "https://rpc.example", "source_url": "https://docs.example", "checked_at": "2026-10-02",
    }), encoding="utf-8")
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    raw["chains"] = [*raw["chains"], "examplechain"]
    (root / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(root / "config.yaml")
    assert cfg.chains[-1] == "examplechain"
    assert chain_by_evm_id(cfg.chains, root)[999999] == "examplechain"
    ov = overview(cfg.chains, cfg.venues, root, set(READERS))
    evm = next(f for f in ov["families"] if f["id"] == "evm")
    assert [c["id"] for c in evm["chains"]] == ["robinhood", "base", "examplechain"]
    assert ov["problems"] == []


def test_overview_reports_problems_without_stopping(tmp_path):
    root = _copy_root(tmp_path)
    v = yaml.safe_load((root / "venues" / "up-robinhood.yaml").read_text(encoding="utf-8"))
    v["mechanisms"] = ["range", "magic"]
    v["reader"] = "unknown_reader"
    (root / "venues" / "up-robinhood.yaml").write_text(yaml.safe_dump(v, allow_unicode=True), encoding="utf-8")
    ov = overview(("base",), ("up-robinhood", "alandale-robinhood"), root, set(READERS))
    text = " ".join(ov["problems"])
    assert "magic" in text and "unknown_reader" in text and "chains にありません" in text
    assert [c["id"] for f in ov["families"] for c in f["chains"]] == ["base"]


def test_reader_is_chosen_by_name_and_family_is_checked():
    v = load_venue("up-robinhood")
    assert isinstance(build_adapter(v, rpc=None), UpRobinhoodAdapter)
    legacy = {k: val for k, val in v.items() if k != "reader"}
    assert reader_name(legacy) == "ve33_cl_gauge"
    with pytest.raises(ValueError, match="solana"):
        build_adapter({**v, "chain": {**v["chain"], "family": "solana"}}, rpc=None)
    with pytest.raises(ValueError):
        build_adapter({**v, "reader": "nope"}, rpc=None)


# --- 決まった形の数字 ---------------------------------------------------------------------------

FEED_NOW = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)


def _feeds(path):
    conn = store.connect(path)
    end = int((FEED_NOW + timedelta(days=3)).timestamp())
    items = [
        store.Item("o-base", "Provide WETH/USDC", "Base", {"chain_id": 8453, "protocol": "Aerodrome", "type": "CLAMM",
                                                          "tokens": ["WETH", "USDC"], "end": end,
                                                          "deposit_url": "https://example/deposit"}),
        store.Item("o-rh", "Provide WETH/USDG", "Robinhood Chain", {"chain_id": 4663, "protocol": "Somewhere",
                                                                    "tokens": ["WETH", "USDG"]}),
        store.Item("o-arb", "Lend USDC", "Arbitrum", {"chain_id": 42161, "protocol": "Lender", "tokens": ["USDC"]}),
    ]
    ts = store.iso(FEED_NOW)
    store.upsert_items(conn, "merkl_opportunities", items, ts, baseline=True)
    old = store.iso(FEED_NOW - timedelta(minutes=15))
    conn.executemany("INSERT INTO merkl_opportunity_snaps(ts, opportunity_id, status, apr, max_apr, native_apr, tvl, "
                     "daily_rewards, live_campaigns) VALUES (?,?,?,?,?,?,?,?,?)", [
                         (old, "o-base", "LIVE", 99.0, None, None, 1.0, 1.0, 1),
                         (ts, "o-base", "LIVE", 40.0, None, None, 1_000_000.0, 1096.0, 1),
                         (ts, "o-rh", "LIVE", 10.0, None, None, 50_000.0, 13.7, 1),
                         (ts, "o-arb", "LIVE", 5.0, None, None, 2_000_000.0, 274.0, 1)])
    now_s = int(FEED_NOW.timestamp())
    conn.executemany("INSERT INTO merkl_campaigns(campaign_id, opportunity_id, end_ts, reward_symbol, first_seen, "
                     "last_seen) VALUES (?,?,?,?,?,?)", [
                         ("c1", "o-base", now_s + 86400, "AERO", ts, ts),
                         ("c2", "o-base", now_s - 86400, "OLD", ts, ts),     # 終わったキャンペーンは数えない
                         ("c3", "o-base", None, "USDC", ts, ts)])
    conn.commit()
    conn.close()


def test_merkl_rows_take_the_standard_shape(tmp_path):
    path = tmp_path / "feeds.sqlite3"
    _feeds(path)
    cfg = load_config()
    rows = standard.from_merkl(path, cfg, FEED_NOW)
    assert [r.key for r in rows] == ["o-base", "o-rh"]           # 登録したチェーンだけ・預かり額の大きい順
    b = rows[0]
    assert (b.source, b.family, b.chain, b.chain_name, b.evm_chain_id) == ("merkl", "evm", "base", "Base", 8453)
    assert b.tvl_usd == 1_000_000.0 and b.bonus_usd_per_day == 1096.0 and b.shown_apr_pct == 40.0
    assert b.bonus_token == "AERO,USDC" and b.mechanisms == ("later",) and b.tokens == ("WETH", "USDC")
    assert b.days_left == pytest.approx(3.0) and b.url == "https://example/deposit"
    assert rows[1].chain == "robinhood" and rows[1].ends_at is None and rows[1].days_left is None
    every = standard.from_merkl(path, cfg, FEED_NOW, registered_only=False)
    arb = next(r for r in every if r.key == "o-arb")
    assert arb.chain is None and arb.chain_name == "Arbitrum" and arb.evm_chain_id == 42161
    assert standard.from_merkl(tmp_path / "missing.sqlite3", cfg, FEED_NOW) == []


def test_own_scores_take_the_standard_shape(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    _fill(conn, 24 * 8)
    score_venue(conn, _ctx(FakeGT()), now=NOW)
    rows = standard.from_own(conn, load_config(), NOW)
    assert {r.key for r in rows} == {"up-robinhood:p-weth", "up-robinhood:p-up", "up-robinhood:p-nvda"}
    r = next(x for x in rows if x.key == "up-robinhood:p-weth")
    assert (r.source, r.chain, r.evm_chain_id, r.venue) == ("chain", "robinhood", 4663, "up-robinhood")
    assert r.name == "WETH/USDG" and r.tokens == ("WETH", "USDG") and r.mechanisms == ("range", "vote_weekly")
    sc = conn.execute("SELECT tvl_usd, details_json FROM scores WHERE pool_id=? ORDER BY ts DESC LIMIT 1",
                      (r.key,)).fetchone()
    day = json.loads(sc["details_json"])["inputs"]["reward_usd_day"]
    assert r.bonus_usd_per_day == pytest.approx(day) and r.tvl_usd == pytest.approx(sc["tvl_usd"])
    assert r.shown_apr_pct == pytest.approx(day * 365 / sc["tvl_usd"] * 100)
    assert r.ends_at is not None and 0 <= r.days_left <= 7          # 次の週の切り替え
    assert any(n.startswith("C4") for n in r.notes)
    conn.close()


def test_api_registry_and_standard(tmp_path, monkeypatch):
    feeds_path = tmp_path / "feeds.sqlite3"
    _feeds(feeds_path)
    base = load_config()
    cfg = dataclasses.replace(base, database_path=tmp_path / "main.sqlite3",
                              feeds=dataclasses.replace(base.feeds, database_path=feeds_path))
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: FEED_NOW)
    c = TestClient(api.app)
    reg = c.get("/api/registry").json()
    assert reg["problems"] == []
    chains = {ch["id"]: ch for f in reg["families"] for ch in f["chains"]}
    assert [v["id"] for v in chains["robinhood"]["venues"]] == ["up-robinhood", "alandale-robinhood"]
    assert chains["base"]["venues"] == [] and "later" in reg["mechanisms"]
    d = c.get("/api/standard").json()
    assert d["total"] == 2 and d["counts"] == {"merkl:base": 1, "merkl:robinhood": 1}
    assert c.get("/api/standard?all_chains=true").json()["counts"]["merkl:-"] == 1
    assert [i["key"] for i in c.get("/api/standard?chain=base").json()["items"]] == ["o-base"]
    assert c.get("/api/standard?limit=1").json()["total"] == 2
    assert len(c.get("/api/standard?limit=1").json()["items"]) == 1
