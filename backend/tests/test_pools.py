"""N3: 幅に配るプール（Uniswap v3 / v4）の今の状態の読み取りと、流動性の割合でのボーナスの取り分のテスト。

チェーンには行かず、偽の読み取り口（eth_call の答え）と仮のデータベースで確かめる。
"""

import dataclasses
import math
from datetime import timedelta

import pytest
from eth_abi import encode

from farm_radar import opportunities as opps
from farm_radar.config import load_config
from farm_radar.feeds import pools, sources, store
from farm_radar.feeds import run
from farm_radar.feeds.run import FeedRunner, ReceiptContext, every_due
from farm_radar.registry import receipt_chains, stable_addresses
from farm_radar.rpc.abi import selector
from farm_radar.rpc.client import RpcCallError
from farm_radar.scoring import model as m

from .test_feeds import FakeFetcher
from .test_opportunities import ETH, NOW, NOW_S, OPPS, USDC, _campaign, _collect, _feeds_db, _opp, _prices

CFG = load_config()
BASE = receipt_chains(CFG.chains, CFG.root)[8453]
ADDRS = pools.official(BASE)
MANAGER = ADDRS["v4_pool_manager"]
POSM = ADDRS["v4_position_manager"]
V4_ID = "0x" + "4" * 64
V3_POOL = "0x" + "3" * 40
HOOK = "0x" + "b" * 40
L_POOL = 10 ** 15                                                 # 今の値段のところの流動性（仮）
PRICE = 2700.0                                                    # 1 WETH = 2,700 USDC
SQRT = int(math.sqrt(PRICE * 10 ** (6 - 18)) * 2 ** 96)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(pools, "CALL_INTERVAL_SECONDS", 0.0)


def _slot0(sqrt_p=SQRT, tick=-198_000, lp_fee=500) -> bytes:
    return (((lp_fee << 208) | ((tick & 0xFFFFFF) << 160) | sqrt_p)).to_bytes(32, "big")


class FakeRpc:
    """住所ごとに、関数の名前 → 答え（型の並び, 値の並び）を返す。無い関数は戻された（revert）とみなす。"""

    def __init__(self, answers):
        self.answers = {k.lower(): v for k, v in answers.items()}
        self.calls = []

    def eth_call(self, to, data, block):
        self.calls.append((to.lower(), data[:10]))
        for sig, (types, vals) in self.answers.get(to.lower(), {}).items():
            if data.startswith("0x" + selector(sig).hex()):
                return "0x" + encode(list(types), list(vals)).hex()
        raise RpcCallError(3, "execution reverted")


def _v4(hooks="0x" + "00" * 20, liquidity=L_POOL):
    words = [_slot0(), b"\0" * 32, b"\0" * 32, liquidity.to_bytes(32, "big")]
    return {MANAGER: {"extsload(bytes32,uint256)": (("bytes32[]",), (words,))},
            POSM: {"poolKeys(bytes25)": (("address", "address", "uint24", "int24", "address"),
                                         (ETH, USDC, 500, 10, hooks))}}


def _v3(factory=None):
    return {V3_POOL: {"factory()": (("address",), (factory or ADDRS["v3_factory"],)),
                      "slot0()": (("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"),
                                  (SQRT, -198_000, 0, 1, 1, 0, True)),
                      "liquidity()": (("uint128",), (L_POOL,)), "fee()": (("uint24",), (3000,))}}


# --- 読み取りの決まり ------------------------------------------------------------------------------

def test_v4_state_slot_follows_the_state_library():
    # keccak256(abi.encode(poolId, 6))（Uniswap v4-core の StateLibrary._getPoolStateSlot）
    from eth_hash.auto import keccak
    pid = bytes.fromhex("4" * 64)
    assert pools.v4_state_slot(V4_ID) == keccak(pid + (6).to_bytes(32, "big"))


def test_slot0_word_is_split_into_price_tick_and_fee():
    assert pools.parse_v4_slot0(_slot0()) == (SQRT, -198_000, 500)
    assert pools.price_from_sqrt(SQRT, 18, 6) == pytest.approx(PRICE, rel=1e-9)


def test_v4_reads_state_and_hooks_only_from_the_official_manager():
    rpc = FakeRpc(_v4(hooks=HOOK))
    p = {"pool_id": V4_ID, "kind": "v4", "manager": MANAGER, "dec0": 18, "dec1": 6}
    row = pools.read_v4(rpc, p, ADDRS)
    assert row["official"] == 1 and row["liquidity"] == L_POOL and row["sqrt_price_x96"] == SQRT
    assert row["lp_fee"] == 500 and row["tick"] == -198_000
    assert row["hooks"] == HOOK and row["tick_spacing"] == 10
    other = pools.read_v4(FakeRpc(_v4()), {**p, "manager": "0x" + "1" * 40}, ADDRS)
    assert other["official"] == 0 and "公式の住所と確かめられない" in other["error"]


