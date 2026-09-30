"""Alandale（Robinhood Chain）のアダプター。読み取り専用。M6 で追加した、観察だけの会場。

仕組みの根拠は venues/alandale-robinhood.yaml（2026-09-30 確認）。up. との違い:
- プールは Algebra Integral 1.0 の集中流動性（CL）。価格は globalState() から読む（slot0() はない）。
- ステークはない。ボーナス（LUTE）は、レンジ内の流動性に応じて運営のサーバーが分ける（チェーンでは確かめられない）。
  なので「ボーナスを分け合う流動性」= プールのレンジ内の流動性すべて（liquidity()）とする。
- LP は取引手数料を受け取らない（communityFee = 100%）。unstaked_fee に 100% を入れて、手数料の収入が0になるようにする。
- ゲージの rewardRate() は 0。プールの週の量は GaugeRewarder.rewardPerGaugePerEpoch(Minter.active_period(), ゲージ) で読む。
  このうち NotifyReward の caller がゲージ自身のものが「いつもの分」、ほかは「運営が手で足した分」。
  1秒あたりのボーナスは「いつもの分 ÷ 7日」（7日に均等に配る前提。会場の警告に書いてある）。手で足した分は判定に入れない。

1回の収集では prefetch() で全プールの読み取りを Multicall でまとめ、NotifyReward のログは前回の続きだけ読む
（公開RPCの回数制限に配慮。up. の収集を優先するので、この会場は後から読む）。
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
from .up_robinhood import price_from_sqrt

log = logging.getLogger(__name__)

ZERO = "0x0000000000000000000000000000000000000000"
NOTIFY_REWARD = topic("NotifyReward(address,address,uint256,uint256)")
COMMUNITY_FEE_DENOMINATOR = 1000     # Algebra の communityFee は 1000分の1（1000 = 100%）

# このアダプターが動くのに必要な、確認済みのコントラクト
REQUIRED_CONTRACTS = ("voter", "gauge_rewarder", "minter", "reward_token")

GLOBAL_STATE = ["uint160", "int24", "uint16", "uint8", "uint16", "bool"]  # price, tick, lastFee, pluginConfig, communityFee, unlocked


@dataclass
class _Prefetched:
    block: int
    block_time: datetime
    epoch: int                            # Minter.active_period()（今のエポックの始まり。Unix 時刻）
    values: dict[tuple[str, str], Any]    # (プールのアドレス, 呼び出し名) → 値（失敗なら None）


def _addr(word: str) -> str:
    return "0x" + word[-40:].lower()


class AlandaleRobinhoodAdapter:
    venue_id = "alandale-robinhood"
    chain = "robinhood"

    def __init__(self, venue: dict[str, Any], rpc: RpcClient):
        self.venue = venue
        self.rpc = rpc
        self.multicall = Multicall(rpc, contract_address(venue, "multicall3"))
        coll = venue.get("collection") or {}
        self.log_chunk = int(coll.get("log_chunk_blocks", 2_000_000))
        mech = venue.get("mechanics") or {}
        self.epoch_seconds = int((mech.get("epoch") or {}).get("length_seconds") or 0)
        self.reward_decimals = int(((venue.get("contracts") or {}).get("reward_token") or {}).get("decimals") or 18)

        # 実行中に覚えておく情報（再起動したら作り直す）
        self._n_cl = 0                                    # Voter.v3Pools の読んだ数
        self._pools: dict[str, dict[str, Any]] = {}       # プール → {token0, token1, tick_spacing, gauge}
        self._tokens: dict[str, tuple[str | None, int | None]] = {}
        self._pre: _Prefetched | None = None
        # 今のエポックの NotifyReward の集計（ゲージ → 最小単位の量）
        self._notify_epoch: int | None = None
        self._notify_to: int | None = None
        self._regular: dict[str, int] = {}
        self._manual: dict[str, int] = {}

    # --- 確認済みかどうか ---
    def _require_verified(self) -> None:
        missing = [n for n in REQUIRED_CONTRACTS if contract_address(self.venue, n) is None]
        missing += [n for n, m in (self.venue.get("mechanics") or {}).items()
                    if m.get("unverified", True) and not m.get("owner_acknowledged")]
        if not self.epoch_seconds:
            missing.append("epoch.length_seconds")
        if missing:
            raise AdapterNotReady("未確認の項目があるため収集できません: " + ", ".join(missing))

    def _call(self, to: str, sig: str, types: list[str], block: int, arg_types=(), args=()) -> tuple:
        return decode_result(types, self.rpc.eth_call(to, "0x" + encode_call(sig, arg_types, args).hex(), block))

    # --- プール一覧 ---
    def list_pools(self, block: int) -> list[PoolInfo]:
        """Voter に登録された CL プール（v3Pools）。どれもゲージがある。従来型（v2）は対象外。"""
        self._require_verified()
        voter = contract_address(self.venue, "voter")
        _total, _v2, n_cl = self._call(voter, "poolsCounts()", ["uint256", "uint256", "uint256"], block)
        if n_cl > self._n_cl:
            idx = range(self._n_cl, n_cl)
            res, _ = self.multicall.call([Call(voter, encode_call("v3Pools(uint256)", ["uint256"], [i])) for i in idx], block)
            new = [decode_result(["address"], r.data)[0].lower() for r in res]
            calls = []
            for p in new:
                calls += [Call(voter, encode_call("poolToGauge(address)", ["address"], [p])),
                          Call(p, encode_call("token0()")), Call(p, encode_call("token1()")),
                          Call(p, encode_call("tickSpacing()"))]
            res, _ = self.multicall.call(calls, block)
            for i, p in enumerate(new):
                g, t0, t1, ts = res[4 * i: 4 * i + 4]
                if not (g.success and t0.success and t1.success):
                    raise RuntimeError(f"プール {p} のゲージ・トークンを読めませんでした")
                gauge = decode_result(["address"], g.data)[0].lower()
                self._pools[p] = {
                    "token0": decode_result(["address"], t0.data)[0].lower(),
                    "token1": decode_result(["address"], t1.data)[0].lower(),
                    "tick_spacing": int(decode_result(["int24"], ts.data)[0]) if ts.success else None,
                    "gauge": gauge if gauge != ZERO else None,
                }
            self._n_cl = n_cl
        targets = sorted(p for p, m in self._pools.items() if m["gauge"])
        self._load_tokens({t for p in targets for t in (self._pools[p]["token0"], self._pools[p]["token1"])}, block)
        log.info("alandale pools listed", extra={"data": {"cl_pools": len(self._pools), "targets": len(targets)}})
        return [self._pool_info(p) for p in targets]

    def _pool_info(self, pool: str) -> PoolInfo:
        meta = self._pools[pool]
        s0, d0 = self._tokens.get(meta["token0"], (None, None))
        s1, d1 = self._tokens.get(meta["token1"], (None, None))
        return PoolInfo(
            pool_id=f"{self.venue_id}:{pool}", venue_id=self.venue_id, address=pool,
            token0=meta["token0"], token1=meta["token1"], tick_spacing=meta["tick_spacing"],
            gauge_address=meta["gauge"],
            token0_symbol=s0, token1_symbol=s1, token0_decimals=d0, token1_decimals=d1,
        )

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
        """全プールとゲージの値を Multicall でまとめて読み、今のエポックの NotifyReward を前回の続きから読む。"""
        self._require_verified()
        voter = contract_address(self.venue, "voter")
        rewarder = contract_address(self.venue, "gauge_rewarder")
        (epoch,) = self._call(contract_address(self.venue, "minter"), "active_period()", ["uint256"], block)
        epoch = int(epoch)
        calls: list[Call] = []
        keys: list[tuple[str, str, list[str]]] = []
        for p in pools:
            calls += [Call(p.address, encode_call("globalState()")), Call(p.address, encode_call("liquidity()"))]
            keys += [(p.address, "global_state", GLOBAL_STATE), (p.address, "liquidity", ["uint128"])]
            if p.gauge_address:
                calls += [Call(voter, encode_call("isAlive(address)", ["address"], [p.gauge_address])),
                          Call(rewarder, encode_call("rewardPerGaugePerEpoch(uint256,address)", ["uint256", "address"],
                                                     [epoch, p.gauge_address]))]
                keys += [(p.address, "alive", ["bool"]), (p.address, "epoch_total", ["uint256"])]
        res, raw = self.multicall.call(calls, block)
        values: dict[tuple[str, str], Any] = {}
        for (addr, name, types), r in zip(keys, res):
            values[(addr, name)] = _safe(lambda r=r, types=types: decode_result(types, r.data)) if r.success else None
        blk = self.rpc.request("eth_getBlockByNumber", [hex(block), False])
        block_time = datetime.fromtimestamp(int(blk["timestamp"], 16), UTC)
        logs = self._scan_notify(rewarder, epoch, block, int(blk["timestamp"], 16))
        self._pre = _Prefetched(block, block_time, epoch, values)
        return (RawCall("multicall_batch", {"block": block, "epoch": epoch,
                                            "keys": [f"{a}:{n}" for a, n, _ in keys]}, raw),
                RawCall("block", {"block": block}, {"timestamp": blk["timestamp"]}),
                RawCall("notify_reward_logs", {"epoch": epoch, "to_block": block}, logs))

    def _scan_notify(self, rewarder: str, epoch: int, block: int, block_ts: int) -> list[dict[str, Any]]:
        """今のエポックの NotifyReward を、前回読んだ続きから読んで、いつもの分と手で足した分に分けて足す。

        公開RPCは「アドレス1つ＋トピック1つ」なら広い範囲を読めるので、トピックは NotifyReward だけにして、
        エポックの一致は読んだ後に確かめる。読み取りに失敗したら例外にして、この回の収集を失敗にする
        （手で足した分を取り違えたまま記録しないため。判定は前の成功した回の値を使う）。
        """
        if self._notify_epoch != epoch or self._notify_to is None:
            start = self._block_before(epoch, block, block_ts)
            self._notify_epoch, self._notify_to = epoch, start - 1
            self._regular, self._manual = {}, {}
        if block <= self._notify_to:
            return []
        logs = self.rpc.get_logs(rewarder, [NOTIFY_REWARD], self._notify_to + 1, block, chunk=self.log_chunk)
        for lg in logs:
            t = lg["topics"]
            if len(t) < 4 or int(t[3], 16) != epoch:
                continue
            caller, gauge, amount = _addr(t[1]), _addr(t[2]), int(lg["data"], 16)
            book = self._regular if caller == gauge else self._manual
            book[gauge] = book.get(gauge, 0) + amount
        self._notify_to = block
        return logs

    def _block_before(self, ts: int, block: int, block_ts: int) -> int:
        """時刻 ts より前（か同じ）のブロック番号を探す。ブロックの間隔を測って見当をつけ、足りなければさらに戻る。"""
        if block_ts <= ts:
            return block
        back = min(block, 200_000)
        ref_ts = self._block_ts(block - back) if back else block_ts
        spb = (block_ts - ref_ts) / back if back and block_ts > ref_ts else 1.0   # 1ブロックあたりの秒数
        guess = block - int((block_ts - ts) / spb * 1.05) - 1_000
        for _ in range(10):
            if guess <= 0:
                return 0
            g_ts = self._block_ts(guess)
            if g_ts <= ts:
                return guess
            guess -= int((g_ts - ts) / spb * 1.5) + 10_000
        raise RuntimeError("エポックの始まりのブロックを見つけられませんでした")

    def _block_ts(self, block: int) -> int:
        return int(self.rpc.request("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)

    def _ensure(self, pool: PoolInfo, block: int) -> _Prefetched:
        if self._pre is None or self._pre.block != block or (pool.address, "global_state") not in self._pre.values:
            self.prefetch([pool], block)
        return self._pre

    def _get(self, pool: PoolInfo, block: int, name: str) -> Any:
        return self._ensure(pool, block).values.get((pool.address, name))

    # --- 状態 ---
    def pool_state(self, pool: PoolInfo, block: int) -> PoolState:
        gs = self._get(pool, block, "global_state")
        liq = self._get(pool, block, "liquidity")
        if gs is None or liq is None:
            raise RuntimeError("globalState / liquidity を読めませんでした")
        sqrt_p, tick, last_fee, _plugin, community_fee, _unlocked = gs
        d0, d1 = pool.token0_decimals, pool.token1_decimals
        price = price_from_sqrt(int(sqrt_p), d0, d1) if d0 is not None and d1 is not None else float("nan")
        return PoolState(
            pool_id=pool.pool_id, block_number=block, sqrt_price_x96=int(sqrt_p), tick=int(tick), price=price,
            fee=int(last_fee),                         # 100万分の1（60 = 0.006%）。up. の fee() と同じ単位
            liquidity_total=int(liq[0]),
            # ステークがないので、ボーナスはレンジ内の流動性すべてで分け合う
            liquidity_staked_inrange=int(liq[0]),
            # LP の手数料から金庫へ回す割合（1000分の1 → 100万分の1）。今は全プール 100%（LP の手数料は0）
            unstaked_fee=int(community_fee) * (1_000_000 // COMMUNITY_FEE_DENOMINATOR),
        )

    def gauge_rewards(self, pool: PoolInfo, block: int) -> RewardInfo:
        pre = self._ensure(pool, block)
        start = datetime.fromtimestamp(pre.epoch, UTC)
        end = datetime.fromtimestamp(pre.epoch + self.epoch_seconds, UTC)
        if not pool.gauge_address:
            return RewardInfo(pool.pool_id, block, None, None, None, end, block_time=pre.block_time, epoch_start=start)
        total = self._get(pool, block, "epoch_total")
        alive = self._get(pool, block, "alive")
        if total is None:
            raise RuntimeError("rewardPerGaugePerEpoch を読めませんでした")
        total_raw = int(total[0])
        gauge = pool.gauge_address.lower()
        manual = min(self._manual.get(gauge, 0), total_raw)
        regular = total_raw - manual
        logged = self._regular.get(gauge, 0) + self._manual.get(gauge, 0)
        if logged != total_raw:
            log.warning("alandale notify logs do not add up", extra={"data": {
                "pool": pool.pool_id, "epoch_total": str(total_raw), "logged": str(logged)}})
        rate = regular // self.epoch_seconds          # 7日に均等に配る前提（会場の警告に書いてある）
        is_alive = alive is None or bool(alive[0])
        # 切り替えの後、active_period が新しい週に変わるまで（配布待ち）は、前の週の量なのでボーナスは0とする
        effective = rate if is_alive and pre.block_time < end else 0
        return RewardInfo(
            pool_id=pool.pool_id, block_number=block,
            reward_token=contract_address(self.venue, "reward_token").lower(),
            reward_rate_raw=rate,
            reward_per_day=effective * 86400 / 10 ** self.reward_decimals,
            epoch_end=end, block_time=pre.block_time, epoch_start=start, period_finish=end,
            reward_rate_effective_raw=effective,
            gauge_alive=bool(alive[0]) if alive else None,
            epoch_total_raw=total_raw, manual_raw=manual,
        )

    def epoch_info(self, block: int) -> EpochInfo | None:
        self._require_verified()
        (epoch,) = self._call(contract_address(self.venue, "minter"), "active_period()", ["uint256"], block)
        return EpochInfo(self.epoch_seconds, datetime.fromtimestamp(int(epoch), UTC),
                         datetime.fromtimestamp(int(epoch) + self.epoch_seconds, UTC))


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
