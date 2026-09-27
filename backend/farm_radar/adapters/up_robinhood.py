"""up.（Robinhood Chain）のアダプター。

2026-09-27: アドレスと3点（手数料の行き先・レンジ内ステーク比例・エポック）は確認済み（venues/up-robinhood.yaml）。
ただし独自の「ゲージ報酬の上限」（gauge_emission_cap）の計算が未確認で、純日利の式にも関わるため、
オーナーの確認が済むまで AdapterNotReady を出して収集を「skipped」として記録する。
"""

from __future__ import annotations

from typing import Any

from ..config import contract_address
from ..rpc.client import RpcClient
from ..rpc.multicall import Multicall
from .base import AdapterNotReady, EpochInfo, PoolInfo, PoolState, RewardInfo


class UpRobinhoodAdapter:
    venue_id = "up-robinhood"
    chain = "robinhood"

    def __init__(self, venue: dict[str, Any], rpc: RpcClient):
        self.venue = venue
        self.rpc = rpc
        self.multicall = Multicall(rpc, contract_address(venue, "multicall3"))

    def _require_verified(self) -> None:
        missing = [name for name, c in (self.venue.get("contracts") or {}).items()
                   if c.get("unverified", True) and name != "multicall3"]
        missing += [name for name, m in (self.venue.get("mechanics") or {}).items()
                    if m.get("unverified", True)]
        if missing:
            raise AdapterNotReady("未確認の項目があるため収集できません: " + ", ".join(missing))

    def list_pools(self, block: int) -> list[PoolInfo]:
        self._require_verified()
        raise AdapterNotReady("up. のプール一覧の取得は、ファクトリーの確認後に実装します。")

    def pool_state(self, pool: PoolInfo, block: int) -> PoolState:
        self._require_verified()
        raise AdapterNotReady("未実装")

    def gauge_rewards(self, pool: PoolInfo, block: int) -> RewardInfo:
        self._require_verified()
        raise AdapterNotReady("未実装")

    def epoch_info(self, block: int) -> EpochInfo | None:
        self._require_verified()
        raise AdapterNotReady("未実装")
