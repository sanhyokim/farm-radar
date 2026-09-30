"""Alandale アダプターのテスト（M6）。偽のチェーン（MockTransport）で、実際のRPCの形のまま動かす。"""

from datetime import UTC, datetime, timedelta

import pytest
from eth_abi import decode, encode

from farm_radar.adapters.alandale_robinhood import NOTIFY_REWARD, AlandaleRobinhoodAdapter
from farm_radar.adapters.registry import build_adapter
from farm_radar.collectors.snapshot import collect_venue
from farm_radar.config import load_venue, mechanic_value, practice_allowed
from farm_radar.db import database as db
from farm_radar.rpc.abi import selector
from farm_radar.rpc.client import Endpoint, RpcClient
from farm_radar.rpc.multicall import AGGREGATE3

VENUE = load_venue("alandale-robinhood")
C = VENUE["contracts"]
VOTER = C["voter"]["address"].lower()
REWARDER = C["gauge_rewarder"]["address"].lower()
MINTER = C["minter"]["address"].lower()
MC = C["multicall3"]["address"].lower()
LUTE = C["reward_token"]["address"].lower()
SAFE = C["rewarder_safe"]["address"].lower()

WETH = "0x" + "0e" * 20
USDG = "0x" + "05" * 20
POOL_A = "0x" + "a1" * 20
POOL_B = "0x" + "b2" * 20
GAUGE_A = "0x" + "9a" * 20
GAUGE_B = "0x" + "9b" * 20
EPOCH = 1790208000          # 2026-09-24 木 00:00 UTC
WEEK = 604800
BLOCK0 = 70_000_000         # EPOCH のときのブロック（1秒に10ブロック）
SQRT_P = 2**96 * 2          # 価格 4（桁の調整前）
E18 = 10**18


def _word(addr):
    return "0x" + "00" * 12 + addr[2:]


def _block_of(ts):
    return BLOCK0 + (ts - EPOCH) * 10


def _ts_of(block):
    return EPOCH + (block - BLOCK0) // 10


class FakeChain:
    def __init__(self, block_time=EPOCH + 3 * 86400, active_period=EPOCH):
        self.block_time = block_time
        self.active_period = active_period
        self.alive = True
        self.community_fee = 1000
        # (ブロック, caller, gauge, epoch, 量)
        self.notify = [
            (BLOCK0 + 700, GAUGE_A, GAUGE_A, EPOCH, 700_000 * E18),        # いつもの分（切り替え直後の配布）
            (BLOCK0 + 700, GAUGE_B, GAUGE_B, EPOCH, 50_000 * E18),
            (BLOCK0 - 3_000_000, GAUGE_A, GAUGE_A, EPOCH - WEEK, 1 * E18),  # 先週の分（数えない）
        ]
        self.calls = []

    def add_manual(self, when_ts, amount):
        self.notify.append((_block_of(when_ts), SAFE, GAUGE_A, self.active_period, amount))

    def total(self, gauge, epoch):
        return sum(a for _, _, g, e, a in self.notify if g == gauge and e == epoch)

    def value(self, to, data):
        sel = data[:4]
        s = lambda sig: selector(sig) == sel  # noqa: E731
        if to == VOTER:
            if s("poolsCounts()"):
                return encode(["uint256"] * 3, [4, 2, 2])
            if s("v3Pools(uint256)"):
                (i,) = decode(["uint256"], data[4:])
                return encode(["address"], [[POOL_A, POOL_B][i]])
            if s("poolToGauge(address)"):
                (p,) = decode(["address"], data[4:])
                return encode(["address"], [{POOL_A: GAUGE_A, POOL_B: GAUGE_B}[p.lower()]])
            if s("isAlive(address)"):
                return encode(["bool"], [self.alive])
        if to == MINTER and s("active_period()"):
            return encode(["uint256"], [self.active_period])
        if to == REWARDER and s("rewardPerGaugePerEpoch(uint256,address)"):
            e, g = decode(["uint256", "address"], data[4:])
            return encode(["uint256"], [self.total(g.lower(), e)])
        if to in (WETH, USDG):
            if s("symbol()"):
                return encode(["string"], ["WETH" if to == WETH else "USDG"])
            if s("decimals()"):
                return encode(["uint8"], [18 if to == WETH else 6])
        if to in (POOL_A, POOL_B):
            if s("token0()"):
                return encode(["address"], [WETH])
            if s("token1()"):
                return encode(["address"], [USDG])
            if s("tickSpacing()"):
                return encode(["int24"], [-10 if to == POOL_A else 60])
            if s("globalState()"):
                return encode(["uint160", "int24", "uint16", "uint8", "uint16", "bool"],
                              [SQRT_P, -197390, 60, 193, self.community_fee, True])
            if s("liquidity()"):
                return encode(["uint128"], [10**20])
        raise AssertionError(f"unexpected call {to} {sel.hex()}")

    def head(self):
        return _block_of(self.block_time)

    def handle(self, body):
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "eth_blockNumber":
            return {"result": hex(self.head())}
        if method == "eth_getCode":
            return {"result": "0x6080"}
        if method == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(_ts_of(int(params[0], 16))), "number": params[0]}}
        if method == "eth_getLogs":
            f = params[0]
            # 公開RPCで広い範囲を読めるのは「アドレス1つ＋トピック1つ」のとき
            assert f["address"].lower() == REWARDER and f["topics"] == [NOTIFY_REWARD]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return {"result": [
                {"topics": [NOTIFY_REWARD, _word(c), _word(g), "0x" + e.to_bytes(32, "big").hex()],
                 "data": "0x" + a.to_bytes(32, "big").hex(), "blockNumber": hex(b)}
                for b, c, g, e, a in self.notify if lo <= b <= hi]}
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
    return build_adapter(VENUE, rpc)


