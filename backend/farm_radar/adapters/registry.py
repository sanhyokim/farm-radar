"""会場ID → アダプターの対応表。会場を増やすときはここに1行足す（M6）。"""

from __future__ import annotations

from typing import Any

from ..rpc.client import RpcClient
from .base import VenueAdapter
from .up_robinhood import UpRobinhoodAdapter

ADAPTERS = {
    "up-robinhood": UpRobinhoodAdapter,
}


def build_adapter(venue: dict[str, Any], rpc: RpcClient) -> VenueAdapter:
    try:
        cls = ADAPTERS[venue["id"]]
    except KeyError:
        raise ValueError(f"会場 {venue['id']} のアダプターがありません。") from None
    return cls(venue, rpc)
