"""N2c: 中身がステーブルの預かり証（SPEC 13.1 の5。2026-10-02 オーナー決定）のテスト。

チェーンには行かず、偽の読み取り口（eth_call の答え）と仮のデータベースで確かめる。
"""

import dataclasses
import json

import pytest
from eth_abi import encode

from farm_radar import opportunities as opps
from farm_radar.config import load_config
from farm_radar.feeds import receipts, store
from farm_radar.feeds.run import FeedRunner, ReceiptContext
from farm_radar.registry import receipt_chains, stable_addresses
from farm_radar.rpc.abi import selector
from farm_radar.rpc.client import RpcCallError

from .test_feeds import FakeFetcher
from .test_opportunities import NOW, _collect, _feeds_db

BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"     # chains/base.yaml の stablecoins（Circle の公式の資料）
GUESS = "0x" + "9" * 40                                         # test_opportunities の o-guess のボーナスのコイン
OTHER = "0x" + "7" * 40


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(receipts, "CALL_INTERVAL_SECONDS", 0.0)


class FakeRpc:
    """住所ごとに、関数の名前 → 答え（型, 値）を返す。無い関数は戻された（revert）とみなす。"""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def eth_call(self, to, data, block):
        self.calls.append((to, data[:10]))
        for sig, (typ, val) in self.answers.get(to.lower(), {}).items():
            if data.startswith("0x" + selector(sig).hex()):
                return "0x" + encode([typ], [val]).hex()
        raise RpcCallError(3, "execution reverted")


def _vault(asset=BASE_USDC, per=1_000_189):
    return {
        GUESS: {"asset()": ("address", asset), "decimals()": ("uint8", 18),
                "convertToAssets(uint256)": ("uint256", per), "name()": ("string", "Test USDC Vault"),
                "symbol()": ("string", "tvUSDC")},
        BASE_USDC: {"decimals()": ("uint8", 6), "symbol()": ("string", "USDC")},
    }


def test_chain_registry_lists_the_stablecoins_and_blockscout():
    cfg = load_config()
    chains = receipt_chains(cfg.chains, cfg.root)
    assert set(chains) == {4663, 8453}
    assert stable_addresses(chains[8453], cfg.root) == {BASE_USDC: "USDC"}
    assert "0x5fc5360d0400a0fd4f2af552add042d716f1d168" in stable_addresses(chains[4663], cfg.root)   # USDG
    assert chains[8453]["blockscout"] == "https://base.blockscout.com"


def test_probe_reads_the_inside_of_a_vault_and_skips_other_coins():
    rpc = FakeRpc(_vault())
    row = receipts.probe(rpc, GUESS, {BASE_USDC: "USDC"})
    assert row["is_vault"] == 1 and row["asset"] == BASE_USDC and row["asset_symbol"] == "USDC"
    assert row["assets_per_share"] == pytest.approx(1.000189) and row["name"] == "Test USDC Vault"
    plain = receipts.probe(rpc, OTHER, {})
    assert plain["is_vault"] == 0 and "金庫の形ではない" in plain["error"]
    assert [c for c in rpc.calls if c[0] == OTHER] == [(OTHER, "0x" + selector("asset()").hex())]   # 1回で見切る


def test_verified_asks_blockscout_then_sourcify():
    chain = {"chain_id": 8453, "blockscout": "https://base.blockscout.com"}
    bs = f"https://base.blockscout.com/api/v2/smart-contracts/{GUESS}"
    sf = f"{receipts.SOURCIFY}/8453/{GUESS}"
    assert receipts.verified(FakeFetcher(texts={bs: json.dumps({"is_verified": True})}).text, chain, GUESS) == \
        (1, "blockscout")
    f = FakeFetcher(texts={bs: json.dumps({"is_verified": False}), sf: json.dumps({"match": "exact_match"})})
    assert receipts.verified(f.text, chain, GUESS) == (1, "sourcify")
    f = FakeFetcher(texts={bs: json.dumps({"is_verified": False})}, fail={sf: ValueError("404")})
    assert receipts.verified(f.text, chain, GUESS) == (0, None)
    f = FakeFetcher(fail={bs: ValueError("x"), sf: ValueError("y")})
    assert receipts.verified(f.text, chain, GUESS) == (None, None)       # どちらも答えない = 分からない


