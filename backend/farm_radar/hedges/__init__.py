"""ヘッジ先アダプターの一覧（SPEC 5.2.1章）。新しいヘッジ先は、このフォルダーに1ファイル足して REGISTRY に1行加え、
config.yaml の hedge_venues に名前を書くだけで使える。"""

from __future__ import annotations

from typing import Any

from .base import HedgeAdapter
from .lighter import LighterHedge, LighterRhHedge

__all__ = ["REGISTRY", "LighterHedge", "LighterRhHedge", "build", "wrap"]

REGISTRY: dict[str, Any] = {"lighter": LighterHedge, "lighter_rh": LighterRhHedge}


def build(names: list[str] | tuple[str, ...], clients: dict[str, Any] | None = None) -> dict[str, HedgeAdapter]:
    """有効なヘッジ先のアダプターを作る。clients に既存のクライアント（テスト用の偽物など）を渡せる。"""
    clients = clients or {}
    out: dict[str, HedgeAdapter] = {}
    for n in names:
        cls = REGISTRY.get(n)
        if cls is None:
            continue
        out[n] = cls(clients[n]) if n in clients else cls()
    return out


def wrap(obj: Any) -> HedgeAdapter | None:
    """古い呼び方（lighter=Lighter()）を、アダプターに包む。すでにアダプターならそのまま。"""
    if obj is None:
        return None
    if hasattr(obj, "hedge_id"):
        return obj
    return LighterHedge(obj)
