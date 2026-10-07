"""N6 の1回（15分ごと）で使う市場の数字。保存した一覧（feeds.sqlite3）を読むだけ。

値段の選び方（記録の新しいものから。どれを使ったかを残す）:
  ステーブル → 1 ドル / Lighter の値段（Robinhood Chain 版は1時間ごと、本体は1日1回）/ Merkl のボーナスのコインの値段
  （15分ごと）/ DefiLlama の1時間ごとの値段（1日1回まとめて読む）。
  幅に配るプールは、チェーンで読んだプールの値段（2つのコインの比率。約1時間ごと）を使い、ドルは上の値段で直す。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .. import opportunities as opps
from ..config import Config, N6Rules
from ..feeds import pools as feed_pools


def rules_config(config: Config, rules: N6Rules) -> Config:
    """N6 の計算に使う設定: 探すと同じ式に、N6 の決まり（新 / 旧）の置き直しの補正と Merkl の分母、N6 の額の段を入れる。
    探す・影の記録は前の設定のまま（2026-10-07 指示書 2・3 は N6 の練習だけ）。"""
    o = replace(config.opportunities, rebalance_factor=rules.rebalance_factor,
                merkl_denominator=rules.merkl_denominator, amounts_usd=tuple(config.n6.amounts_usd))
    return replace(config, opportunities=o)


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@dataclass
class Price:
    usd: float
    source: str                      # stable / lighter_rh / lighter / merkl / defillama / pool
    at: datetime | None

    def age_h(self, now: datetime) -> float | None:
        return None if self.at is None else max(0.0, (now - self.at).total_seconds() / 3600)


def is_cl_campaign(c: dict[str, Any]) -> bool:
    """幅に配るキャンペーン（読み方のあるプール。opportunities._cl_pool と同じ見分け方）。"""
    st = opps._settings(c)
    return (str(c.get("type") or "").upper() in feed_pools.POOL_TYPES and bool(st.get("poolId"))
            and st.get("decimalsCurrency0") is not None and "weightFees" in st)


def live_campaigns(camps: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    now_s = int(now.timestamp())
    return [c for c in camps if (c.get("start_ts") or 0) <= now_s and (c.get("end_ts") is None or c["end_ts"] > now_s)]


class Market:
    """1回分の数字。ops は N6 の新しい決まりで計算した入れる先（探すと同じ式）、ops_old は前の決まり（比べる影）。"""

    def __init__(self, fconn: sqlite3.Connection | None, config: Config, now: datetime,
                 ops: list[Any] | None = None, ops_old: list[Any] | None = None):
        self.fconn = fconn
        self.config = config
        self.now = now
        self.s = config.opportunities
        self.ops = {o.base.key: o for o in ops or []}
        self.ops_old = {o.base.key: o for o in ops_old or []}
        self.data = opps.FeedData(fconn, config, now)
        self.infos: dict[str, dict[str, Any]] = {}
        self.camps: dict[str, list[dict[str, Any]]] = {}
        self.tvl: dict[str, tuple[float | None, float | None]] = {}       # 入れる先 → (預かり額, 1日のボーナス)
        self.marks: dict[tuple[str, int], tuple[float, datetime | None]] = {}
        self.funding: dict[tuple[str, int], tuple[float, datetime | None]] = {}
        self.reward_px: dict[tuple[int, str], tuple[float, datetime | None]] = {}
        if fconn is None:
            return
        self.infos, self.camps = opps._merkl_rows(fconn)
        last = fconn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
        if last:
            for r in fconn.execute("SELECT opportunity_id, tvl, daily_rewards FROM merkl_opportunity_snaps WHERE ts=?",
                                   (last,)):
                self.tvl[str(r[0])] = (r[1], r[2])
        self.snap_ts = _ts(last)
        for book, table in (("main", "lighter_markets"), ("rh", "lighter_rh_markets")):
            try:
                for r in fconn.execute(f"SELECT market_id, mark_price, updated_at FROM {table} WHERE mark_price > 0"):
                    self.marks[(book, int(r[0]))] = (float(r[1]), _ts(r[2]))
            except sqlite3.OperationalError:
                pass
        for book, table in (("main", "lighter_funding_snaps"), ("rh", "lighter_rh_funding_snaps")):
            try:
                for r in fconn.execute(f"""SELECT s.market_id, s.rate_8h, s.ts FROM {table} s
                                           JOIN (SELECT market_id, MAX(ts) m FROM {table} GROUP BY market_id) l
                                           ON l.market_id = s.market_id AND l.m = s.ts"""):
                    if r[1] is not None:
                        self.funding[(book, int(r[0]))] = (float(r[1]), _ts(r[2]))
            except sqlite3.OperationalError:
                pass
        for cs in self.camps.values():
            for c in cs:
                if c.get("reward_price") and c.get("reward_address") and c.get("distribution_chain_id") is not None:
                    k = (int(c["distribution_chain_id"]), str(c["reward_address"]).lower())
                    at = _ts(c.get("last_seen"))
                    if k not in self.reward_px or (at and (self.reward_px[k][1] is None or at > self.reward_px[k][1])):
                        self.reward_px[k] = (float(c["reward_price"]), at)

    # --- 値段 -------------------------------------------------------------------------------------

    def price(self, chain_id: int | None, address: str | None, symbol: str | None) -> Price | None:
        """コイン1個のドルの値段（記録の新しいもの）。分からなければ None。"""
        stat = self.data.token(self.data.coin(chain_id, address)) if address else None
        stat = stat or self.data.token_by_symbol(chain_id, symbol)
        if self.data.is_stable(stat, symbol):
            return Price(1.0, "stable", self.now)
        cands: list[Price] = []
        perp = self.data.perp(symbol, chain_id=chain_id) if symbol else None
        if perp is not None:
            book = perp.get("book", "main")
            mid = perp.get("rh_market_id") if book == "rh" else perp.get("market_id")
            if mid is not None and (book, int(mid)) in self.marks:
                p, at = self.marks[(book, int(mid))]
                cands.append(Price(p, "lighter_rh" if book == "rh" else "lighter", at))
        if chain_id is not None and address and (int(chain_id), address.lower()) in self.reward_px:
            p, at = self.reward_px[(int(chain_id), address.lower())]
            cands.append(Price(p, "merkl", at))
        if stat is not None and stat.grid:
            t, p = stat.grid[-1]
            cands.append(Price(float(p), "defillama", datetime.fromtimestamp(int(t), UTC)))
        cands = [c for c in cands if c.usd > 0]
        if not cands:
            return None
        return max(cands, key=lambda c: c.at or datetime.min.replace(tzinfo=UTC))

    def reward_price(self, c: dict[str, Any]) -> float | None:
        """ボーナスのコインの値段（Merkl の15分ごとの値。無ければ None）。"""
        try:
            p = float(c.get("reward_price"))
        except (TypeError, ValueError):
            return None
        return p if p > 0 else None

    def pool_state(self, chain_id: int | None, pool_id: str | None) -> dict[str, Any] | None:
        if chain_id is None or not pool_id:
            return None
        return self.data.pools.get((int(chain_id), str(pool_id).lower()))

    def funding_daily(self, market: dict[str, Any]) -> tuple[float | None, datetime | None]:
        """売り（保険）が1日に払う割合（プラス = 払う、マイナス = 受け取る）。funding-rates の値はプラス = 買いが払う
        （2026-10-02 に照合）ので、売りの1日 = −値 × 3（8時間 → 1日）。"""
        book = market.get("book", "main")
        mid = market.get("rh_market_id") if book == "rh" else market.get("market_id")
        if mid is None or (book, int(mid)) not in self.funding:
            return None, None
        rate, at = self.funding[(book, int(mid))]
        return -rate * 3, at

    def opportunity_tvl_hours_ago(self, key: str, hours: float, tol_minutes: float = 35.0) -> float | None:
        """hours 時間前（±tol_minutes 分）の Merkl の預かり額（15分ごとの記録）。"""
        if self.fconn is None:
            return None
        from datetime import timedelta
        t = self.now - timedelta(hours=hours)
        lo = (t - timedelta(minutes=tol_minutes)).isoformat(timespec="seconds")
        hi = (t + timedelta(minutes=tol_minutes)).isoformat(timespec="seconds")
        rows = self.fconn.execute("SELECT ts, tvl FROM merkl_opportunity_snaps WHERE opportunity_id=? AND ts BETWEEN ? AND ? "
                                  "AND tvl IS NOT NULL", (key, lo, hi)).fetchall()
        if not rows:
            return None
        best = min(rows, key=lambda r: abs(((_ts(r[0]) or t) - t).total_seconds()))
        return float(best[1])

    def defillama_hours_ago(self, chain_id: int | None, address: str | None, symbol: str | None,
                            hours: float) -> float | None:
        """DefiLlama の1時間ごとの値段の、hours 時間前の値（記録の端から2時間以内のものだけ）。"""
        stat = self.data.token(self.data.coin(chain_id, address)) if address else None
        stat = stat or self.data.token_by_symbol(chain_id, symbol)
        if stat is None or not stat.grid:
            return None
        t = self.now.timestamp() - hours * 3600
        best = min(stat.grid, key=lambda g: abs(g[0] - t))
        return float(best[1]) if abs(best[0] - t) <= 2 * 3600 else None

    def info(self, key: str) -> dict[str, Any]:
        return self.infos.get(key) or {}

    def campaigns(self, key: str) -> list[dict[str, Any]]:
        return live_campaigns(self.camps.get(key) or [], self.now)


def settings_of(c: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(c.get("settings_json") or "{}") or {}
    except (TypeError, ValueError):
        return {}
