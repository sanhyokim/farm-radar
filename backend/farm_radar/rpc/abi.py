"""ABI のエンコード・デコードの小さな道具。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from eth_abi import decode, encode
from eth_hash.auto import keccak


def selector(signature: str) -> bytes:
    """関数のシグネチャ（例: "slot0()"）から先頭4バイトを作る。"""
    return keccak(signature.encode())[:4]


def topic(signature: str) -> str:
    """イベントのシグネチャからログの topic0 を作る。"""
    return "0x" + keccak(signature.encode()).hex()


def encode_call(signature: str, types: Sequence[str] = (), args: Sequence[Any] = ()) -> bytes:
    return selector(signature) + (encode(list(types), list(args)) if types else b"")


def decode_result(types: Sequence[str], data: bytes | str) -> tuple[Any, ...]:
    if isinstance(data, str):
        data = bytes.fromhex(data.removeprefix("0x"))
    return decode(list(types), data)
