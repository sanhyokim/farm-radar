from eth_abi import decode, encode

from farm_radar.rpc.abi import encode_call, selector
from farm_radar.rpc.client import Endpoint, RpcClient
from farm_radar.rpc.multicall import AGGREGATE3, Call, Multicall

MC = "0x" + "ca" * 20
POOL = "0x" + "11" * 20


def test_known_selectors():
    # 既知の値と照合（Uniswap v3 / Multicall3）
    assert selector("slot0()").hex() == "3850c7bd"
    assert selector("liquidity()").hex() == "1a686502"
    assert selector(AGGREGATE3).hex() == "82ad56cb"


def test_aggregate3_roundtrip(node, settings):
    def handle(body):
        method, params = body["method"], body["params"]
        if method == "eth_getCode":
            return {"result": "0x6080"}
        data = bytes.fromhex(params[0]["data"][2:])
        assert data[:4] == selector(AGGREGATE3)
        (calls,) = decode(["(address,bool,bytes)[]"], data[4:])
        out = [(True, encode(["uint256"], [i + 7])) for i, _ in enumerate(calls)]
        return {"result": "0x" + encode(["(bool,bytes)[]"], [out]).hex()}

    node.on("https://public/", handle)
    rpc = RpcClient([Endpoint("public", "https://public/")], settings, transport=node.transport(), sleep=lambda s: None)
    mc = Multicall(rpc, MC, batch_size=2)
    calls = [Call(POOL, encode_call("liquidity()")) for _ in range(3)]
    results, raw = mc.call(calls, 123)
    assert [decode(["uint256"], r.data)[0] for r in results] == [7, 8, 7]
    assert len(raw) == 2  # 2件ずつにまとめて2回
    eth_calls = [c for c in node.calls if c[1] == "eth_call"]
    assert all(c[2][1] == hex(123) for c in eth_calls)  # 指定したブロックで読んでいる


def test_falls_back_when_no_code(node, settings):
    def handle(body):
        if body["method"] == "eth_getCode":
            return {"result": "0x"}
        return {"result": "0x" + encode(["uint256"], [5]).hex()}

    node.on("https://public/", handle)
    rpc = RpcClient([Endpoint("public", "https://public/")], settings, transport=node.transport(), sleep=lambda s: None)
    mc = Multicall(rpc, MC)
    results, raw = mc.call([Call(POOL, encode_call("liquidity()"))], 1)
    assert mc.address is None
    assert results[0].success and decode(["uint256"], results[0].data)[0] == 5


def test_no_address_uses_sequential(node, settings):
    node.on("https://public/", lambda b: {"error": {"code": 3, "message": "execution reverted"}})
    rpc = RpcClient([Endpoint("public", "https://public/")], settings, transport=node.transport(), sleep=lambda s: None)
    results, raw = Multicall(rpc, None).call([Call(POOL, b"\x00\x00\x00\x00")], 1)
    assert not results[0].success
    assert raw[0]["response"]["code"] == 3
