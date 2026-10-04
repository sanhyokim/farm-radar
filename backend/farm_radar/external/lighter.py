"""Lighter（perp取引所）の公開API。認証なし・読み取りだけ（SPEC 5.3章）。

資金調達率（funding）の単位は、次の2つの照合で確かめた（2026-09-29）:
- 公式ドキュメント https://docs.lighter.xyz/trading/funding : 支払いは毎時0分（1時間ごと）
- /api/v1/fundings（1時間ごとの履歴）の rate は「1時間あたりの%」、direction は払う側。
  例: NVDA の rate "0.0004"（%/時）× 8 ÷ 100 = 0.000032 が /api/v1/funding-rates の lighter の値（8時間あたり）と一致。
  ETH も同じ（0.0012 × 8 ÷ 100 = 0.000096）。value（1単位あたりの支払額）= rate% × 価格 ÷ 100 とも一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from .http import JsonGetter

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"
# Lighter の Robinhood Chain 版（別の取引所。読み取り口の形は本体と同じ。venues/lighter.yaml の robinhood_chain）
RH_BASE_URL = "https://api.rh.lighter.xyz/api/v1"


@dataclass(frozen=True)
class PerpMarket:
    symbol: str
    market_id: int


class Lighter:
    def __init__(self, getter: JsonGetter | None = None):
        self.http = getter or JsonGetter(BASE_URL, min_interval=0.3)

    def active_perps(self) -> dict[str, PerpMarket]:
        data = self.http.get("orderBooks")
        out = {}
        for m in data.get("order_books") or []:
            if m.get("market_type") == "perp" and m.get("status") == "active":
                out[m["symbol"]] = PerpMarket(m["symbol"], int(m["market_id"]))
        return out

    def market_fees(self) -> dict[int, tuple[float, float]]:
        """perp の市場ごとの (取引手数料 taker %, maker %)。/api/v1/orderBookDetails の taker_fee・maker_fee。

        単位は%（公式ドキュメント https://docs.lighter.xyz/trading/trading-fees の表と同じ書き方。
        例: 手数料ありのアカウントの taker "0.0280%"）。2026-09-29 時点は全市場 "0.0000"（標準アカウントは無料）。
        """
        data = self.http.get("orderBookDetails")
        out = {}
        for m in data.get("order_book_details") or []:
            try:
                out[int(m["market_id"])] = (float(m.get("taker_fee") or 0.0), float(m.get("maker_fee") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def short_funding_hourly(self, market_id: int, start: int, end: int) -> list[tuple[int, float]]:
        """売り（ショート）を持った場合の、1時間ごとの資金調達の支払い（元本に対する割合）。

        プラス = 払う、マイナス = 受け取る。古い順の (UNIX秒, 割合)。
        """
        count = max(1, (end - start) // 3600 + 1)
        data = self.http.get("fundings", {
            "market_id": market_id, "resolution": "1h",
            "start_timestamp": start, "end_timestamp": end, "count_back": count,
        })
        out = []
        for f in data.get("fundings") or []:
            rate = float(f["rate"]) / 100          # %/時 → 割合/時
            # direction は払う側。long が払うならショートは受け取る
            out.append((int(f["timestamp"]), -rate if f.get("direction") == "long" else rate))
        return sorted(out)
