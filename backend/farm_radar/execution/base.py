"""取引をする部品の共通の形（SPEC 12.1章）。

練習（ペーパートレード）はこの形の PaperExecutor で動かす。本物の取引をする LiveExecutor は作らない
（CLAUDE.md の絶対ルール。オーナーが「Phase 3aを開始」と言うまで、送信・署名・秘密鍵のコードは書かない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PositionRef:
    position_id: int


@dataclass(frozen=True)
class CloseResult:
    position_id: int
    net_usd: float


@dataclass(frozen=True)
class ClaimResult:
    token: str
    amount: float
    price_usd: float


@dataclass(frozen=True)
class SwapResult:
    token: str
    amount: float
    usd_out: float
    cost_usd: float


@dataclass(frozen=True)
class HedgeResult:
    symbol: str
    size: float
    price_usd: float


class Executor(Protocol):
    def open_position(self, pool_id: str, capital: float, lower: float, upper: float) -> PositionRef: ...
    def close_position(self, ref: PositionRef) -> CloseResult: ...
    def claim(self, ref: PositionRef) -> ClaimResult: ...
    def swap_to_usdg(self, token: str, amount: float, max_slippage: float) -> SwapResult: ...
    def hedge_adjust(self, symbol: str, target_size: float) -> HedgeResult: ...
    def hedge_close(self, symbol: str) -> HedgeResult: ...
