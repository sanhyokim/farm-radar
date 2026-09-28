"""up. アダプターのテスト。偽のチェーン（MockTransport）で、実際のRPCの形のまま動かす。"""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from eth_abi import decode, encode

from farm_radar.adapters.base import AdapterNotReady
from farm_radar.adapters.up_robinhood import UpRobinhoodAdapter, price_from_sqrt
from farm_radar.collectors.snapshot import collect_venue
from farm_radar.config import load_venue
from farm_radar.db import database as db
from farm_radar.rpc.abi import selector, topic
from farm_radar.rpc.client import Endpoint, RpcClient
from farm_radar.rpc.multicall import AGGREGATE3

VENUE = load_venue("up-robinhood")
C = VENUE["contracts"]
FACTORY = C["factory_v3"]["address"].lower()
VOTER = C["voter"]["address"].lower()
MC = C["multicall3"]["address"].lower()
UP = C["reward_token"]["address"].lower()

WETH = "0x" + "0e" * 20
USDG = "0x" + "05" * 20
POOL_A = "0x" + "a1" * 20   # ゲージあり
POOL_B = "0x" + "b2" * 20   # ゲージなし
POOL_V2 = "0x" + "c3" * 20  # Voter にはあるが CL ではない（v2）
GAUGE_A = "0x" + "9a" * 20
GAUGE_V2 = "0x" + "9c" * 20
EPOCH = 1790208000          # 2026-09-24 木 00:00 UTC
SQRT_P = 2**96 * 2          # 価格 4（桁の調整前）


def _word(addr):
    return "0x" + "00" * 12 + addr[2:]


class FakeChain:
    def __init__(self, block_time=EPOCH + 3 * 86400, reward_rate=10**17, period_finish=EPOCH + 604800):
        self.block_time = block_time
        self.reward_rate = reward_rate
        self.period_finish = period_finish
        self.calls = []

    def value(self, to, data):
        sel = data[:4]
        s = lambda sig: selector(sig) == sel  # noqa: E731
        if to == VOTER:
            if s("length()"):
                return encode(["uint256"], [2])
            if s("pools(uint256)"):
                (i,) = decode(["uint256"], data[4:])
                return encode(["address"], [[POOL_A, POOL_V2][i]])
            if s("gauges(address)"):
                (p,) = decode(["address"], data[4:])
                return encode(["address"], [{POOL_A: GAUGE_A, POOL_V2: GAUGE_V2}[p.lower()]])
            if s("isAlive(address)"):
                return encode(["bool"], [True])
        if to in (WETH, USDG):
            if s("symbol()"):
                return encode(["string"], ["WETH" if to == WETH else "USDG"])
            if s("decimals()"):
                return encode(["uint8"], [18 if to == WETH else 6])
        if to in (POOL_A, POOL_B):
            if s("slot0()"):
                return encode(["uint160", "int24", "uint16", "uint16", "uint16", "bool"], [SQRT_P, 13863, 0, 1, 1, True])
            if s("liquidity()"):
                return encode(["uint128"], [10**20])
            if s("stakedLiquidity()"):
                return encode(["uint128"], [6 * 10**19 if to == POOL_A else 0])
            if s("fee()"):
                return encode(["uint24"], [500])
            if s("unstakedFee()"):
                return encode(["uint24"], [100000 if to == POOL_A else 0])
        if to == GAUGE_A:
            if s("rewardRate()"):
                return encode(["uint256"], [self.reward_rate])
            if s("periodFinish()"):
                return encode(["uint256"], [self.period_finish])
        raise AssertionError(f"unexpected call {to} {sel.hex()}")

    def handle(self, body):
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "eth_blockNumber":
            return {"result": hex(7_000_000 + self.block_time - EPOCH)}  # 時刻ごとに別のブロック
        if method == "eth_getCode":
            return {"result": "0x6080"}
        if method == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(self.block_time), "number": params[0]}}
        if method == "eth_getLogs":
            f = params[0]
            assert f["address"].lower() == FACTORY and f["topics"] == [topic("PoolCreated(address,address,int24,address)")]
            logs = []
            for pool, blk in ((POOL_A, 6_200_000), (POOL_B, 6_300_000)):
                if int(f["fromBlock"], 16) <= blk <= int(f["toBlock"], 16):
                    logs.append({"topics": [topic("PoolCreated(address,address,int24,address)"), _word(WETH), _word(USDG),
                                            "0x" + (2**256 - 100).to_bytes(32, "big").hex()],  # tickSpacing = -100 → 符号のテスト
                                 "data": _word(pool), "blockNumber": hex(blk)})
            return {"result": logs}
        if method == "eth_call":
            to, data = params[0]["to"].lower(), bytes.fromhex(params[0]["data"][2:])
            if to == MC:
                assert data[:4] == selector(AGGREGATE3)
                (calls,) = decode(["(address,bool,bytes)[]"], data[4:])
                out = [(True, self.value(t.lower(), bytes(d))) for t, _, d in calls]
                return {"result": "0x" + encode(["(bool,bytes)[]"], [out]).hex()}
            return {"result": "0x" + self.value(to, data).hex()}
        raise AssertionError(method)


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def adapter(node, settings, chain):
    node.on("https://public/", chain.handle)
    rpc = RpcClient([Endpoint("public", "https://public/")], settings, transport=node.transport(), sleep=lambda s: None)
    return UpRobinhoodAdapter(VENUE, rpc)


