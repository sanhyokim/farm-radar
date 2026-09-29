import json

import httpx
import pytest

from farm_radar import ratelimit
from farm_radar.config import RpcSettings


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    ratelimit.reset()
    ratelimit.set_sink(None)
    yield
    ratelimit.set_sink(None)


class FakeNode:
    """httpx.MockTransport 用の偽RPCノード。URLごとに振る舞いを決められる。"""

    def __init__(self):
        self.handlers = {}
        self.calls = []

    def on(self, url, fn):
        self.handlers[url] = fn

    def transport(self):
        def handle(request: httpx.Request):
            body = json.loads(request.content)
            url = str(request.url)
            self.calls.append((url, body["method"], body["params"]))
            out = self.handlers[url](body)
            if isinstance(out, httpx.Response):
                return out
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **out})
        return httpx.MockTransport(handle)


@pytest.fixture
def node():
    return FakeNode()


@pytest.fixture
def settings():
    return RpcSettings(max_retries=2, backoff_seconds=0.01, timeout_seconds=1, latest_cache_seconds=5)
