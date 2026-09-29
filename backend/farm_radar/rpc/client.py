"""読み取り専用の JSON-RPC クライアント。

- 使えるメソッドは READ_ONLY_METHODS だけ。取引の送信や署名のメソッドは呼べない。
- RPC の優先順: Alchemy（.env に ALCHEMY_API_KEY があるとき）→ 公式の公開RPC → 予備RPC
- 1つのRPCで再試行してもだめなら、次のRPCに切り替える。
- 固定ブロックを指定した読み取りは結果が変わらないのでキャッシュする。
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import RpcSettings
from ..logging_setup import redact

log = logging.getLogger(__name__)

READ_ONLY_METHODS = frozenset({
    "eth_chainId",
    "eth_blockNumber",
    "eth_getBlockByNumber",
    "eth_call",
    "eth_getLogs",
    "eth_getCode",
    "eth_getStorageAt",
    "eth_gasPrice",
})

# 一時的な失敗として再試行する JSON-RPC エラーコード（回数制限・サーバー内部の一時エラー）
_RETRYABLE_RPC_CODES = {-32005, -32603, 429}
_MOVING_BLOCK_TAGS = {"latest", "pending", "safe", "finalized"}


class RpcError(Exception):
    """すべてのRPCで失敗した。"""


class RpcCallError(Exception):
    """RPC が明確なエラーを返した（例: コントラクトの呼び出しが revert）。再試行しても同じ結果になる。"""

    def __init__(self, code: int | None, message: str, data: Any = None):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class Endpoint:
    name: str        # ログに出す名前（URLはキーを含むことがあるので出さない）
    url: str


def build_endpoints(chain: Mapping[str, Any], settings: RpcSettings, env: Mapping[str, str]) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    key = (env.get("ALCHEMY_API_KEY") or "").strip()
    if key and chain.get("alchemy_rpc"):
        endpoints.append(Endpoint("alchemy", chain["alchemy_rpc"].replace("{API_KEY}", key)))
    if chain.get("public_rpc"):
        endpoints.append(Endpoint("public", chain["public_rpc"]))
    for i, url in enumerate(settings.extra_urls, start=1):
        endpoints.append(Endpoint(f"extra{i}", url))
    if not endpoints:
        raise ValueError("使えるRPCがありません。venues の chain.public_rpc を確認してください。")
    return endpoints


class RpcClient:
    def __init__(
        self,
        endpoints: list[Endpoint],
        settings: RpcSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        cache_size: int = 5000,
    ):
        self.endpoints = endpoints
        self.settings = settings
        # 公開RPCは User-Agent によっては 403 を返すので、名乗りを付ける
        self._http = httpx.Client(timeout=settings.timeout_seconds, transport=transport,
                                  headers={"User-Agent": "farm-radar/0.1 (read-only)"})
        self._sleep = sleep
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float | None, Any, str]] = OrderedDict()
        self._cache_size = cache_size
        self._next_id = 0
        self.last_endpoint: str | None = None

    def close(self) -> None:
        self._http.close()

    # --- 基本 ---
    def request(self, method: str, params: list[Any]) -> Any:
        if method not in READ_ONLY_METHODS:
            raise PermissionError(f"{method} は読み取り専用クライアントでは使えません。")

        key = json.dumps([method, params], sort_keys=True)
        cached = self._cache.get(key)
        if cached is not None:
            expires, value, endpoint = cached
            if expires is None or self._clock() < expires:
                self._cache.move_to_end(key)
                self.last_endpoint = endpoint
                return value

        errors: list[str] = []
        for ep in self.endpoints:
            try:
                value = self._request_with_retry(ep, method, params)
            except RpcCallError:
                raise
            except Exception as exc:  # 通信エラー・回数制限など → 次のRPCへ
                errors.append(f"{ep.name}: {redact(str(exc))}")
                log.warning("rpc endpoint failed, switching", extra={"data": {"endpoint": ep.name, "method": method, "error": redact(str(exc))}})
                continue
            self.last_endpoint = ep.name
            self._store(key, method, params, value, ep.name)
            return value
        raise RpcError(f"すべてのRPCで失敗しました（{method}）: " + " / ".join(errors))

    def _request_with_retry(self, ep: Endpoint, method: str, params: list[Any]) -> Any:
        attempts = self.settings.max_retries + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._post(ep, method, params)
            except RpcCallError as exc:
                if exc.code not in _RETRYABLE_RPC_CODES:
                    raise
                last = exc
            except (httpx.HTTPError, _Transient) as exc:
                last = exc
            if attempt < attempts - 1:
                self._sleep(self.settings.backoff_seconds * (2 ** attempt))
        raise _Transient(str(last))

    def _post(self, ep: Endpoint, method: str, params: list[Any]) -> Any:
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        resp = self._http.post(ep.url, json=body)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Transient(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload and payload["error"] is not None:
            err = payload["error"]
            raise RpcCallError(err.get("code"), err.get("message", ""), err.get("data"))
        return payload["result"]

    def _store(self, key: str, method: str, params: list[Any], value: Any, endpoint: str) -> None:
        if method in ("eth_blockNumber", "eth_gasPrice"):
            expires: float | None = self._clock() + self.settings.latest_cache_seconds
        elif _refers_to_moving_block(params):
            expires = self._clock() + self.settings.latest_cache_seconds
        else:
            expires = None
        self._cache[key] = (expires, value, endpoint)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # --- よく使う呼び出し ---
    def chain_id(self) -> int:
        return int(self.request("eth_chainId", []), 16)

    def gas_price(self) -> int:
        """今のガス価格（wei）。"""
        return int(self.request("eth_gasPrice", []), 16)

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str, block: int | str) -> str:
        return self.request("eth_call", [{"to": to, "data": data}, _block_param(block)])

    def get_code(self, address: str, block: int | str = "latest") -> str:
        return self.request("eth_getCode", [address, _block_param(block)])

    def get_storage(self, address: str, slot: str, block: int | str = "latest") -> str:
        return self.request("eth_getStorageAt", [address, slot, _block_param(block)])

    def get_logs(
        self, address: str, topics: list[Any], from_block: int, to_block: int, *, chunk: int = 10_000,
    ) -> list[dict[str, Any]]:
        """ブロック範囲を分割してログを取る。範囲が広すぎると言われたら半分にして再試行する。"""
        logs: list[dict[str, Any]] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            try:
                logs += self.request("eth_getLogs", [{
                    "address": address, "topics": topics,
                    "fromBlock": hex(start), "toBlock": hex(end),
                }])
            except (RpcCallError, RpcError):
                if chunk <= 1:
                    raise
                chunk = max(1, chunk // 2)
                continue
            start = end + 1
        return logs


class _Transient(Exception):
    pass


def _block_param(block: int | str) -> str:
    return hex(block) if isinstance(block, int) else block


def _refers_to_moving_block(params: list[Any]) -> bool:
    for p in params:
        if isinstance(p, str) and p in _MOVING_BLOCK_TAGS:
            return True
        if isinstance(p, dict) and any(v in _MOVING_BLOCK_TAGS for v in p.values() if isinstance(v, str)):
            return True
    return False
