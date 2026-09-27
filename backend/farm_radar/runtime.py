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


def build(config: Config | None = None, *, check_chain: bool = True) -> tuple[Config, list[VenueRuntime]]:
    config = config or load_config()
    conn = db.connect(config.database_path)
    runtimes: list[VenueRuntime] = []
    for venue_id in config.venues:
        venue = load_venue(venue_id, config.root)
        db.upsert_venue(conn, venue)
        endpoints = build_endpoints(venue["chain"], config.rpc, os.environ)
        rpc = RpcClient(endpoints, config.rpc)
        if check_chain:
            got = rpc.chain_id()
            if got != venue["chain"]["chain_id"]:
                raise ConfigError(f"RPCのチェーンID {got} が {venue_id} の {venue['chain']['chain_id']} と違います。")
        log.info("venue ready", extra={"data": {"venue": venue_id, "rpc_order": [e.name for e in endpoints]}})
        runtimes.append(VenueRuntime(venue, rpc, build_adapter(venue, rpc)))
    conn.commit()
    conn.close()
    return config, runtimes
