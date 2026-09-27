"""Multicall3 で複数の読み取りを1回のRPC呼び出しにまとめる。

Multicall3 のアドレスが確認できていない（または、そのアドレスにコードがない）ときは、
1件ずつ eth_call する方式に自動で切り替える。結果は同じで、RPCの呼び出し回数だけが増える。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .abi import decode_result, encode_call
from .client import RpcCallError, RpcClient

log = logging.getLogger(__name__)

AGGREGATE3 = "aggregate3((address,bool,bytes)[])"


@dataclass(frozen=True)
class Call:
    target: str
    data: bytes


@dataclass(frozen=True)
class CallResult:
    success: bool
    data: bytes


class Multicall:
    def __init__(self, rpc: RpcClient, address: str | None, *, batch_size: int = 100):
        self.rpc = rpc
        self.address = address
        self.batch_size = batch_size
        self._checked = False

    @property
    def enabled(self) -> bool:
        if self.address and not self._checked:
            self._checked = True
            code = self.rpc.get_code(self.address)
            if not code or code in ("0x", "0x0"):
                log.warning("multicall3 has no code at configured address; falling back", extra={"data": {"address": self.address}})
                self.address = None
        return self.address is not None

    def call(self, calls: Sequence[Call], block: int) -> tuple[list[CallResult], list[dict]]:
        """結果と、生データ（要求と応答）を返す。"""
        if not calls:
            return [], []
        if self.enabled:
            return self._aggregate(calls, block)
        return self._sequential(calls, block)

    def _aggregate(self, calls: Sequence[Call], block: int) -> tuple[list[CallResult], list[dict]]:
        results: list[CallResult] = []
        raw: list[dict] = []
        for i in range(0, len(calls), self.batch_size):
            chunk = calls[i : i + self.batch_size]
            data = encode_call(AGGREGATE3, ["(address,bool,bytes)[]"], [[(c.target, True, c.data) for c in chunk]])
            hex_data = "0x" + data.hex()
            resp = self.rpc.eth_call(self.address, hex_data, block)
            (decoded,) = decode_result(["(bool,bytes)[]"], resp)
            results += [CallResult(bool(ok), bytes(ret)) for ok, ret in decoded]
            raw.append({"method": "eth_call", "to": self.address, "block": block,
                        "calls": [{"target": c.target, "data": "0x" + c.data.hex()} for c in chunk],
                        "response": resp})
        return results, raw

    def _sequential(self, calls: Sequence[Call], block: int) -> tuple[list[CallResult], list[dict]]:
        results: list[CallResult] = []
        raw: list[dict] = []
        for c in calls:
            hex_data = "0x" + c.data.hex()
            try:
                resp = self.rpc.eth_call(c.target, hex_data, block)
                results.append(CallResult(True, bytes.fromhex(resp.removeprefix("0x"))))
            except RpcCallError as exc:
                resp = {"error": exc.message, "code": exc.code}
                results.append(CallResult(False, b""))
            raw.append({"method": "eth_call", "to": c.target, "data": hex_data, "block": block, "response": resp})
        return results, raw
