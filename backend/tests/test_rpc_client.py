import httpx
import pytest

from farm_radar.config import RpcSettings
from farm_radar.logging_setup import redact
from farm_radar.rpc.client import Endpoint, RpcCallError, RpcClient, RpcError, build_endpoints

CHAIN = {"public_rpc": "https://public", "alchemy_rpc": "https://alchemy/v2/{API_KEY}"}


def make(node, settings, eps=None):
    eps = eps or [Endpoint("public", "https://public/"), Endpoint("extra1", "https://extra/")]
    return RpcClient(eps, settings, transport=node.transport(), sleep=lambda s: None)


def test_alchemy_first_when_key_set(settings):
    eps = build_endpoints(CHAIN, RpcSettings(extra_urls=("https://x",)), {"ALCHEMY_API_KEY": "k" * 20})
    assert [e.name for e in eps] == ["alchemy", "public", "extra1"]
    assert eps[0].url == "https://alchemy/v2/" + "k" * 20


def test_public_when_no_key(settings):
    eps = build_endpoints(CHAIN, RpcSettings(), {"ALCHEMY_API_KEY": "  "})
    assert [e.name for e in eps] == ["public"]


def test_write_methods_are_refused(node, settings):
    c = make(node, settings)
    for m in ["eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign", "eth_signTypedData_v4"]:
        with pytest.raises(PermissionError):
            c.request(m, [])
    assert node.calls == []


def test_retry_then_success(node, settings):
    n = {"i": 0}

    def flaky(body):
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else {"result": "0x10"}

    node.on("https://public/", flaky)
    assert make(node, settings).block_number() == 16
    assert n["i"] == 3


def test_failover_to_backup(node, settings):
    node.on("https://public/", lambda b: httpx.Response(429))
    node.on("https://extra/", lambda b: {"result": "0x1237"})
    c = make(node, settings)
    assert c.chain_id() == 0x1237
    assert c.last_endpoint == "extra1"


def test_all_fail(node, settings):
    node.on("https://public/", lambda b: httpx.Response(500))
    node.on("https://extra/", lambda b: httpx.Response(500))
    with pytest.raises(RpcError):
        make(node, settings).block_number()


def test_revert_is_not_retried(node, settings):
    node.on("https://public/", lambda b: {"error": {"code": 3, "message": "execution reverted"}})
    c = make(node, settings)
    with pytest.raises(RpcCallError):
        c.eth_call("0x" + "11" * 20, "0x", 100)
    assert len(node.calls) == 1


def test_fixed_block_calls_are_cached(node, settings):
    node.on("https://public/", lambda b: {"result": "0xab"})
    c = make(node, settings)
    c.eth_call("0x" + "11" * 20, "0x01", 100)
    c.eth_call("0x" + "11" * 20, "0x01", 100)
    assert len(node.calls) == 1


def test_latest_cache_expires(node, settings):
    node.on("https://public/", lambda b: {"result": "0x1"})
    t = {"now": 0.0}
    c = RpcClient([Endpoint("public", "https://public/")], settings, transport=node.transport(),
                  sleep=lambda s: None, clock=lambda: t["now"])
    c.block_number(); c.block_number()
    assert len(node.calls) == 1
    t["now"] = 10.0
    c.block_number()
    assert len(node.calls) == 2


def test_get_logs_halves_range_on_error(node, settings):
    def logs(body):
        f = body["params"][0]
        span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        if span > 500:
            return {"error": {"code": -32000, "message": "block range too large"}}
        return {"result": [{"blockNumber": f["fromBlock"]}]}

    node.on("https://public/", logs)
    out = make(node, settings, [Endpoint("public", "https://public/")]).get_logs("0x" + "22" * 20, [], 0, 1999, chunk=2000)
    assert len(out) == 4  # 2000 → 1000 → 500 ずつ4回


def test_api_key_redacted_in_logs():
    assert "secretkey123" not in redact("https://robinhood-mainnet.g.alchemy.com/v2/secretkey123 failed")
