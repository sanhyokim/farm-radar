"""up.（Robinhood Chain）のアダプター。読み取り専用。

仕組みの根拠は venues/up-robinhood.yaml（2026-09-27 確認）。要点:
- up. は Aerodrome Slipstream（集中流動性=CL）の派生。
- プール一覧: CLファクトリーの PoolCreated イベントから作る。ゲージの有無は Voter.pools(i) / gauges(pool) で調べる。
- ステークしたLPは取引手数料を受け取らない（手数料は投票者へ）。報酬はレンジ内のステーク流動性に比例する。
- エポックは7日、木曜 00:00 UTC に切り替わる。
- 独自の「ゲージ報酬の上限」があるので、報酬は票の割合から計算せず、ゲージの実際の rewardRate を読む。

1回の収集では、prefetch() で全プールの読み取りを Multicall でまとめて行い、
pool_state() / gauge_rewards() はその結果を返すだけにする（RPCの回数制限に配慮）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import contract_address
from ..rpc.abi import decode_result, encode_call, topic
from ..rpc.client import RpcClient
from ..rpc.multicall import Call, Multicall
from .base import AdapterNotReady, EpochInfo, PoolInfo, PoolState, RawCall, RewardInfo

log = logging.getLogger(__name__)

POOL_CREATED = "PoolCreated(address,address,int24,address)"
ZERO = "0x0000000000000000000000000000000000000000"

# このアダプターが動くのに必要な、確認済みのコントラクト
REQUIRED_CONTRACTS = ("factory_v3", "voter", "reward_token")

# (呼び出し名, シグネチャ, 戻り値の型)
POOL_CALLS = (
    ("slot0", "slot0()", ["uint160", "int24", "uint16", "uint16", "uint16", "bool"]),
    ("liquidity", "liquidity()", ["uint128"]),
    ("staked_liquidity", "stakedLiquidity()", ["uint128"]),
    ("fee", "fee()", ["uint24"]),
    ("unstaked_fee", "unstakedFee()", ["uint24"]),
)
GAUGE_CALLS = (
    ("reward_rate", "rewardRate()", ["uint256"]),
    ("period_finish", "periodFinish()", ["uint256"]),
)


@dataclass
class _Prefetched:
    block: int
    block_time: datetime
    values: dict[tuple[str, str], Any]   # (プールのアドレス, 呼び出し名) → 値（失敗なら None）


def _addr(word: str) -> str:
    return "0x" + word[-40:].lower()


def _int24(word: str) -> int:
    v = int(word, 16) & 0xFFFFFF
    return v - (1 << 24) if v >= 1 << 23 else v


def price_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """sqrtPriceX96 から「token0 1個あたりの token1 の量」を計算する（小数点の桁を調整）。"""
    return (sqrt_price_x96 / 2**96) ** 2 * 10 ** (dec0 - dec1)


class UpRobinhoodAdapter:
    venue_id = "up-robinhood"
    chain = "robinhood"

    def __init__(self, venue: dict[str, Any], rpc: RpcClient):
        self.venue = venue
        self.rpc = rpc
        self.multicall = Multicall(rpc, contract_address(venue, "multicall3"))
        coll = venue.get("collection") or {}
        self.scope = coll.get("snapshot_scope", "gauged")
        self.log_chunk = int(coll.get("log_chunk_blocks", 5_000_000))
        mech = venue.get("mechanics") or {}
        self.epoch_seconds = int((mech.get("epoch") or {}).get("length_seconds") or 0)
        self.reward_decimals = 18

        # 実行中に覚えておく情報（再起動したらイベントから作り直す。1回あたり十数回のRPCで済む）
        self._scanned_to: int | None = None
        self._cl_pools: dict[str, dict[str, Any]] = {}   # プール → {token0, token1, tick_spacing, created_block}
        self._voter_len = 0
        self._gauges: dict[str, str] = {}               # プール → ゲージ
        self._tokens: dict[str, tuple[str | None, int | None]] = {}  # トークン → (記号, 桁数)
        self._pre: _Prefetched | None = None

    # --- 確認済みかどうか ---
    def _require_verified(self) -> None:
        missing = [n for n in REQUIRED_CONTRACTS if contract_address(self.venue, n) is None]
        missing += [n for n, m in (self.venue.get("mechanics") or {}).items()
                    if m.get("unverified", True) and not m.get("owner_acknowledged")]
        if not self.epoch_seconds:
            missing.append("epoch.length_seconds")
        if missing:
            raise AdapterNotReady("未確認の項目があるため収集できません: " + ", ".join(missing))

    # --- プール一覧 ---
    def list_pools(self, block: int) -> list[PoolInfo]:
        self._require_verified()
        self._scan_pool_created(block)
        self._refresh_gauges(block)
        if self.scope == "all":
            targets = list(self._cl_pools)
        else:
            targets = [p for p in self._cl_pools if p in self._gauges]
        self._load_tokens({t for p in targets for t in (self._cl_pools[p]["token0"], self._cl_pools[p]["token1"])}, block)
        log.info("up pools listed", extra={"data": {
            "cl_pools": len(self._cl_pools), "gauged_cl_pools": sum(p in self._gauges for p in self._cl_pools),
            "targets": len(targets), "scope": self.scope,
        }})
        return [self._pool_info(p) for p in sorted(targets)]

    def _pool_info(self, pool: str) -> PoolInfo:
        meta = self._cl_pools[pool]
        s0, d0 = self._tokens.get(meta["token0"], (None, None))
        s1, d1 = self._tokens.get(meta["token1"], (None, None))
        return PoolInfo(
            pool_id=f"{self.venue_id}:{pool}", venue_id=self.venue_id, address=pool,
            token0=meta["token0"], token1=meta["token1"], tick_spacing=meta["tick_spacing"],
            gauge_address=self._gauges.get(pool), created_block=meta["created_block"],
            token0_symbol=s0, token1_symbol=s1, token0_decimals=d0, token1_decimals=d1,
        )

    def _scan_pool_created(self, block: int) -> None:
        """ファクトリーの PoolCreated イベントを、前回読んだ続きから読む。"""
        factory = contract_address(self.venue, "factory_v3")
        start = self._scanned_to + 1 if self._scanned_to is not None else int(
            self.venue["contracts"]["factory_v3"].get("deploy_block") or 0)
        if start > block:
            return
        logs = self.rpc.get_logs(factory, [topic(POOL_CREATED)], start, block, chunk=self.log_chunk)
        for lg in logs:
            pool = _addr(lg["data"])
            self._cl_pools[pool] = {
                "token0": _addr(lg["topics"][1]), "token1": _addr(lg["topics"][2]),
                "tick_spacing": _int24(lg["topics"][3]), "created_block": int(lg["blockNumber"], 16),
            }
        self._scanned_to = block

    def _refresh_gauges(self, block: int) -> None:
        """Voter に登録されたプールが増えていたら、そのゲージを読む。"""
        voter = contract_address(self.venue, "voter")
        (n,) = decode_result(["uint256"], self.rpc.eth_call(voter, "0x" + encode_call("length()").hex(), block))
        if n <= self._voter_len:
            return
        idx = range(self._voter_len, n)
        res, _ = self.multicall.call([Call(voter, encode_call("pools(uint256)", ["uint256"], [i])) for i in idx], block)
        pools = [decode_result(["address"], r.data)[0].lower() for r in res]
        res, _ = self.multicall.call([Call(voter, encode_call("gauges(address)", ["address"], [p])) for p in pools], block)
        for p, r in zip(pools, res):
            g = decode_result(["address"], r.data)[0].lower()
            if g != ZERO and p in self._cl_pools:   # v2 プールのゲージは今は対象外
                self._gauges[p] = g
        self._voter_len = n

    def _load_tokens(self, tokens: set[str], block: int) -> None:
        todo = sorted(t for t in tokens if t not in self._tokens)
        if not todo:
            return
        calls = []
        for t in todo:
            calls += [Call(t, encode_call("symbol()")), Call(t, encode_call("decimals()"))]
        res, _ = self.multicall.call(calls, block)
        for i, t in enumerate(todo):
            sym = _safe(lambda r=res[2 * i]: decode_result(["string"], r.data)[0]) if res[2 * i].success else None
            dec = _safe(lambda r=res[2 * i + 1]: int(decode_result(["uint8"], r.data)[0])) if res[2 * i + 1].success else None
            self._tokens[t] = (sym, dec)

    # --- まとめ読み ---
    def prefetch(self, pools: list[PoolInfo], block: int) -> tuple[RawCall, ...]:
        """全プールとゲージの値を Multicall でまとめて読む。生データを返す。"""
        self._require_verified()
        voter = contract_address(self.venue, "voter")
        calls: list[Call] = []
        keys: list[tuple[str, str, list[str]]] = []
        for p in pools:
            for name, sig, types in POOL_CALLS:
                calls.append(Call(p.address, encode_call(sig)))
                keys.append((p.address, name, types))
            # プールが持っているコインの量（緊急離脱の「プールのお金」。2026-10-01 オーナー決定 A）
            for name, tok in (("balance0", p.token0), ("balance1", p.token1)):
                calls.append(Call(tok, encode_call("balanceOf(address)", ["address"], [p.address])))
                keys.append((p.address, name, ["uint256"]))
            if p.gauge_address:
                for name, sig, types in GAUGE_CALLS:
                    calls.append(Call(p.gauge_address, encode_call(sig)))
                    keys.append((p.address, name, types))
                calls.append(Call(voter, encode_call("isAlive(address)", ["address"], [p.gauge_address])))
                keys.append((p.address, "alive", ["bool"]))
        res, raw = self.multicall.call(calls, block)
        values: dict[tuple[str, str], Any] = {}
        for (addr, name, types), r in zip(keys, res):
            values[(addr, name)] = _safe(lambda r=r, types=types: decode_result(types, r.data)) if r.success else None
        blk = self.rpc.request("eth_getBlockByNumber", [hex(block), False])
        block_time = datetime.fromtimestamp(int(blk["timestamp"], 16), UTC)
        self._pre = _Prefetched(block, block_time, values)
        return (RawCall("multicall_batch", {"block": block, "keys": [f"{a}:{n}" for a, n, _ in keys]}, raw),
                RawCall("block", {"block": block}, {"timestamp": blk["timestamp"]}))

    def _ensure(self, pool: PoolInfo, block: int) -> _Prefetched:
        if self._pre is None or self._pre.block != block or (pool.address, "slot0") not in self._pre.values:
            self.prefetch([pool], block)
        return self._pre

    def _get(self, pool: PoolInfo, block: int, name: str) -> Any:
        return self._ensure(pool, block).values.get((pool.address, name))

    # --- 状態 ---
    def pool_state(self, pool: PoolInfo, block: int) -> PoolState:
        slot0 = self._get(pool, block, "slot0")
        liq = self._get(pool, block, "liquidity")
        if slot0 is None or liq is None:
            raise RuntimeError("slot0 / liquidity を読めませんでした")
        sqrt_p, tick = int(slot0[0]), int(slot0[1])
        staked = self._get(pool, block, "staked_liquidity")
        fee = self._get(pool, block, "fee")
        unstaked = self._get(pool, block, "unstaked_fee")
        bal0, bal1 = self._get(pool, block, "balance0"), self._get(pool, block, "balance1")
        d0, d1 = pool.token0_decimals, pool.token1_decimals
        price = price_from_sqrt(sqrt_p, d0, d1) if d0 is not None and d1 is not None else float("nan")
        return PoolState(
            pool_id=pool.pool_id, block_number=block, sqrt_price_x96=sqrt_p, tick=tick, price=price,
            fee=int(fee[0]) if fee else None, liquidity_total=int(liq[0]),
            liquidity_staked_inrange=int(staked[0]) if staked else None,
            unstaked_fee=int(unstaked[0]) if unstaked else None,
            balance0_raw=int(bal0[0]) if bal0 else None, balance1_raw=int(bal1[0]) if bal1 else None,
        )

    def gauge_rewards(self, pool: PoolInfo, block: int) -> RewardInfo:
        pre = self._ensure(pool, block)
        epoch = self._epoch_bounds(pre.block_time)
        if not pool.gauge_address:
            return RewardInfo(pool.pool_id, block, None, None, None, epoch.current_end,
                              block_time=pre.block_time, epoch_start=epoch.current_start)
        rate = self._get(pool, block, "reward_rate")
        finish = self._get(pool, block, "period_finish")
        alive = self._get(pool, block, "alive")
        if rate is None or finish is None:
            raise RuntimeError("ゲージの rewardRate / periodFinish を読めませんでした")
        rate_raw, finish_ts = int(rate[0]), int(finish[0])
        now_ts = int(pre.block_time.timestamp())
        effective = rate_raw if now_ts < finish_ts and (alive is None or alive[0]) else 0
        return RewardInfo(
            pool_id=pool.pool_id, block_number=block,
            reward_token=contract_address(self.venue, "reward_token").lower(),
            reward_rate_raw=rate_raw,
            reward_per_day=effective * 86400 / 10**self.reward_decimals,
            epoch_end=epoch.current_end,
            block_time=pre.block_time, epoch_start=epoch.current_start,
            period_finish=datetime.fromtimestamp(finish_ts, UTC) if finish_ts else None,
            reward_rate_effective_raw=effective,
            gauge_alive=bool(alive[0]) if alive else None,
        )

    def epoch_info(self, block: int) -> EpochInfo | None:
        self._require_verified()
        blk = self.rpc.request("eth_getBlockByNumber", [hex(block), False])
        return self._epoch_bounds(datetime.fromtimestamp(int(blk["timestamp"], 16), UTC))

    def _epoch_bounds(self, block_time: datetime) -> EpochInfo:
        """エポックの始まりと終わり（ProtocolTimeLibrary.epochStart と同じ計算: t − t mod 7日）。"""
        ts = int(block_time.timestamp())
        start = ts - ts % self.epoch_seconds
        return EpochInfo(self.epoch_seconds, datetime.fromtimestamp(start, UTC),
                         datetime.fromtimestamp(start + self.epoch_seconds, UTC))


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
