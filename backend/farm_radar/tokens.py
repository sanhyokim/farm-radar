"""トークンの分類（venues/tokens-<chain>.yaml）を読む。

どれも公式の情報で確認できたものだけ（作り方は farm_radar/tools/refresh_tokens.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import REPO_ROOT


@dataclass(frozen=True)
class PerpRef:
    venue: str
    symbol: str
    market_id: int


@dataclass(frozen=True)
class TokenBook:
    stablecoins: frozenset[str] = frozenset()        # 小文字のアドレス
    stock_tokens: dict[str, str] = field(default_factory=dict)   # アドレス → ティッカー
    perps: dict[str, PerpRef] = field(default_factory=dict)      # アドレス → ヘッジに使える perp
    wrapped_native: str | None = None                            # WETH（ガス代をドルに直すのに使う）

    def is_stable(self, token: str) -> bool:
        return token.lower() in self.stablecoins

    def is_stock(self, token: str) -> bool:
        return token.lower() in self.stock_tokens

    def perp_for(self, token: str) -> PerpRef | None:
        return self.perps.get(token.lower())


def load_tokens(chain: str = "robinhood", root: Path = REPO_ROOT) -> TokenBook:
    path = root / "venues" / f"tokens-{chain}.yaml"
    if not path.exists():
        return TokenBook()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stables = frozenset(v["address"].lower() for v in (raw.get("stablecoins") or {}).values())
    stock = {a.lower(): s for a, s in ((raw.get("stock_tokens") or {}).get("tokens") or {}).items()}
    perps = {
        a.lower(): PerpRef(m["venue"], m["symbol"], int(m["market_id"]))
        for a, m in ((raw.get("perps") or {}).get("map") or {}).items()
    }
    wn = next(iter((raw.get("wrapped_native") or {}).values()), None)
    return TokenBook(stables, stock, perps, wn["address"].lower() if wn else None)
