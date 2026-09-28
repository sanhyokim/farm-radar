"""オンチェーンのプール価格から、各トークンのドル価格を求める（SPEC 5.3章: オンチェーンの価格を優先）。

考え方:
- ステーブルコイン（venues/tokens-*.yaml で確認済みのもの）を $1 とする。
- 「値段が分かっているトークン」と組になっているプールから、相手のトークンの値段が分かる。
- 同じトークンに複数の道があるときは、置かれている額（今の価格の近くの流動性をドルに直したもの）が
  いちばん大きいプールを先に使う。薄いプールのおかしな価格に引っぱられないようにするため。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Q96 = 2 ** 96


@dataclass(frozen=True)
class PoolPrice:
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    price: float                  # token1 / token0（小数点調整済み）
    liquidity: int                # 今の価格のところにある流動性（プール全体）
    sqrt_price_x96: int


def virtual_amounts(p: PoolPrice) -> tuple[float, float]:
    """今の価格のところにある流動性を、トークンの量（小数点調整済み）に直す。深さの目安に使う。"""
    sp = p.sqrt_price_x96 / Q96
    if sp <= 0:
        return 0.0, 0.0
    x = p.liquidity / sp / 10 ** p.decimals0
    y = p.liquidity * sp / 10 ** p.decimals1
    return x, y


def usd_prices(pools: list[PoolPrice], stablecoins: frozenset[str]) -> dict[str, float]:
    """{トークン(小文字): ドル価格}。値段の道がないトークンは入らない。"""
    prices = {s: 1.0 for s in stablecoins}
    amounts = [virtual_amounts(p) for p in pools]
    while True:
        best: tuple[float, str, float] | None = None
        for p, (x, y) in zip(pools, amounts):
            if not p.price or p.price <= 0 or not math.isfinite(p.price):
                continue
            t0, t1 = p.token0.lower(), p.token1.lower()
            if t1 in prices and t0 not in prices:
                cand = (y * prices[t1], t0, p.price * prices[t1])
            elif t0 in prices and t1 not in prices:
                cand = (x * prices[t0], t1, prices[t0] / p.price)
            else:
                continue
            if cand[0] > 0 and (best is None or cand[0] > best[0]):
                best = cand
        if best is None:
            return prices
        prices[best[1]] = best[2]


def depth_usd(p: PoolPrice, prices: dict[str, float]) -> float | None:
    """今の価格のところにある流動性のドル換算（両側の合計）。"""
    x, y = virtual_amounts(p)
    u0, u1 = prices.get(p.token0.lower()), prices.get(p.token1.lower())
    if u0 is None or u1 is None:
        return None
    return x * u0 + y * u1
