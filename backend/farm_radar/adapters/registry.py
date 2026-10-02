"""読み方の名前 → アダプター（読み方）の対応表（N1）。

会場のファイル（venues/<id>.yaml）の `reader:` で読み方を選ぶ。同じ型の会場を足すときは、コードはそのままで
会場のファイルだけを足せばよい。新しい型の会場のときだけ、ここに1行と読み方のコードを足す（N3）。
"""

from __future__ import annotations

from typing import Any

from ..rpc.client import RpcClient
from .alandale_robinhood import AlandaleRobinhoodAdapter
from .base import VenueAdapter
from .up_robinhood import UpRobinhoodAdapter

# 読み方の名前: (アダプター, 系統)
READERS: dict[str, tuple[type, str]] = {
    "ve33_cl_gauge": (UpRobinhoodAdapter, "evm"),                    # ve(3,3) の集中流動性とゲージ（up.）
    "algebra_weekly_rewarder": (AlandaleRobinhoodAdapter, "evm"),    # Algebra の集中流動性と週の量（Alandale。M6）
}

# 昔の会場のファイル（reader: が無い）のための対応（会場 id → 読み方の名前）
_LEGACY = {"up-robinhood": "ve33_cl_gauge", "alandale-robinhood": "algebra_weekly_rewarder"}


def reader_name(venue: dict[str, Any]) -> str | None:
    return venue.get("reader") or _LEGACY.get(venue.get("id", ""))


def build_adapter(venue: dict[str, Any], rpc: RpcClient) -> VenueAdapter:
    name = reader_name(venue)
    if name not in READERS:
        raise ValueError(f"会場 {venue['id']} の読み方 {name!r} がありません。")
    cls, family = READERS[name]
    chain_family = (venue.get("chain") or {}).get("family", "evm")
    if chain_family != family:
        raise ValueError(f"会場 {venue['id']} の読み方 {name} は {family} 用ですが、チェーンは {chain_family} です。")
    return cls(venue, rpc)