def test_v4_hooks_are_read_once_per_pool():
    rpc = FakeRpc(_v4())
    p = {"pool_id": V4_ID, "kind": "v4", "manager": MANAGER, "dec0": 18, "dec1": 6}
    keys = {}
    pools.read_v4(rpc, p, ADDRS, keys)
    pools.read_v4(rpc, p, ADDRS, keys)
    assert [c for c in rpc.calls if c[0] == POSM.lower()] == [(POSM.lower(), "0x" + selector("poolKeys(bytes25)").hex())]


def test_v3_needs_the_official_factory():
    p = {"pool_id": V3_POOL, "kind": "v3", "manager": "", "dec0": 18, "dec1": 6}
    row = pools.read_v3(FakeRpc(_v3()), p, ADDRS, {})
    assert row["official"] == 1 and row["liquidity"] == L_POOL and row["lp_fee"] == 3000 and row["hooks"] == pools.ZERO
    bad = pools.read_v3(FakeRpc(_v3(factory="0x" + "2" * 40)), p, ADDRS, {})
    assert bad["official"] == 0 and "factory" in bad["error"]


# --- 1時間に1回の一覧（保存と、古い記録の扱い） --------------------------------------------------------

V4_SETTINGS = {"poolId": V4_ID, "poolManager": MANAGER, "currency0": ETH, "currency1": USDC,
               "decimalsCurrency0": 18, "decimalsCurrency1": 6, "symbolCurrency0": "WETH", "symbolCurrency1": "USDC",
               "lpFee": 500, "isOutOfRangeIncentivized": False,
               "weightFees": 6000, "weightToken0": 2000, "weightToken1": 2000}


def _cl_campaign(cid, opp, typ="UNISWAP_V4", **kw):
    c = _campaign(cid, opp, daily=1000.0, **kw)
    c["type"] = typ
    c["params"].update(V4_SETTINGS)
    return c


CL_OPPS = [
    _opp("o-cl", "Provide WETH/USDC on v4", typ="UNISWAP_V4", campaigns=[_cl_campaign("c-cl", "o-cl")]),
    _opp("o-alg", "Provide WETH/USDC on Algebra", typ="QUICKSWAP_ALGEBRA_12",
         campaigns=[_cl_campaign("c-alg", "o-alg", typ="QUICKSWAP_ALGEBRA_12")]),
]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_opportunities.OPPS", OPPS + CL_OPPS)
    path = tmp_path / "feeds.sqlite3"
    _feeds_db(path)
    return dataclasses.replace(CFG, database_path=tmp_path / "main.sqlite3",
                               feeds=dataclasses.replace(CFG.feeds, database_path=path, raw_dir=tmp_path / "raw"))


