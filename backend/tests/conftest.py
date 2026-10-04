import json

import httpx
import pytest

from farm_radar import ratelimit
from farm_radar.config import RpcSettings


@pytest.fixture(autouse=True)
def _no_better_place(request, monkeypatch):
    """段階4（もっと良い場所へ。N4b）は、試験用の記録の極端な利回りのプールで、ほかの決まりのテストの建玉を閉じてしまう。
    段階4を確かめるテスト（@pytest.mark.stage4）のほかは止めておく。"""
    if request.node.get_closest_marker("stage4") is None:
        from farm_radar.execution import risk_job
        monkeypatch.setattr(risk_job, "better_place", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_workspace_lighter(monkeypatch):
    """テストの設定は作業場所の data/feeds.sqlite3 を指すので、Lighter の証拠金の割合を読まない（仮の値 5% で計算する）。"""
    from farm_radar.execution import hedge_guard
    monkeypatch.setattr(hedge_guard, "lighter_margin_table", lambda path, rh=False: {})


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
