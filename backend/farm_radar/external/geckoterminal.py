"""GeckoTerminal（外部の価格サイト）の公開API。認証なし、1分あたり30回まで。

使いみち（SPEC 3.1章・5.3章）:
- プールの24時間の出来高と、プールに置かれている額（TVL）
- 自分の記録が7日分たまるまでの、1時間足の価格（値動き σ の計算を補う。2026-09-29 オーナー承認）
- 報酬トークンの24時間の出来高（売り圧の目安）
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .http import JsonGetter

BASE_URL = "https://api.geckoterminal.com/api/v2"
_BATCH = 30   # multi エンドポイントで一度に聞ける数


@dataclass(frozen=True)
class PoolMarket:
    address: str
    reserve_usd: float | None        # プールに置かれている額（ドル）
    volume_24h_usd: float | None
    base_token: str | None
    quote_token: str | None


@dataclass(frozen=True)
class TokenMarket:
    address: str
    price_usd: float | None
    volume_24h_usd: float | None
    top_pool: str | None             # そのトークンでいちばん大きいプール（どのDEXでも）


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _addr_from_id(rel: dict | None) -> str | None:
    data = (rel or {}).get("data") or {}
    ident = data.get("id") or ""
    return ident.split("_", 1)[1].lower() if "_" in ident else None


class GeckoTerminal:
    def __init__(self, network: str, getter: JsonGetter | None = None):
        self.network = network
        # 公開APIの回数制限は公称1分30回だが、実際はもっと早く 429 が返ることがあるので、6秒に1回にする
        self.http = getter or JsonGetter(BASE_URL, min_interval=6.0, backoff_seconds=20.0)

    def pools(self, addresses: Iterable[str]) -> dict[str, PoolMarket]:
        out: dict[str, PoolMarket] = {}
        addrs = sorted({a.lower() for a in addresses})
        for i in range(0, len(addrs), _BATCH):
            chunk = addrs[i:i + _BATCH]
            data = self.http.get(f"networks/{self.network}/pools/multi/{','.join(chunk)}")
            for p in data.get("data") or []:
                a = p.get("attributes") or {}
                rel = p.get("relationships") or {}
                addr = (a.get("address") or "").lower()
                out[addr] = PoolMarket(
                    address=addr,
                    reserve_usd=_f(a.get("reserve_in_usd")),
                    volume_24h_usd=_f((a.get("volume_usd") or {}).get("h24")),
                    base_token=_addr_from_id(rel.get("base_token")),
                    quote_token=_addr_from_id(rel.get("quote_token")),
                )
        return out

    def tokens(self, addresses: Iterable[str]) -> dict[str, TokenMarket]:
        out: dict[str, TokenMarket] = {}
        addrs = sorted({a.lower() for a in addresses})
        for i in range(0, len(addrs), _BATCH):
            chunk = addrs[i:i + _BATCH]
            data = self.http.get(f"networks/{self.network}/tokens/multi/{','.join(chunk)}")
            for t in data.get("data") or []:
                a = t.get("attributes") or {}
                top = ((t.get("relationships") or {}).get("top_pools") or {}).get("data") or []
                top_pool = top[0]["id"].split("_", 1)[1].lower() if top and "_" in top[0].get("id", "") else None
                addr = (a.get("address") or "").lower()
                out[addr] = TokenMarket(
                    address=addr,
                    price_usd=_f(a.get("price_usd")),
                    volume_24h_usd=_f((a.get("volume_usd") or {}).get("h24")),
                    top_pool=top_pool,
                )
        return out

    def hourly_usd(self, pool: str, token: str, limit: int = 169) -> list[tuple[int, float]]:
        """そのプールでの、トークンのドル建て1時間足の終値。古い順の (UNIX秒, 価格)。

        取引がなかった時間の足は返ってこない（使う側で直前の値を引き継ぐ）。
        """
        data = self.http.get(
            f"networks/{self.network}/pools/{pool.lower()}/ohlcv/hour",
            {"aggregate": 1, "limit": limit, "currency": "usd", "token": token.lower()},
        )
        rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = [(int(r[0]), float(r[4])) for r in rows if r and r[4] is not None and float(r[4]) > 0]
        return sorted(out)

    def dexes(self) -> list[tuple[str, str]]:
        """このネットワークの DEX の一覧（id, 名前）。週1回の候補の一覧で、新しい DEX を見つけるのに使う（SPEC 5.2.2章）。"""
        out: list[tuple[str, str]] = []
        for page in range(1, 6):
            data = self.http.get(f"networks/{self.network}/dexes", {"page": page})
            rows = data.get("data") or []
            out += [(d["id"], (d.get("attributes") or {}).get("name") or d["id"]) for d in rows if d.get("id")]
            if not (data.get("links") or {}).get("next") or not rows:
                break
        return out

    def dex_pools(self, dex_id: str) -> list[dict]:
        """その DEX の、取引量の多いプール（1ページ目だけ）。名前・預かり額・24時間の取引量・作られた日。"""
        data = self.http.get(f"networks/{self.network}/dexes/{dex_id}/pools",
                             {"page": 1, "sort": "h24_volume_usd_desc"})
        out = []
        for p in data.get("data") or []:
            a = p.get("attributes") or {}
            out.append({"address": (a.get("address") or "").lower(), "name": a.get("name"),
                        "tvl_usd": _f(a.get("reserve_in_usd")),
                        "volume_24h_usd": _f((a.get("volume_usd") or {}).get("h24")),
                        "created_at": a.get("pool_created_at")})
        return out
