from pathlib import Path

import pytest

from farm_radar.config import ConfigError, contract_address, load_config, load_venue
from farm_radar.config import REPO_ROOT


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_repo_config_loads():
    cfg = load_config(REPO_ROOT / "config.yaml", env={})
    assert cfg.mode == "observe"
    assert cfg.snapshot_minutes == 15
    assert cfg.venues == ("up-robinhood",)


@pytest.mark.parametrize("mode", ["dryrun", "exit_only", "full", None, "live"])
def test_other_modes_stop_startup(tmp_path, mode):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"mode: {mode}\n"), env={})


def test_paper_mode_allowed(tmp_path):
    assert load_config(write(tmp_path, "mode: paper\n"), env={}).mode == "paper"


def test_extra_rpc_from_env(tmp_path):
    cfg = load_config(write(tmp_path, "mode: observe\nrpc:\n  extra_urls: [https://a]\n"),
                      env={"EXTRA_RPC_URLS": "https://b, https://c"})
    assert cfg.rpc.extra_urls == ("https://a", "https://b", "https://c")


def test_unverified_address_is_never_returned():
    venue = load_venue("up-robinhood")
    for name, entry in venue["contracts"].items():
        if entry.get("unverified", True):
            assert contract_address(venue, name) is None
        else:
            # 確認済みのアドレスには、確認日と根拠が必ず書かれていること
            assert contract_address(venue, name) == entry["address"]
            assert entry["checked_at"] and entry["evidence"]
    assert contract_address(venue, "gauge_cap_controller") is None
    fake = {"contracts": {"x": {"address": "0xabc", "unverified": True}}}
    assert contract_address(fake, "x") is None
    fake["contracts"]["x"]["unverified"] = False
    assert contract_address(fake, "x") == "0xabc"


def test_venue_chain_facts_have_source():
    chain = load_venue("up-robinhood")["chain"]
    assert chain["chain_id"] == 4663
    assert chain["source_url"].startswith("https://docs.robinhood.com/")
    assert chain["checked_at"]


def test_perp_fee_in_config_matches_recorded_source():
    import yaml
    lighter = yaml.safe_load((REPO_ROOT / "venues" / "lighter.yaml").read_text(encoding="utf-8"))
    fees = lighter["fees"]
    assert all(x["url"].startswith("https://") and x["checked_at"] for x in fees["sources"])
    s = load_config(REPO_ROOT / "config.yaml", env={}).scoring
    assert s.hedge_taker_fee_pct == fees["standard_account"]["taker_pct"]


def test_secrets_and_data_are_gitignored():
    import subprocess
    for path in [".env", ".env.local", "data/farm_radar.sqlite3", "data/farm_radar.sqlite3-wal"]:
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
        assert r.returncode == 0, f"{path} が .gitignore で除外されていません"
    r = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=REPO_ROOT)
    assert r.returncode == 1  # 見本は残す


def test_scoring_settings_from_repo_config():
    s = load_config(REPO_ROOT / "config.yaml", env={}).scoring
    assert s.rebalance_wait_minutes == 15          # 2026-09-29 オーナー決定の初期値
    assert (s.allocation_lp, s.allocation_hedge_margin, s.allocation_reserve) == (0.55, 0.40, 0.05)
    assert s.ranges_pct == (0.5, 1, 2, 3, 5, 10, 15)
    assert (s.green_min_pct, s.yellow_min_pct) == (0.30, 0.10)
    # 2026-09-29 オーナー指示: 両替のずれの初期値、高すぎる日利の警告
    assert s.slippage_trade_usd is None
    assert (s.slippage_fallback_stable_stock_pct, s.slippage_fallback_other_pct) == (0.1, 1.0)
    assert s.too_high_pct == 5.0
    assert s.volume_suspicious_tvl_multiple == 10


def test_allocation_must_sum_to_one(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "mode: observe\nscoring:\n  allocation: {lp: 0.6, hedge_margin: 0.4, reserve: 0.05}\n"),
                    env={})


def test_token_book_from_repo():
    from farm_radar.tokens import load_tokens
    book = load_tokens("robinhood")
    assert book.is_stable("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")          # USDG
    assert book.wrapped_native == "0x0bd7d308f8e1639fab988df18a8011f41eacad73"   # WETH
    assert book.perp_for(book.wrapped_native).symbol == "ETH"
    assert book.is_stock("0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")            # NVDA
    # 記号が同じだけの偽物（株トークンの一覧にないアドレス）は株として扱わない
    assert not book.is_stock("0xca9c78dd337a67f6e0077f65f5e9218719d30edf")        # プールにある "NET"
