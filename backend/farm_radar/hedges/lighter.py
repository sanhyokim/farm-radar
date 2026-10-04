"""ヘッジ先アダプター: Lighter（読み取りのみ。SPEC 5.2.1章）。

公開 API（認証なし）だけを使う。確認済みの事実と出典は venues/lighter.yaml。
- 銘柄の一覧: /api/v1/orderBooks（market_type = perp、status = active）
- 手数料: /api/v1/orderBookDetails の taker_fee / maker_fee（%）
- 資金調達: /api/v1/fundings（external/lighter.py に単位の確認のメモ）
- 担保と建玉: /api/v1/account?by=l1_address&value=<アドレス> の collateral・available_balance・positions
  （2026-09-29 に公開 API の応答で形を確認。見つからないアドレスは code 21100 "account not found"）
"""

from __future__ import annotations

from ..external.http import JsonGetter
from ..external.lighter import RH_BASE_URL, Lighter
from .base import HedgeAccount, HedgeMarket, HedgePosition


class LighterHedge:
    hedge_id = "lighter"
    name = "Lighter"

    def __init__(self, client: Lighter | None = None):
        self.client = client or Lighter()

    def markets(self) -> dict[str, HedgeMarket]:
        """銘柄の一覧と手数料。どちらか片方が取れなくても、取れた方だけ返す（両方だめなら例外）。"""
        perps, fees, err = None, {}, None
        try:
            perps = self.client.active_perps()
        except Exception as exc:
            err = exc
        try:
            fees = self.client.market_fees()
        except Exception as exc:
            if perps is None:
                raise exc from err
        if perps is None:
            return {f"#{mid}": HedgeMarket(self.hedge_id, f"#{mid}", mid, t, m) for mid, (t, m) in fees.items()}
        return {sym: HedgeMarket(self.hedge_id, sym, m.market_id, *(fees.get(m.market_id) or (None, None)))
                for sym, m in perps.items()}

    def short_funding_hourly(self, market_id: int, start: int, end: int) -> list[tuple[int, float]]:
        return self.client.short_funding_hourly(market_id, start, end)

    def market_fees(self) -> dict[int, tuple[float, float]]:
        return self.client.market_fees()

    def account(self, address: str) -> HedgeAccount | None:
        data = self.client.http.get("account", {"by": "l1_address", "value": address})
        accounts = data.get("accounts") or []
        if not accounts:
            return None
        a = accounts[0]
        positions = []
        for p in a.get("positions") or []:
            try:
                size = float(p.get("position") or 0.0) * (-1 if int(p.get("sign", 1)) < 0 else 1)
            except (TypeError, ValueError):
                continue
            if size:
                val = p.get("position_value")
                positions.append(HedgePosition(str(p.get("symbol")), size, float(val) if val is not None else None))
        avail = a.get("available_balance")
        return HedgeAccount(self.hedge_id, float(a.get("collateral") or 0.0),
                            float(avail) if avail is not None else None, positions)


class LighterRhHedge(LighterHedge):
    """Lighter の Robinhood Chain 版（2026-10-04 オーナー決定 ②A。読み取りだけ）。

    練習の資金調達料を、Robinhood Chain のプールでは この版の記録で積み上げるために使う。市場の番号は本体と別。
    選ぶ先（config.yaml の hedge_venues）には入れない。本体の市場と同じ記号の市場を、練習の計算が自分で探して使う。
    """
    hedge_id = "lighter_rh"
    name = "Lighter（Robinhood Chain 版）"

    def __init__(self, client: Lighter | None = None):
        super().__init__(client or Lighter(JsonGetter(RH_BASE_URL, min_interval=0.3)))