def test_venue_file_is_observe_only_with_two_minor_warnings():
    assert isinstance(build_adapter(VENUE, rpc=None), AlandaleRobinhoodAdapter)
    assert practice_allowed(VENUE) is False and VENUE["practice_note_ja"]
    # オーナー条件3: 分け方がサーバーで決まる・手で足すことがある → 軽微な警告2つ（判定は最高🟡）
    assert {w["key"] for w in VENUE["warnings"]} == {"offchain_reward_split", "manual_top_ups"}
    assert all(w["code"] == "C4" and w["level"] == "minor" for w in VENUE["warnings"])
    # LP は取引手数料を受け取らない（確認済み）
    assert mechanic_value(VENUE, "lp_receives_swap_fees", True) is False
    assert VENUE["epoch_note_ja"] and VENUE["mechanics"]["epoch"]["length_seconds"] == WEEK
    for name, c in VENUE["contracts"].items():
        assert c["sourcify_match"] in ("full", "partial", "none"), name
        assert c.get("unverified") is False and c.get("checked_at"), name
        if (c.get("source_url") or "").startswith("https://repo.sourcify.dev"):
            assert c["sourcify_match"] != "none", name


def test_list_pools_from_voter_cl_pools(adapter):
    pools = adapter.list_pools(_block_of(EPOCH + 86400))
    assert [p.address for p in pools] == [POOL_A, POOL_B]
    a = pools[0]
    assert a.gauge_address == GAUGE_A and a.tick_spacing == -10 and a.pool_id == f"alandale-robinhood:{POOL_A}"
    assert (a.token0_symbol, a.token1_symbol, a.token0_decimals, a.token1_decimals) == ("WETH", "USDG", 18, 6)


def test_state_counts_all_inrange_liquidity_and_no_lp_fees(adapter):
    b = _block_of(EPOCH + 86400)
    pools = adapter.list_pools(b)
    adapter.prefetch(pools, b)
    st = adapter.pool_state(pools[0], b)
    assert st.price == pytest.approx(4.0 * 10**12) and st.tick == -197390 and st.fee == 60
    assert st.liquidity_total == st.liquidity_staked_inrange == 10**20   # ステークがないので全部で分け合う
    assert st.unstaked_fee == 1_000_000                                  # 手数料は100%金庫へ（LP は0）


