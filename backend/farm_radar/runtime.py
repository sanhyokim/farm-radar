"""起動時の準備（設定の読み込み、RPCとアダプターの組み立て）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .adapters.base import VenueAdapter
from .adapters.registry import build_adapter
from .config import Config, ConfigError, load_config, load_venue
from .db import database as db
from .rpc.client import RpcClient, build_endpoints

log = logging.getLogger(__name__)


@dataclass
class VenueRuntime:
    venue: dict
    rpc: RpcClient
    adapter: VenueAdapter
    chain_verified: bool = False

    def ensure_chain(self) -> None:
        """RPCが正しいチェーンにつながっているか確認する（最初に成功するまで毎回試す）。

        起動直後はネットがまだつながっていないことがある（スリープ復帰・再起動の直後など）。
        そのときは起動を止めずに、次の収集のときにもう一度確認する。
        """
        if self.chain_verified:
            return
        got = self.rpc.chain_id()
        want = self.venue["chain"]["chain_id"]
        if got != want:
            raise ConfigError(f"RPCのチェーンID {got} が {self.venue['id']} の {want} と違います。")
        self.chain_verified = True


def build(config: Config | None = None) -> tuple[Config, list[VenueRuntime]]:
    config = config or load_config()
    conn = db.connect(config.database_path)
    runtimes: list[VenueRuntime] = []
    # 同じチェーンの会場は、RPC の窓口を1つだけ使う（呼び出しの間隔と、読み取りの記憶を共有する。M6）
    rpcs: dict[tuple, RpcClient] = {}
    for venue_id in config.venues:
        venue = load_venue(venue_id, config.root)
        db.upsert_venue(conn, venue)
        endpoints = build_endpoints(venue["chain"], config.rpc, os.environ)
        key = (venue["chain"].get("chain_id"), tuple(e.url for e in endpoints))
        rpc = rpcs.get(key) or rpcs.setdefault(key, RpcClient(endpoints, config.rpc))
        log.info("venue ready", extra={"data": {"venue": venue_id, "rpc_order": [e.name for e in endpoints]}})
        runtimes.append(VenueRuntime(venue, rpc, build_adapter(venue, rpc)))
    conn.commit()
    conn.close()
    return config, runtimes