def _run_pools(cfg, rpc, now=NOW):
    """1時間に1回の一覧だけを、15分ごとの回として動かす（ほかの一覧は外に行くので動かさない）。"""
    chains = receipt_chains(cfg.chains, cfg.root)
    ctx = ReceiptContext(chains=chains, stables={c: stable_addresses(v, cfg.root) for c, v in chains.items()},
                         rpc_factory=lambda chain: rpc, sleep=lambda s: None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(run, "SOURCES", (sources.BY_ID["pool_states"],))
        return FeedRunner(cfg.feeds, fetcher=FakeFetcher(), coin_chains={8453: "base"}, receipt_ctx=ctx).run("15min", now)


def test_the_hourly_run_reads_only_readable_pool_types_and_saves_them(cfg):
    rpc = FakeRpc(_v4())
    out = _run_pools(cfg, rpc)
    assert {"source": "pool_states", "status": "ok", "items": 1, "new": 0, "gone": 0} in out
    conn = store.connect(cfg.feeds.database_path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM pool_state_snaps")]
    assert len(rows) == 1                           # Algebra のプールは読み方がないので読まない
    r = rows[0]
    assert r["pool_id"] == V4_ID and r["kind"] == "v4" and r["official"] == 1 and int(r["liquidity"]) == L_POOL
    assert r["hooks"] == pools.ZERO and r["tick_spacing"] == 10 and r["price"] == pytest.approx(PRICE)
    # 55分たつまでは読まない。たてば読む
    src = sources.BY_ID["pool_states"]
    assert not every_due(conn, src, NOW + timedelta(minutes=30))
    assert every_due(conn, src, NOW + timedelta(minutes=56))
    assert pools.latest(conn, 3, NOW + timedelta(hours=2))
    assert not pools.latest(conn, 3, NOW + timedelta(hours=4))   # 3時間より古い記録は使わない


def test_old_pool_table_gets_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE pool_state_snaps (chain_id INTEGER NOT NULL, pool_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL, kind TEXT NOT NULL, block INTEGER, sqrt_price_x96 TEXT, tick INTEGER,
                    liquidity TEXT, lp_fee INTEGER, price REAL, official INTEGER, error TEXT,
                    PRIMARY KEY (chain_id, pool_id, checked_at))""")
    conn.commit()
    conn.close()
    conn = store.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pool_state_snaps)")}
    assert {"hooks", "tick_spacing"} <= cols


# --- 流動性の割合でのボーナスの取り分 -----------------------------------------------------------------

def test_chain_record_gives_the_liquidity_share_and_picks_a_range(cfg):
    _run_pools(cfg, FakeRpc(_v4()))
    ops = _collect(cfg)
    o = ops["o-cl"]
    codes = {f.code: f for f in o.flags}
    assert "RANGE_CHAIN" in codes and "RANGE" not in codes
    assert "60%・20%・20%" in codes["RANGE_CHAIN"].text and "チェーンの記録" in codes["RANGE_CHAIN"].text
    s = cfg.opportunities
    eth_usd = _prices(2700.0, 0.006)[-1]["price"]   # test_opportunities の WETH の値段の記録の最後
    t0 = m.TokenSide(usd=eth_usd, decimals=18, sigma_usd=0.0)
    t1 = m.TokenSide(usd=1.0, decimals=6, sigma_usd=0.0)
    for case, mult in (("normal", 1.0), ("cautious", s.cautious_tvl_multiple)):
        v = o.calc["1000"][case]["no_hedge"]
        r = v.range_pct / 100
        assert r in opps.RANGES
        c_pos = v.split["pool"]
        mine = m.liquidity_for_usd(c_pos, PRICE, r, t0, t1)
        lshare = mine / (L_POOL * mult + mine)
        dshare = c_pos / (1_000_000.0 * mult + c_pos)
        assert v.liquidity_share == pytest.approx(lshare, rel=1e-6)
        # 予算 × (手数料の重み × 流動性の割合 ＋ コインの重み × 預かり額の割合) ÷ 重みの合計（Merkl の資料）× 幅の中にいる割合
        bonus = 1000.0 * (0.6 * lshare + 0.4 * dshare) * v.in_range_ratio
        if case == "normal":
            assert v.income == pytest.approx(bonus, rel=1e-9)
        else:                                       # 控えめは、いる日数のあいだのボーナスのコインの値下がりも数える
            assert v.income < bonus
    # 手数料の段はチェーンの記録（500 = 0.05%）を使う。Algebra のプールは読み方がないので前と同じ
    alg = {f.code: f for f in ops["o-alg"].flags}
    assert "RANGE_CHAIN" not in alg and "読み方がまだ無い" in alg["RANGE"].text
    assert ops["o-alg"].calc["1000"]["normal"]["no_hedge"].range_pct == s.merkl_range_pct
    assert ops["o-alg"].calc["1000"]["normal"]["no_hedge"].liquidity_share is None


def test_hooked_pool_falls_back_to_the_deposit_share(cfg):
    _run_pools(cfg, FakeRpc(_v4(hooks=HOOK)))
    o = _collect(cfg)["o-cl"]
    codes = {f.code: f for f in o.flags}
    assert "RANGE_CHAIN" not in codes and "フック" in codes["RANGE"].text
    v = o.calc["1000"]["normal"]["no_hedge"]
    assert v.liquidity_share is None and v.range_pct == cfg.opportunities.merkl_range_pct


def test_no_or_stale_chain_record_falls_back_with_the_reason(cfg):
    o = _collect(cfg)["o-cl"]
    assert "まだ無い" in {f.code: f for f in o.flags}["RANGE"].text
    _run_pools(cfg, FakeRpc(_v4()), now=NOW - timedelta(hours=5))   # 5時間前に読んだきり
    o = _collect(cfg)["o-cl"]
    assert "まだ無い" in {f.code: f for f in o.flags}["RANGE"].text


def test_wider_range_gives_a_smaller_share_for_the_same_money():
    cl = opps.ClPool(chain_id=8453, pool_id=V4_ID, kind="v4", price=PRICE, liquidity=float(L_POOL), dec0=18, dec1=6,
                     usd0=2700.0, usd1=1.0, checked_at=f"{NOW.isoformat()}", lp_fee=500)
    shares = [opps.cl_share(1000.0, r, cl, 1.0)[1] for r in sorted(opps.RANGES)]
    assert shares == sorted(shares, reverse=True) and 0 < shares[-1] < shares[0] < 1
    assert opps.cl_share(1000.0, 0.15, cl, 1.5)[1] < opps.cl_share(1000.0, 0.15, cl, 1.0)[1]   # 控えめは薄まる
    assert NOW_S > 0
