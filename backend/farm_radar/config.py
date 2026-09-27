"""config.yaml と venues/*.yaml、.env の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 12.3章: 今の段階で実装するモードはこの2つだけ。
ALLOWED_MODES = ("observe", "paper")

# config.yaml と venues/ がある場所。Docker では環境変数 FARM_RADAR_ROOT で指定する。
REPO_ROOT = Path(os.environ.get("FARM_RADAR_ROOT") or Path(__file__).resolve().parents[2])


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RpcSettings:
    max_retries: int = 3
    backoff_seconds: float = 1.0
    timeout_seconds: float = 15.0
    extra_urls: tuple[str, ...] = ()
    latest_cache_seconds: float = 5.0


@dataclass(frozen=True)
class Config:
    mode: str
    database_path: Path
    snapshot_minutes: int
    rpc: RpcSettings
    venues: tuple[str, ...]
    stale_after_minutes: int
    limits: dict[str, Any] = field(default_factory=dict)
    root: Path = REPO_ROOT


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    path = path or REPO_ROOT / "config.yaml"
    env = os.environ if env is None else env
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = path.resolve().parent

    mode = raw.get("mode")
    if mode not in ALLOWED_MODES:
        raise ConfigError(
            f"config.yaml の mode={mode!r} は使えません。使えるのは {', '.join(ALLOWED_MODES)} だけです。"
        )

    snapshot_minutes = int(raw.get("schedule", {}).get("snapshot_minutes", 15))
    if snapshot_minutes <= 0:
        raise ConfigError("schedule.snapshot_minutes は1以上にしてください。")

    rpc_raw = raw.get("rpc", {}) or {}
    extra = list(rpc_raw.get("extra_urls") or [])
    extra += [u.strip() for u in env.get("EXTRA_RPC_URLS", "").split(",") if u.strip()]
    rpc = RpcSettings(
        max_retries=int(rpc_raw.get("max_retries", 3)),
        backoff_seconds=float(rpc_raw.get("backoff_seconds", 1.0)),
        timeout_seconds=float(rpc_raw.get("timeout_seconds", 15)),
        extra_urls=tuple(extra),
        latest_cache_seconds=float(rpc_raw.get("latest_cache_seconds", 5)),
    )

    db_path = Path(raw.get("database", {}).get("path", "data/farm_radar.sqlite3"))
    if not db_path.is_absolute():
        db_path = root / db_path

    return Config(
        mode=mode,
        database_path=db_path,
        snapshot_minutes=snapshot_minutes,
        rpc=rpc,
        venues=tuple(raw.get("venues") or ()),
        stale_after_minutes=int(raw.get("stale_after_minutes", 45)),
        limits=dict(raw.get("limits") or {}),
        root=root,
    )


def load_venue(venue_id: str, root: Path = REPO_ROOT) -> dict[str, Any]:
    path = root / "venues" / f"{venue_id}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("id") != venue_id:
        raise ConfigError(f"{path} の id が {venue_id} と一致しません。")
    return data


def contract_address(venue: dict[str, Any], name: str) -> str | None:
    """確認済みのアドレスだけを返す。未確認（unverified: true）なら None。"""
    entry = (venue.get("contracts") or {}).get(name) or {}
    if entry.get("unverified", True) or not entry.get("address"):
        return None
    return entry["address"]
