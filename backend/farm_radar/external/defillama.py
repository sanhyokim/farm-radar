"""DefiLlama の公開API（認証なし・読み取りだけ）。週1回の候補の会場の一覧に使う（SPEC 5.2.2章）。

- 会場の一覧: https://api.llama.fi/protocols （種類・チェーンごとの預かり額・掲載日・監査の記録）
- プールの利回り: https://yields.llama.fi/pools （ボーナスの利回り apyReward・手数料の利回り apyBase）
- トークン価格: https://coins.llama.fi （現在の価格と、7日の値動き）
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .http import JsonGetter

PROTOCOLS_URL = "https://api.llama.fi/protocols"
YIELDS_URL = "https://yields.llama.fi/pools"
COINS_BASE = "https://coins.llama.fi"
_BATCH = 40     # トークン価格を一度に聞く数


class DefiLlama:
    def __init__(self, getter: JsonGetter | None = None):
        # 会場の一覧は約10MB、利回りは約12MBあるので、待ち時間を長めにする
        self.http = getter or JsonGetter(COINS_BASE, min_interval=1.0, backoff_seconds=10.0, timeout=90.0)

    def protocols(self) -> list[dict[str, Any]]:
        data = self.http.get(PROTOCOLS_URL)
        return data if isinstance(data, list) else []

    def yield_pools(self) -> list[dict[str, Any]]:
        data = self.http.get(YIELDS_URL)
        return (data or {}).get("data") or [] if isinstance(data, dict) else []

    def token_info(self, coins: Iterable[str]) -> dict[str, dict[str, Any]]:
        """"<chain>:<address>" ごとの {symbol, price, change_7d_pct}。わからないものは入らない。"""
        keys = sorted({c.lower() for c in coins if c and ":" in c})
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), _BATCH):
            chunk = ",".join(keys[i:i + _BATCH])
            prices = (self.http.get(f"prices/current/{chunk}") or {}).get("coins") or {}
            changes = (self.http.get(f"percentage/{chunk}", {"period": "1w", "lookForward": "false"}) or {}
                       ).get("coins") or {}
            for k, v in prices.items():
                out.setdefault(k.lower(), {}).update({"symbol": v.get("symbol"), "price": v.get("price")})
            for k, v in changes.items():
                out.setdefault(k.lower(), {})["change_7d_pct"] = v
        return out