def _run_receipts(cfg, rpc, fetcher):
    chains = receipt_chains(cfg.chains, cfg.root)
    ctx = ReceiptContext(chains=chains, stables={c: stable_addresses(v, cfg.root) for c, v in chains.items()},
                         rpc_factory=lambda chain: rpc, sleep=lambda s: None)
    runner = FeedRunner(cfg.feeds, fetcher=fetcher, coin_chains={8453: "base"}, receipt_ctx=ctx)
    conn = store.connect(cfg.feeds.database_path)
    from farm_radar.feeds.sources import SOURCES
    for s in [x.id for x in SOURCES if x.cadence == "daily" and x.id != "receipts"] + ["venue_checks"]:
        store.finish_run(conn, store.start_run(conn, s, NOW), NOW, "ok")   # ほかの1日1回の一覧は今日読んだことにする
    conn.commit()
    conn.close()
    return runner.run("daily", NOW)


def _bs(addr=GUESS):
    return f"https://base.blockscout.com/api/v2/smart-contracts/{addr}"


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "feeds.sqlite3"
    _feeds_db(path)
    base = load_config()
    return dataclasses.replace(base, database_path=tmp_path / "main.sqlite3",
                               feeds=dataclasses.replace(base.feeds, database_path=path, raw_dir=tmp_path / "raw"))


def test_daily_run_checks_only_bonus_coins_without_a_price_record(cfg):
    rpc = FakeRpc(_vault())
    out = _run_receipts(cfg, rpc, FakeFetcher(texts={_bs(): json.dumps({"is_verified": True})}))
    assert out == [{"source": "receipts", "status": "ok", "items": 1, "new": 0, "gone": 0}]
    assert {c[0] for c in rpc.calls} == {GUESS, BASE_USDC}    # 値段の記録がある RWD は聞かない
    conn = store.connect(cfg.feeds.database_path)
    row = dict(conn.execute("SELECT * FROM receipt_checks").fetchone())
    assert row["is_vault"] == 1 and row["verified"] == 1 and row["verified_by"] == "blockscout"


def test_verified_stable_receipt_gets_the_small_drop_and_a_reason_mark(cfg):
    _run_receipts(cfg, FakeRpc(_vault()), FakeFetcher(texts={_bs(): json.dumps({"is_verified": True})}))
    o = _collect(cfg)["o-guess"]
    codes = {f.code: f for f in o.flags}
    assert "RWD_GUESS" not in codes and codes["RECEIPT"].level == "info"
    for word in ("GUESS", "Test USDC Vault", "中身は USDC", "1枚 = 1.0002 USDC", "チェーンの記録", "Blockscout", "月 −3%"):
        assert word in codes["RECEIPT"].text
    assert "RECEIPT_OFF" not in codes                         # 値段 1.0 と中身 1.0002 の差は 0.02%
    assert o.unprotected == ["ボーナスの預かり証（GUESS）の金庫の損・引き出しの待ち"]
    s = cfg.opportunities
    cau, nor = o.calc["1000"]["cautious"]["no_hedge"], o.calc["1000"]["normal"]["no_hedge"]
    r = opps.receipt_trend(s)
    assert r == pytest.approx(0.97 ** (1 / 30) - 1)
    pool, tvl = cau.split["pool"], 1_000_000.0 * s.cautious_tvl_multiple
    bonus = 500.0 * pool / (tvl + pool) * opps.stay_factor(r, cau.stay_days)
    assert cau.haircut == pytest.approx(bonus * -r) and nor.haircut == 0


def test_unverified_vault_keeps_the_provisional_drop(cfg):
    _run_receipts(cfg, FakeRpc(_vault()), FakeFetcher(texts={_bs(): json.dumps({"is_verified": False})},
                                                     fail={f"{receipts.SOURCIFY}/8453/{GUESS}": ValueError("404")}))
    f = {x.code: x for x in _collect(cfg)["o-guess"].flags}
    assert "RECEIPT" not in f and "契約の中身が公開・確認済みか分からない" in f["RWD_GUESS"].text


def test_vault_of_a_moving_coin_keeps_the_provisional_drop(cfg):
    other = _vault(asset=OTHER)
    other[OTHER] = {"decimals()": ("uint8", 18), "symbol()": ("string", "UP")}
    _run_receipts(cfg, FakeRpc(other), FakeFetcher(texts={_bs(): json.dumps({"is_verified": True})}))
    f = {x.code: x for x in _collect(cfg)["o-guess"].flags}
    assert "RECEIPT" not in f and "中身（UP）がステーブルではない" in f["RWD_GUESS"].text


def test_price_far_from_the_inside_raises_a_notice(cfg):
    # 1枚 = 1.05 USDC なのに、Merkl の値段は 1.0（差 −4.8%。知らせの線 2%）
    _run_receipts(cfg, FakeRpc(_vault(per=1_050_000)), FakeFetcher(texts={_bs(): json.dumps({"is_verified": True})}))
    f = {x.code: x for x in _collect(cfg)["o-guess"].flags}
    assert f["RECEIPT_OFF"].level == "warn" and "-4.8%" in f["RECEIPT_OFF"].text
