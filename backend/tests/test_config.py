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
    for name in venue["contracts"]:
        assert contract_address(venue, name) is None
    fake = {"contracts": {"x": {"address": "0xabc", "unverified": True}}}
    assert contract_address(fake, "x") is None
    fake["contracts"]["x"]["unverified"] = False
    assert contract_address(fake, "x") == "0xabc"


def test_venue_chain_facts_have_source():
    chain = load_venue("up-robinhood")["chain"]
    assert chain["chain_id"] == 4663
    assert chain["source_url"].startswith("https://docs.robinhood.com/")
    assert chain["checked_at"]


def test_secrets_and_data_are_gitignored():
    import subprocess
    for path in [".env", ".env.local", "data/farm_radar.sqlite3", "data/farm_radar.sqlite3-wal"]:
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
        assert r.returncode == 0, f"{path} が .gitignore で除外されていません"
    r = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=REPO_ROOT)
    assert r.returncode == 1  # 見本は残す
