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
    perps: dict[str, PerpRef] = field(default_factory=dict)      # アドレス → ヘッジに使える perp（1つ目の候補）
    wrapped_native: str | None = None                            # WETH（ガス代をドルに直すのに使う）
    perp_alts: dict[str, tuple[PerpRef, ...]] = field(default_factory=dict)  # アドレス → ヘッジ先ごとの候補すべて

    def is_stable(self, token: str) -> bool:
        return token.lower() in self.stablecoins

    def is_stock(self, token: str) -> bool:
        return token.lower() in self.stock_tokens

    def perp_for(self, token: str) -> PerpRef | None:
        return self.perps.get(token.lower())

    def perp_candidates(self, token: str) -> tuple[PerpRef, ...]:
        """使えるヘッジ先の候補すべて（ヘッジ先を比べて一番安いところを選ぶのに使う。SPEC 5.2.1章）。"""
        t = token.lower()
        return self.perp_alts.get(t) or ((self.perps[t],) if t in self.perps else ())


def load_tokens(chain: str = "robinhood", root: Path = REPO_ROOT) -> TokenBook:
    path = root / "venues" / f"tokens-{chain}.yaml"
    if not path.exists():
        return TokenBook()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stables = frozenset(v["address"].lower() for v in (raw.get("stablecoins") or {}).values())
    stock = {a.lower(): s for a, s in ((raw.get("stock_tokens") or {}).get("tokens") or {}).items()}
    # 1つのトークンに、ヘッジ先ごとの候補を並べて書ける（1つだけなら辞書、いくつもならリスト）
    alts: dict[str, tuple[PerpRef, ...]] = {}
    for a, m in ((raw.get("perps") or {}).get("map") or {}).items():
        items = m if isinstance(m, list) else [m]
        alts[a.lower()] = tuple(PerpRef(x["venue"], x["symbol"], int(x["market_id"])) for x in items)
    perps = {a: refs[0] for a, refs in alts.items() if refs}
    wn = next(iter((raw.get("wrapped_native") or {}).values()), None)
    return TokenBook(stables, stock, perps, wn["address"].lower() if wn else None, alts)
