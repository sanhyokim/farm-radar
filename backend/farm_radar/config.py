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
class ScoringSettings:
    """スコア計算と判定の設定（SPEC 3.2章・4章）。config.yaml の scoring / signal から読む。"""
    every_minutes: int = 60
    total_capital_usd: float = 1000.0
    allocation_lp: float = 0.55
    allocation_hedge_margin: float = 0.40
    allocation_reserve: float = 0.05
    ranges_pct: tuple[float, ...] = (0.5, 1, 2, 3, 5, 10, 15)
    rebalance_wait_minutes: float = 15.0
    gas_units_per_tx: int = 600_000
    swap_ratio: float = 0.5
    slippage_trade_usd: float | None = None      # 両替のずれを見積もる金額（None = LPに置く額）
    slippage_fallback_stable_stock_pct: float = 0.1
    slippage_fallback_other_pct: float = 1.0
    volume_cap_tvl_multiple: float = 10.0          # 1日の取引量がTVLのこの倍を超えたら警告し、手数料はこの倍までで計算
    hedge_taker_fee_pct: float = 0.0
    count_funding_income: bool = False
    volatility_days: float = 7.0
    external_refresh_hours: float = 3.0
    green_min_pct: float = 0.30
    yellow_min_pct: float = 0.10
    green_min_tvl_usd: float = 200_000.0
    reward_token_7d_major_pct: float = -30.0
    too_high_pct: float = 5.0



def _scoring(raw: dict[str, Any]) -> ScoringSettings:
    sc = raw.get("scoring") or {}
    sig = raw.get("signal") or {}
    alloc = sc.get("allocation") or {}
    hedge = sc.get("hedge") or {}
    slip = sc.get("slippage") or {}
    d = ScoringSettings()
    out = ScoringSettings(
        every_minutes=int(sc.get("every_minutes", d.every_minutes)),
        total_capital_usd=float(sc.get("total_capital_usd", d.total_capital_usd)),
        allocation_lp=float(alloc.get("lp", d.allocation_lp)),
        allocation_hedge_margin=float(alloc.get("hedge_margin", d.allocation_hedge_margin)),
        allocation_reserve=float(alloc.get("reserve", d.allocation_reserve)),
        ranges_pct=tuple(float(x) for x in sc.get("ranges_pct", d.ranges_pct)),
        rebalance_wait_minutes=float(sc.get("rebalance_wait_minutes", d.rebalance_wait_minutes)),
        gas_units_per_tx=int(sc.get("gas_units_per_tx", d.gas_units_per_tx)),
        swap_ratio=float(sc.get("swap_ratio", d.swap_ratio)),
        slippage_trade_usd=(float(slip["trade_usd"]) if slip.get("trade_usd") is not None else None),
        slippage_fallback_stable_stock_pct=float(
            slip.get("fallback_stable_stock_pct", d.slippage_fallback_stable_stock_pct)),
        slippage_fallback_other_pct=float(slip.get("fallback_other_pct", d.slippage_fallback_other_pct)),
        volume_cap_tvl_multiple=float(sc.get("volume_cap_tvl_multiple", d.volume_cap_tvl_multiple)),
        hedge_taker_fee_pct=float(hedge.get("taker_fee_pct", d.hedge_taker_fee_pct)),
        count_funding_income=bool(hedge.get("count_funding_income", d.count_funding_income)),
        volatility_days=float(sc.get("volatility_days", d.volatility_days)),
        external_refresh_hours=float(sc.get("external_refresh_hours", d.external_refresh_hours)),
        green_min_pct=float(sig.get("green_min_pct", d.green_min_pct)),
        yellow_min_pct=float(sig.get("yellow_min_pct", d.yellow_min_pct)),
        green_min_tvl_usd=float(sig.get("green_min_tvl_usd", d.green_min_tvl_usd)),
        reward_token_7d_major_pct=float(sig.get("reward_token_7d_major_pct", d.reward_token_7d_major_pct)),
        too_high_pct=float(sig.get("too_high_pct", d.too_high_pct)),
    )
    total = out.allocation_lp + out.allocation_hedge_margin + out.allocation_reserve
    if abs(total - 1) > 1e-6:
        raise ConfigError(f"scoring.allocation の合計が1になっていません（今は {total}）。")
    if out.every_minutes <= 0 or not out.ranges_pct or min(out.ranges_pct) <= 0 or max(out.ranges_pct) >= 100:
        raise ConfigError("scoring の every_minutes / ranges_pct を確認してください。")
    return out


@dataclass(frozen=True)
class Config:
    mode: str
    database_path: Path
    snapshot_minutes: int
    rpc: RpcSettings
    venues: tuple[str, ...]
    stale_after_minutes: int
    limits: dict[str, Any] = field(default_factory=dict)
    epoch_fresh_minutes: int = 120          # エポック切り替えからこの分数までは「エポック更新直後」の印を付ける
    reward_drop_alert_pct: float = 30.0     # エポックの途中で報酬の毎秒量がこの%以上減ったら通知
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
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
        epoch_fresh_minutes=int((raw.get("rewards") or {}).get("epoch_fresh_minutes", 120)),
        reward_drop_alert_pct=float((raw.get("alerts") or {}).get("reward_rate_drop_pct", 30)),
        scoring=_scoring(raw),
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
