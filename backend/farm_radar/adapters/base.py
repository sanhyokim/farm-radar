"""会場アダプターの共通インターフェース（SPEC 5.2章）。

M1の追加指示に合わせて、SPEC の形から次を拡張している:
- 読み取りは block（ブロック番号）を指定して行う。同じ回の値がすべて同じ時点のものになる。
- 結果には RPC の生の応答（raw）を持たせ、DBに保存して後から再計算できるようにする。
- プール全体の流動性と、ゲージにステークされた流動性を別の値として返す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class PoolInfo:
    pool_id: str               # "<venue_id>:<pool address>"
    venue_id: str
    address: str
    token0: str
    token1: str
    fee_tier: int | None = None
    tick_spacing: int | None = None
    gauge_address: str | None = None
    created_block: int | None = None
    is_stock_pair: bool | None = None
    has_perp: bool | None = None
    token0_symbol: str | None = None
    token1_symbol: str | None = None
    token0_decimals: int | None = None
    token1_decimals: int | None = None


@dataclass(frozen=True)
class RawCall:
    """RPC の生の要求と応答。"""
    kind: str
    request: Any
    response: Any


@dataclass(frozen=True)
class PoolState:
    pool_id: str
    block_number: int
    sqrt_price_x96: int
    tick: int
    price: float                        # token1 / token0（小数点調整済み）
    fee: int | None
    liquidity_total: int                # プール全体のレンジ内流動性
    liquidity_staked_inrange: int | None  # ゲージにステークされたレンジ内流動性（取得できなければ None）
    raw: tuple[RawCall, ...] = field(default=())
    unstaked_fee: int | None = None     # ステークしていないLPから取る手数料の割合（1e-6単位。100000 = 10%）
    # 2026-10-01 追加（オーナー決定 A）: プールのコントラクトが持っている2つのコインの量（最小単位）。
    # 緊急離脱の「プールのお金」に使う（risk_job.pool_funds）。読めなければ None
    balance0_raw: int | None = None
    balance1_raw: int | None = None


@dataclass(frozen=True)
class RewardInfo:
    pool_id: str
    block_number: int
    reward_token: str | None
    reward_rate_raw: int | None         # ゲージに設定された報酬レート（最小単位/秒）
    reward_per_day: float | None        # 今実際に出ている量（トークン数/日）。配布期間が終わっていれば 0
    epoch_end: datetime | None
    raw: tuple[RawCall, ...] = field(default=())
    # 以下は 2026-09-27 のオーナー指示で追加（報酬の毎秒量とエポックをスナップショットごとに保存する）
    block_time: datetime | None = None
    epoch_start: datetime | None = None
    period_finish: datetime | None = None   # ゲージの今の配布期間の終わり
    reward_rate_effective_raw: int | None = None  # period_finish を過ぎていれば 0
    gauge_alive: bool | None = None
    # 以下は M6（Alandale）で追加。週ごとの量で配る会場の、今のエポックの合計と、そのうち運営が手で足した分（最小単位）。
    # 運営が手で足した分は続く保証がないので、reward_rate_* には入れず、判定にも使わない（2026-09-30 オーナー条件）
    epoch_total_raw: int | None = None
    manual_raw: int | None = None


@dataclass(frozen=True)
class EpochInfo:
    length_seconds: int
    current_start: datetime
    current_end: datetime


class AdapterNotReady(Exception):
    """仕組みやアドレスが未確認のため、アダプターがまだ動かせない。"""


class VenueAdapter(Protocol):
    venue_id: str
    chain: str

    def list_pools(self, block: int) -> list[PoolInfo]: ...
    def pool_state(self, pool: PoolInfo, block: int) -> PoolState: ...
    def gauge_rewards(self, pool: PoolInfo, block: int) -> RewardInfo: ...
    def epoch_info(self, block: int) -> EpochInfo | None: ...