def test_rewards_split_regular_and_manual(adapter, chain):
    chain.add_manual(EPOCH + 2 * 86400, 150_000 * E18)          # 週の途中で運営が手で足した
    b = chain.head()
    pools = adapter.list_pools(b)
    raw = adapter.prefetch(pools, b)
    assert raw[-1].kind == "notify_reward_logs"
    rw = adapter.gauge_rewards(pools[0], b)
    assert rw.reward_token == LUTE
    assert rw.epoch_total_raw == 850_000 * E18 and rw.manual_raw == 150_000 * E18
    # 1秒あたりは「いつもの分 ÷ 7日」。手で足した分は入れない
    assert rw.reward_rate_raw == 700_000 * E18 // WEEK == rw.reward_rate_effective_raw
    assert rw.reward_per_day == pytest.approx(100_000, rel=1e-9)
    assert rw.epoch_start == datetime.fromtimestamp(EPOCH, UTC) and rw.period_finish == rw.epoch_end
    assert rw.epoch_end == datetime.fromtimestamp(EPOCH + WEEK, UTC)
    b2 = adapter.gauge_rewards(pools[1], b)
    assert b2.manual_raw == 0 and b2.reward_rate_raw == 50_000 * E18 // WEEK


def test_notify_logs_are_read_incrementally(adapter, chain):
    b = chain.head()
    pools = adapter.list_pools(b)
    adapter.prefetch(pools, b)
    first = chain.calls.count("eth_getLogs")
    # 15分後: 前回の続きだけ読む（1回）。その間に手で足された分が入る
    chain.block_time += 900
    chain.add_manual(chain.block_time - 60, 10 * E18)
    b = chain.head()
    adapter.prefetch(pools, b)
    assert chain.calls.count("eth_getLogs") - first == 1
    assert adapter.gauge_rewards(pools[0], b).manual_raw == 10 * E18


def test_waiting_for_distribution_after_the_flip_means_zero(adapter, chain):
    # 木曜 00:00 を過ぎても、配布の取引が出るまでは active_period が前の週のまま → ボーナスは0で計算
    chain.block_time = EPOCH + WEEK + 30
    b = chain.head()
    pools = adapter.list_pools(b)
    rw = adapter.gauge_rewards(pools[0], b)
    assert rw.reward_rate_raw > 0 and rw.reward_rate_effective_raw == 0 and rw.reward_per_day == 0


def test_dead_gauge_gives_no_reward(adapter, chain):
    chain.alive = False
    b = chain.head()
    rw = adapter.gauge_rewards(adapter.list_pools(b)[0], b)
    assert rw.gauge_alive is False and rw.reward_rate_effective_raw == 0


def test_collect_saves_manual_bonus_apart(adapter, chain):
    conn = db.connect(":memory:")
    db.upsert_venue(conn, VENUE)
    chain.add_manual(EPOCH + 86400, 150_000 * E18)
    when = datetime.fromtimestamp(chain.block_time, UTC)
    r = collect_venue(conn, adapter, adapter.rpc, snapshot_minutes=15, now=lambda: when)
    assert r.status == "ok" and r.pools_ok == 2
    row = conn.execute("SELECT * FROM pool_snapshots WHERE pool_id=?", (f"alandale-robinhood:{POOL_A}",)).fetchone()
    assert row["reward_epoch_total_raw"] == str(850_000 * E18) and row["reward_manual_raw"] == str(150_000 * E18)
    assert row["reward_rate_raw"] == str(700_000 * E18 // WEEK) and row["unstaked_fee"] == 1_000_000
    assert row["liquidity_staked_inrange"] == row["liquidity_total"]
    kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM raw_rpc")}
    assert {"multicall_batch", "notify_reward_logs"} <= kinds


def test_epoch_start_is_found_even_if_blocks_were_slow(node, settings):
    # ブロックの間隔が途中で変わっても、エポックの始まりより前から読む（手で足した分を読み落とさない）
    chain = FakeChain(block_time=EPOCH + 5 * 86400)
    chain.add_manual(EPOCH + 5, 7 * E18)        # 切り替えの直後
    node.on("https://public/", chain.handle)
    a = AlandaleRobinhoodAdapter(VENUE, RpcClient([Endpoint("public", "https://public/")], settings,
                                                  transport=node.transport(), sleep=lambda s: None))
    start = a._block_before(EPOCH, chain.head(), chain.block_time)
    assert _ts_of(start) <= EPOCH and start > _block_of(EPOCH - 2 * 86400)
    b = chain.head()
    assert a.gauge_rewards(a.list_pools(b)[0], b).manual_raw == 7 * E18


def test_block_before_returns_head_when_time_already_passed(adapter, chain):
    assert adapter._block_before(chain.block_time + 10, chain.head(), chain.block_time) == chain.head()
    assert adapter._block_before(EPOCH, 5, EPOCH + timedelta(days=1).total_seconds()) == 0