def test_price_from_sqrt():
    # sqrtPriceX96 = 2^96 → 生の価格 1。token0 が18桁、token1 が6桁なら 1e12
    assert price_from_sqrt(2**96, 18, 6) == pytest.approx(1e12)
    assert price_from_sqrt(2**96 * 2, 6, 6) == pytest.approx(4.0)


def test_refuses_when_mechanic_unverified_and_not_acknowledged(node, settings):
    venue = copy.deepcopy(VENUE)
    del venue["mechanics"]["gauge_emission_cap"]["owner_acknowledged"]
    a = UpRobinhoodAdapter(venue, rpc=None)
    with pytest.raises(AdapterNotReady) as e:
        a.list_pools(1)
    assert "gauge_emission_cap" in str(e.value)


def test_list_pools_uses_factory_events_and_only_gauged_cl(adapter):
    pools = adapter.list_pools(7_000_000)
    assert [p.address for p in pools] == [POOL_A]          # ゲージのない B と、v2 のゲージは除く
    p = pools[0]
    assert p.gauge_address == GAUGE_A and p.tick_spacing == -100 and p.created_block == 6_200_000
    assert (p.token0_symbol, p.token1_symbol, p.token0_decimals, p.token1_decimals) == ("WETH", "USDG", 18, 6)
    adapter.scope = "all"
    assert {p.address for p in adapter.list_pools(7_000_001)} == {POOL_A, POOL_B}


def test_state_and_rewards_from_one_prefetch(adapter, chain):
    pools = adapter.list_pools(7_000_000)
    before = chain.calls.count("eth_call")
    adapter.prefetch(pools, 7_000_000)
    assert chain.calls.count("eth_call") - before == 1     # 1回の Multicall でまとめて読む
    st = adapter.pool_state(pools[0], 7_000_000)
    assert st.liquidity_total == 10**20 and st.liquidity_staked_inrange == 6 * 10**19
    assert st.unstaked_fee == 100000 and st.price == pytest.approx(4.0 * 10**12)
    rw = adapter.gauge_rewards(pools[0], 7_000_000)
    assert rw.reward_token == UP and rw.reward_rate_raw == 10**17 and rw.reward_rate_effective_raw == 10**17
    assert rw.reward_per_day == pytest.approx(8640.0)
    assert rw.epoch_start == datetime.fromtimestamp(EPOCH, UTC)
    assert rw.epoch_end == datetime.fromtimestamp(EPOCH + 604800, UTC)
    assert rw.epoch_start.weekday() == 3 and rw.epoch_start.hour == 0   # 木曜 00:00 UTC
    assert chain.calls.count("eth_call") - before == 1


