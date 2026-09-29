"""ヘッジ先（perp の取引所）アダプターの共通の形（SPEC 5.2.1章。2026-09-29 オーナー追加）。

会場アダプターと同じ考え方で、ヘッジ先ごとに1ファイル。どれも読み取りだけで、お金を動かすコード
（注文・送金・署名）は持たない。秘密鍵も使わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class HedgeMarket:
    hedge_id: str
    symbol: str                   # ヘッジ先での銘柄名（例: "NVDA"、"ETH"）
    market_id: int
    taker_pct: float | None = None   # 取引手数料（%）。分からなければ None
    maker_pct: float | None = None


@dataclass(frozen=True)
class HedgePosition:
    symbol: str
    size: float                   # 数量（売りはマイナス）
    value_usd: float | None = None


@dataclass(frozen=True)
class HedgeAccount:
    hedge_id: str
    collateral_usd: float         # 担保の残高（ドル）
    available_usd: float | None = None
    positions: list[HedgePosition] = field(default_factory=list)


class HedgeAdapter(Protocol):
    hedge_id: str                 # 例: "lighter"
    name: str                     # 画面に出す名前（例: "Lighter"）

    def markets(self) -> dict[str, HedgeMarket]:
        """扱っている perp の一覧（銘柄 → 市場）。手数料つき。"""
        ...

    def short_funding_hourly(self, market_id: int, start: int, end: int) -> list[tuple[int, float]]:
        """売りを持った場合の1時間ごとの資金調達の支払い（元本に対する割合。プラス = 払う）。古い順の (UNIX秒, 割合)。"""
        ...

    def account(self, address: str) -> HedgeAccount | None:
        """アドレスだけで担保の残高と建玉を読む（見つからなければ None）。"""
        ...


def funding_daily(adapter: HedgeAdapter, market_id: int, start: int, end: int) -> float | None:
    """直近の1時間ごとの支払いの平均 × 24（1日あたりの割合）。記録がなければ None。"""
    rows = adapter.short_funding_hourly(market_id, start, end)
    return sum(r for _, r in rows) / len(rows) * 24 if rows else None


def round_trip_cost(taker_pct: float | None, funding_day: float | None, default_taker_pct: float) -> float | None:
    """ヘッジ先の比べ方（2026-09-29 オーナー指示「手数料と資金調達料が一番安いところ」）:
    開く＋閉じるの取引手数料（2回分）＋ 1日の資金調達料。どちらも元本に対する割合。
    資金調達を受け取る側（マイナス）でも、安全側で0として比べる。資金調達が分からない市場は選ばない。
    """
    if funding_day is None:
        return None
    t = (taker_pct if taker_pct is not None else default_taker_pct) / 100
    return 2 * t + max(0.0, funding_day)


def mask(address: str | None) -> str | None:
    """画面に出すときのアドレスの省略形（0x1234…abcd）。"""
    if not address:
        return None
    a = address.strip()
    return a if len(a) <= 12 else f"{a[:6]}…{a[-4:]}"