def test_expired_period_means_zero_effective_rate(node, settings):
    chain = FakeChain(period_finish=EPOCH)   # 先週で配布が終わり、今週はまだ配られていない
    node.on("https://public/", chain.handle)
    a = UpRobinhoodAdapter(VENUE, RpcClient([Endpoint("public", "https://public/")], settings,
                                            transport=node.transport(), sleep=lambda s: None))
    p = a.list_pools(7_000_000)[0]
    rw = a.gauge_rewards(p, 7_000_000)
    assert rw.reward_rate_raw == 10**17 and rw.reward_rate_effective_raw == 0 and rw.reward_per_day == 0


def _collect(adapter, chain, conn, when, **kw):
    chain.block_time = int(when.timestamp())
    adapter.rpc._cache.clear()   # 実際には15分おきなので、"latest" の短いキャッシュは切れている
    return collect_venue(conn, adapter, adapter.rpc, snapshot_minutes=15, now=lambda: when, **kw)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.upsert_venue(c, VENUE)
    return c


def test_collect_saves_reward_rate_epoch_and_flip_mark(adapter, chain, conn):
    t0 = datetime.fromtimestamp(EPOCH, UTC) + timedelta(minutes=30)   # 切り替えから30分
    r = _collect(adapter, chain, conn, t0)
    assert r.status == "ok" and r.pools_ok == 1
    row = conn.execute("SELECT * FROM pool_snapshots").fetchone()
    assert row["reward_rate_raw"] == str(10**17) and row["reward_rate_effective_raw"] == str(10**17)
    assert row["epoch_start"].startswith("2026-09-24T00:00") and row["epoch_end"].startswith("2026-10-01T00:00")
    assert row["epoch_just_flipped"] == 1 and row["liquidity_staked_inrange"] == str(6 * 10**19)
    pool = conn.execute("SELECT * FROM pools").fetchone()
    assert pool["token0_symbol"] == "WETH" and pool["gauge_address"] == GAUGE_A
    assert conn.execute("SELECT COUNT(*) FROM raw_rpc WHERE kind='multicall_batch'").fetchone()[0] == 1

    r = _collect(adapter, chain, conn, t0 + timedelta(hours=3))
    assert conn.execute("SELECT epoch_just_flipped FROM pool_snapshots ORDER BY ts DESC").fetchone()[0] == 0


def test_reward_rate_drop_alert_once_per_epoch(adapter, chain, conn):
    t = datetime.fromtimestamp(EPOCH, UTC) + timedelta(days=1)
    _collect(adapter, chain, conn, t)
    chain.reward_rate = int(10**17 * 0.75)                    # 25% 減: 通知しない
    _collect(adapter, chain, conn, t + timedelta(minutes=15))
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    chain.reward_rate = int(10**17 * 0.69)                    # 最大値から31% 減: 通知する
    _collect(adapter, chain, conn, t + timedelta(minutes=30))
    _collect(adapter, chain, conn, t + timedelta(minutes=45))  # 同じエポックでは二重に記録しない
    rows = conn.execute("SELECT * FROM alerts").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "reward_rate_drop" and "31%" in rows[0]["message_ja"]


def test_new_epoch_is_not_compared_with_previous(adapter, chain, conn):
    t = datetime.fromtimestamp(EPOCH, UTC) + timedelta(days=6)
    _collect(adapter, chain, conn, t)
    chain.reward_rate = 10**16                                # 次のエポックで大きく減っても、エポックをまたいで比べない
    chain.period_finish = EPOCH + 2 * 604800
    _collect(adapter, chain, conn, t + timedelta(days=1, hours=3))
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_venue_yaml_has_c4_warning_and_sourcify_match():
    warn = [w for w in VENUE["warnings"] if w["key"] == "gauge_cap_controller_unverified"]
    assert warn and warn[0]["code"] == "C4" and warn[0]["message_ja"]
    for name, c in VENUE["contracts"].items():
        assert c["sourcify_match"] in ("full", "partial", "none"), name
        if not c.get("unverified", True) and (c.get("source_url") or "").startswith("https://repo.sourcify.dev"):
            assert c["sourcify_match"] != "none", name
