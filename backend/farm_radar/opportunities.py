"""機会の一覧（N2b。SPEC 13.4、設計案 3章「本当に残る利回り」）。

決まった形の数字（standard.py）から、機会ごとに「自分のお金を入れたあとに残る利回り」を、金額ごとに、
保険あり・保険なしの両方で計算する。お金を動かすコードはない（読み取りと計算だけ）。

残る利回り（1日）= ボーナスの取り分 − ボーナスのコインの値下がり ＋ 手数料 − 値動きの目減り − 置き直し
                  − 保険の費用 − ヘッジしない値動きの損 − 入る・出る費用 ÷ いる日数

式の出どころ:
- ボーナスの取り分は Merkl の配り方ごとに変える（research/redesign-merkl-2026-10-01.md の 1-1・3章。出典は Merkl の
  公式の資料と API の説明文）。山分け: D × X ÷ (T + X)、上限つき: min(上限 × X ÷ 365, D × X ÷ (T + X))、
  固定: 設定の年利 × X ÷ 365、トークン数の固定: 設定の数 × 値段 × X ÷ 365。
- 幅に配るプールの「幅の中にいる時間の割合」「置き直しの回数」「目減り（ガンマ）」「ヘッジしない値動きの損」は、
  今の版で承認された式（scoring/model.py。2026-09-27・09-29 オーナー承認）をそのまま使う。
- 全体に配るプールの目減りは、値動き σ の2乗 ÷ 8（50:50 のプールの1日の目減りの近似）。
- 自分で読んだ会場（up. など）は、今の版の計算（scoring/model.py の evaluate）を、金額と分け方だけ変えて使い直す。
仮の数字（幅 ±15%、控えめの 1.5 倍、5% の上限など）は config.yaml の opportunities にあり、N5 の試しで決め直す（13.3）。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config, ConfigError, OpportunitySettings, load_venue, mechanic_value
from .registry import coin_chains, load_chain
from .scoring import model as m
from .scoring import volatility as vol
from .standard import StandardOpportunity, _days_left, _feeds_conn, from_merkl, from_own

# 値段の記録が無いときだけ使う「値動きしないコイン」の記号（記録があれば、記録の値動きで決める）
STABLE_SYMBOLS = frozenset({"USDC", "USDT", "USDG", "DAI", "USDE", "USDS", "PYUSD", "RLUSD", "USDT0", "AUSD", "FRAX",
                            "GHO", "CRVUSD", "USDC.E", "USDBC", "LUSD", "USD0", "USDM", "USR", "EURC"})
# Merkl の機会の型のうち「幅に配る」プール（集中流動性）の目印
CL_HINTS = ("V3", "V4", "CLAMM", "SLIPSTREAM", "ALGEBRA", "CONCENTRATED", "_CL", "CL_")
# 上乗せ型（利回りを作った人が目標の年利から元の利回りを引いて出す。調べた結果の 1-1 の表）
TOP_UP_TYPES = ("ERC4626", "AAVE", "NET_APR", "TARGET_APR", "SOFR", "BORROW_SUBSIDY", "COMPOSED", "YIELD_BASIS", "DEEL")

LEVEL_EXCLUDE, LEVEL_WARN, LEVEL_INFO = "exclude", "warn", "info"
# 名前の [ ] の中に「〜だけ」とある機会（例: "[Robinhood Users Only]"）。参加できないので外す（設計案 2.3）
_ONLY = re.compile(r"\[[^\]]*\bonly\b[^\]]*\]", re.I)


@dataclass
class Flag:
    code: str
    level: str                      # exclude（一覧から外す） / warn（注意） / info（印だけ）
    text: str


@dataclass
class Variant:
    """1つの金額・1つの見込み（ふつう／控えめ）・保険あり／なしの計算。金額はドル、1日あたり。"""
    hedge: bool
    amount: float
    split: dict[str, float]          # pool（機会に置く）・hedge_margin（保険に預ける）・reserve（予備）
    income: float                    # ボーナスの取り分（手数料・元の利回りを含む）
    points: bool                     # ポイントだけのボーナスがある（0として数えた）
    gamma: float                     # 値動きの目減り
    rebalance: float                 # 置き直しの費用
    hedge_cost: float                # 保険の費用（資金調達料・取引の手数料）
    haircut: float                   # ボーナスのコインの値下がり
    direction: float                 # 保険を掛けていない値動きの損の見込み
    net: float                       # 1日に残る額（入る・出る費用の前）
    move_cost: float                 # 入る・出る費用（両替・ガス代・保険の開け閉め。送金は入っていない）
    stay_days: float
    net_after_move: float            # 入る・出る費用を、いる日数で割って引いたあと
    apr_pct: float                   # 総額あたりの年利（%。単利）
    payback_days: float | None       # 入る・出る費用を取り返す日数（残る額が0以下なら None）
    in_range_ratio: float | None = None
    range_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Opportunity:
    base: StandardOpportunity
    kind: str                        # pool_range / pool_full / hold / other
    calc: dict[str, dict[str, dict[str, Variant | None]]] = field(default_factory=dict)
    # calc[金額]["normal"|"cautious"]["hedge"|"no_hedge"]
    flags: list[Flag] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)   # 保険で守れない値動き
    cap_usd: float | None = None     # 入れてよい上限（預かり額 × 割合）
    computable: bool = True
    reason: str | None = None        # 計算できない理由
    new_pool: bool = False
    campaigns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return any(f.level == LEVEL_EXCLUDE for f in self.flags)

    def best(self, amount: float, case: str = "cautious") -> Variant | None:
        vs = [v for v in (self.calc.get(_akey(amount)) or {}).get(case, {}).values() if v is not None]
        return max(vs, key=lambda v: v.net_after_move) if vs else None

    def to_dict(self, amount: float | None = None, target_apr_pct: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            **self.base.to_dict(), "kind": self.kind, "computable": self.computable, "reason": self.reason,
            "flags": [asdict(f) for f in self.flags], "excluded": self.excluded, "unprotected": self.unprotected,
            "cap_usd": self.cap_usd, "new_pool": self.new_pool,
        }
        amounts = [amount] if amount is not None else [float(a) for a in self.calc]
        out["calc"] = {_akey(a): {case: {k: (v.to_dict() if v else None) for k, v in vs.items()}
                                  for case, vs in (self.calc.get(_akey(a)) or {}).items()} for a in amounts}
        if amount is not None:
            b = self.best(amount)
            out["best"] = b.to_dict() if b else None
            out["above_target"] = (b.apr_pct >= target_apr_pct) if b and target_apr_pct is not None else None
            out["over_cap"] = bool(self.cap_usd is not None and amount > self.cap_usd)
        return out


def _akey(amount: float) -> str:
    return f"{amount:g}"


# --- 計算に使う数字（feeds.sqlite3） ---------------------------------------------------------------

@dataclass(frozen=True)
class TokenStat:
    coin: str
    symbol: str | None
    price: float
    sigma: float | None              # 1日の値動き（ドル建て）
    trend_daily: float | None        # 7日の変化を1日あたりに直したもの
    grid: tuple[tuple[int, float], ...]


class FeedData:
    """保存した一覧から、計算に使う数字を取り出す（読み取りだけ）。"""

    def __init__(self, conn: sqlite3.Connection | None, config: Config, now: datetime):
        self.conn = conn
        self.now = now
        self.s = config.opportunities
        self.coin_keys = coin_chains(config.chains, config.root)
        self._tokens: dict[str, TokenStat | None] = {}
        self.perps: dict[str, dict[str, Any]] = {}
        if conn is None:
            return
        since = (now - timedelta(days=7)).isoformat(timespec="seconds")
        funding = {r[0]: r[1] for r in conn.execute(
            "SELECT market_id, AVG(rate_8h) FROM lighter_funding_snaps WHERE ts >= ? GROUP BY market_id", (since,))}
        for r in conn.execute("SELECT market_id, symbol, status, taker_pct, initial_margin_fraction, "
                              "maintenance_margin_fraction FROM lighter_markets"):
            if r["status"] != "active":
                continue
            rate = funding.get(r["market_id"])
            # funding-rates の値はプラス = 買いが払う（2026-10-02 に /fundings の direction と照合）。
            # 売り（保険）の1日の支払い = −値 × 3（8時間あたり → 1日）。受け取りは収入に数えない（今の版と同じ。安全側）
            cost = None if rate is None else -rate * 3
            # 証拠金の割合: API の値 ÷ 10000（公式の表 https://docs.lighter.xyz/trading/contract-specifications と
            # 照合: CRV は API 1000 / 600、表は IMR 10% / MMR 6%。2026-10-02）
            imf, mmf = r["initial_margin_fraction"], r["maintenance_margin_fraction"]
            self.perps[str(r["symbol"]).upper()] = {
                "market_id": r["market_id"], "symbol": r["symbol"], "funding_daily": cost, "taker_pct": r["taker_pct"],
                "imf": imf / 10000 if imf is not None else None, "mmf": mmf / 10000 if mmf is not None else None}

    def coin(self, chain_id: int | None, address: str | None) -> str | None:
        key = self.coin_keys.get(chain_id) if chain_id is not None else None
        return f"{key}:{address.lower()}" if key and address else None

    def token(self, coin: str | None) -> TokenStat | None:
        if coin is None or self.conn is None:
            return None
        if coin not in self._tokens:
            end = int(self.now.timestamp())
            start = end - 8 * 86400
            pts = [(r[0], r[1]) for r in self.conn.execute(
                "SELECT ts, price FROM token_prices WHERE coin=? AND ts>=? AND ts<=? ORDER BY ts", (coin, start, end))]
            sym = self.conn.execute("SELECT symbol FROM token_meta WHERE coin=?", (coin,)).fetchone()
            if len(pts) < 25:
                self._tokens[coin] = None
            else:
                grid = vol.hourly_grid(pts, pts[0][0], end)
                sigma = vol.daily_sigma([r for _, r in vol.hourly_returns(grid)])
                days = max(1e-9, (grid[-1][0] - grid[0][0]) / 86400)
                trend = (grid[-1][1] / grid[0][1]) ** (1 / days) - 1 if grid[0][1] > 0 else None
                self._tokens[coin] = TokenStat(coin, sym[0] if sym else None, grid[-1][1], sigma, trend, tuple(grid))
        return self._tokens[coin]

    def token_by_symbol(self, chain_id: int | None, symbol: str | None) -> TokenStat | None:
        """住所で値段が見つからないとき（ETH そのものなど）、同じチェーンの同じ記号（ETH は WETH）のコインで代わりに使う。"""
        key = self.coin_keys.get(chain_id) if chain_id is not None else None
        if not key or not symbol or self.conn is None:
            return None
        sym = {"ETH": "WETH"}.get(symbol.upper(), symbol.upper())
        row = self.conn.execute(
            """SELECT m.coin FROM token_meta m JOIN token_prices p ON p.coin = m.coin
               WHERE m.coin LIKE ? AND upper(m.symbol) = ? GROUP BY m.coin ORDER BY COUNT(*) DESC LIMIT 1""",
            (f"{key}:%", sym)).fetchone()
        return self.token(row[0]) if row else None

    def pair_sigma(self, a: TokenStat, b: TokenStat) -> float | None:
        return vol.daily_sigma([r for _, r in vol.hourly_returns(vol.ratio_grid(list(a.grid), list(b.grid)))])

    def perp(self, symbol: str | None, alias: bool = True) -> dict[str, Any] | None:
        """保険に使える銘柄（資金調達率と証拠金の割合が分かるものだけ）。"""
        if not symbol:
            return None
        aliases = {k.upper(): v for k, v in self.s.perp_alias.items()}
        sym = aliases.get(symbol.upper(), symbol) if alias else symbol
        p = self.perps.get(sym.upper())
        return p if p and p.get("funding_daily") is not None and p.get("mmf") is not None else None

    def is_stable(self, t: TokenStat | None, symbol: str | None) -> bool | None:
        """値動きしないコインか。記録があれば記録で決め、無ければ記号で決める（どちらでもなければ None）。"""
        if t is not None and t.sigma is not None:
            return t.sigma < self.s.stable_max_sigma and abs(t.price - 1) <= 0.02
        if symbol and symbol.upper() in STABLE_SYMBOLS:
            return True
        return None


# --- 配り方ごとのボーナス -------------------------------------------------------------------------

def _settings(c: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(c.get("settings_json") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def budget_daily(c: dict[str, Any]) -> float | None:
    """予算どおりの1日のボーナス（ドル）= 配る量 ÷ 日数 × 値段（調べた結果の 3章の D）。"""
    try:
        amount = int(c["amount_raw"]) / 10 ** int(c["reward_decimals"])
        days = (int(c["end_ts"]) - int(c["start_ts"])) / 86400
        return amount / days * float(c["reward_price"]) if days > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def campaign_income(c: dict[str, Any], x: float, tvl: float) -> tuple[float, str | None]:
    """1つのキャンペーンから、X ドル入れたときの1日のボーナス（ドル）と、計算の注意（あれば）。"""
    if x <= 0:
        return 0.0, None
    dt = (c.get("distribution_type") or "").upper()
    method = (c.get("distribution_method") or "").upper()
    s = _settings(c)
    d = c.get("daily_rewards") if c.get("daily_rewards") is not None else budget_daily(c)
    share = x / (max(tvl, 0.0) + x)
    if method == "AIRDROP":
        return 0.0, "一度きりの配布なので数えない"
    if dt == "DUTCH_AUCTION":
        return (d or 0.0) * share, None
    if dt.startswith("MAX_REWARD"):
        cap = _float(s.get("apr"))
        dutch = (budget_daily(c) or d or 0.0) * share
        return (min(cap * x / 365, dutch) if cap is not None else dutch), None
    if dt == "FIX_REWARD_VALUE_PER_LIQUIDITY_VALUE":
        apr = _float(s.get("apr"))
        return (apr * x / 365, None) if apr is not None else ((d or 0.0) * share, "配り方の設定が読めない（山分けとして計算）")
    if dt == "FIX_REWARD_AMOUNT_PER_LIQUIDITY_VALUE":
        per = _float(s.get("apr"))
        price = _float(c.get("reward_price"))
        if per is not None and price is not None:
            return per * price * x / 365, None
        return (d or 0.0) * share, "配り方の設定が読めない（山分けとして計算）"
    if any(h in dt or h in method for h in TOP_UP_TYPES):
        apr = _float(c.get("apr"))
        dutch = (budget_daily(c) or d or 0.0) * share
        return (min(apr / 100 * x / 365, dutch) if apr is not None else dutch), "上乗せ型（目標の年利までの差だけもらえる。粗い見込み）"
    return (d or 0.0) * share, f"配り方 {dt or '不明'} は山分けとして計算（粗い見込み）"


def _float(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# --- 1つの機会の計算 ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    symbol: str
    stat: TokenStat | None
    stable: bool
    perp: dict[str, Any] | None


def margin_need(perp: dict[str, Any], withstand_rise: float) -> float:
    """売り（保険）1ドルあたりに預けるお金: max(最初に要る割合, 上がり幅 u ＋ (1 + u) × 維持の割合)。

    値段が u 上がると売りは u だけ損をし、残りの担保が「(1 + u) × 維持の割合」を下回ると一部を強制的に閉じられる
    （公式の説明 https://docs.lighter.xyz/trading/liquidations-and-llp-insurance-fund の Maintenance Margin Req）。
    そこまで耐えられる額を預ける（2026-10-02 オーナー決定「自動で計算」）。
    """
    return max(perp.get("imf") or 0.0, withstand_rise + (1 + withstand_rise) * (perp.get("mmf") or 0.0))


def _split(s: OpportunitySettings, config: Config, hedge: bool, margin_per_pool: float = 0.0) -> dict[str, float]:
    """総額を、機会に置く分・保険に預ける分・予備に分ける（割合）。

    保険あり: 予備を引いた残りを P ×（1 ＋ 保険に預ける割合）に分ける（2026-10-02 オーナー決定「自動で計算」）。
    margin_per_pool = 機会に置く1ドルあたりに保険へ預けるお金（売る量の割合 × margin_need の合計）。
    保険なし: 予備のほかは全部、機会に置く。
    """
    reserve = config.scoring.allocation_reserve
    if hedge:
        pool = (1 - reserve) / (1 + margin_per_pool)
        return {"pool": pool, "hedge_margin": pool * margin_per_pool, "reserve": reserve}
    return {"pool": 1 - reserve, "hedge_margin": 0.0, "reserve": reserve}


def _variant(*, hedge: bool, amount: float, split: dict[str, float], kind: str, legs: list[Leg], sigma_pair: float | None,
             campaigns: list[dict[str, Any]], tvl: float, base_apr_pct: float, reward_trend: float | None,
             s: OpportunitySettings, config: Config, gas: float, fee: float, stay_days: float) -> Variant:
    c_pos = amount * split["pool"]
    income, points = 0.0, False
    for c in campaigns:
        if (c.get("reward_type") or "TOKEN").upper() != "TOKEN":
            points = True
            continue
        inc, _ = campaign_income(c, c_pos, tvl)
        income += inc
    r = s.merkl_range_pct / 100
    irr = None
    gamma_day = reb = 0.0
    swap_ratio = config.scoring.swap_ratio
    if kind == "pool_range" and sigma_pair is not None:
        irr = m.in_range_ratio(sigma_pair, r, config.scoring.rebalance_wait_minutes)
        income *= irr
        gamma_day = m.gamma(c_pos, sigma_pair, r)
        n = m.rebalances_per_day(sigma_pair, r)
        reb = n * (gas * 2 + c_pos * swap_ratio * fee)
    elif kind == "pool_full" and sigma_pair is not None:
        gamma_day = c_pos * sigma_pair ** 2 / 8
    n_reb = m.rebalances_per_day(sigma_pair, r) if kind == "pool_range" and sigma_pair else 0.0
    income += c_pos * base_apr_pct / 100 / 365            # 元の利回り（貸し出しの利息など。分かるときだけ）
    haircut = income * -reward_trend if reward_trend is not None and reward_trend < 0 else 0.0
    exposure = c_pos * 0.5 if kind.startswith("pool") else c_pos      # 1つのコインあたりの値動きの量
    hedge_cost = direction = 0.0
    hedged_notional = 0.0
    for leg in legs:
        if leg.stable:
            continue
        if hedge and leg.perp is not None:
            cost = leg.perp["funding_daily"]
            cost = cost if config.scoring.count_funding_income else max(0.0, cost)
            taker = (leg.perp.get("taker_pct") or 0.0) / 100
            hedge_cost += exposure * cost + n_reb * exposure * taker
            hedged_notional += exposure
        else:
            sig = leg.stat.sigma if leg.stat and leg.stat.sigma is not None else 0.0
            direction += exposure * 0.4 * sig          # 承認済みの C_lp × 0.5 × 0.4 × σ と同じ（プールは exposure = C_lp × 0.5）
    net = income - gamma_day - reb - hedge_cost - haircut - direction
    # 入る・出る費用: 両替（プールは半分、1つのコインを持つ型は値動きするときだけ全部）× 2回、ガス代4回、保険の開け閉め
    swap_part = c_pos * 0.5 if kind.startswith("pool") else (c_pos if any(not lg.stable for lg in legs) else 0.0)
    move = 2 * swap_part * fee + 4 * gas
    move += 2 * hedged_notional * max((leg.perp or {}).get("taker_pct") or 0.0 for leg in legs) / 100 if hedged_notional else 0.0
    after = net - move / stay_days
    return Variant(hedge=hedge, amount=amount, split={k: v * amount for k, v in split.items()}, income=income,
                   points=points, gamma=gamma_day, rebalance=reb, hedge_cost=hedge_cost, haircut=haircut,
                   direction=direction, net=net, move_cost=move, stay_days=stay_days, net_after_move=after,
                   apr_pct=after / amount * 365 * 100, payback_days=(move / net) if net > 0 else None,
                   in_range_ratio=irr, range_pct=s.merkl_range_pct if kind == "pool_range" else None)


def _kind(info: dict[str, Any]) -> str:
    action = str(info.get("action") or "").upper()
    typ = str(info.get("type") or "").upper()
    if action == "POOL" and len(info.get("tokens") or []) >= 2:
        return "pool_range" if any(h in typ for h in CL_HINTS) else "pool_full"
    if action == "POOL":
        return "hold"                 # コインが1つだけの「プール」（金庫に預ける型）は、預ける型として計算する
    if action in ("LEND", "HOLD"):
        return "hold"
    return "other"


def evaluate_merkl(base: StandardOpportunity, info: dict[str, Any], campaigns: list[dict[str, Any]], data: FeedData,
                   config: Config) -> Opportunity:
    s = config.opportunities
    op = Opportunity(base=base, kind=_kind(info), campaigns=[_campaign_view(c) for c in campaigns])
    now_s = int(data.now.timestamp())
    live = [c for c in campaigns if (c.get("start_ts") or 0) <= now_s and (c.get("end_ts") is None or c["end_ts"] > now_s)]
    tvl = base.tvl_usd or 0.0
    _flags(op, info, live, base, s, now_s)
    if op.kind == "other":
        op.computable, op.reason = False, "借りる型などは計算しない（預けて受け取る型だけ）"
        return op
    if not live:
        op.computable, op.reason = False, "今動いているキャンペーンがない（予定だけ）"
        return op
    pairs = list(zip(info.get("tokens") or [], (info.get("token_addrs") or []) + [None] * 6))
    if op.kind.startswith("pool") and len(pairs) > 2:
        op.computable, op.reason = False, "3つ以上のコインのプールは、まだ計算しない"
        return op
    legs = []
    for sym, addr in pairs:
        stat = data.token(data.coin(base.evm_chain_id, addr)) or data.token_by_symbol(base.evm_chain_id, sym)
        stable = data.is_stable(stat, sym)
        if stable is None or (not stable and (stat is None or stat.sigma is None)):
            legs.append(Leg(sym, None, False, None))       # 分からないコイン（下で扱いを決める）
            continue
        legs.append(Leg(sym, stat, stable, None if stable else data.perp(sym)))
    if op.kind == "hold":
        # 預ける型: 一覧には「受け取る証書のコイン」と「預ける元のコイン」の両方が出ることがある。
        # 値段の分かるコインのうち最初のもの（預ける元のコイン）で計算する
        known = [lg for lg in legs if lg.stable or lg.stat is not None]
        if not known:
            op.computable, op.reason = False, f"{legs[0].symbol if legs else 'コイン'} の値段の記録がなく、値動きが分からない"
            return op
        if len(known) < len(legs):
            op.flags.append(Flag("HOLD_UNDERLYING", LEVEL_INFO, f"預ける元のコイン（{known[0].symbol}）の値動きで計算"))
        legs = known[:1]
    else:
        unknown = [lg.symbol for lg in legs if not lg.stable and lg.stat is None]
        if unknown:
            op.computable, op.reason = False, f"{unknown[0]} の値段の記録がなく、値動きが分からない"
            return op
    if not legs:
        op.computable, op.reason = False, "コインが分からない"
        return op
    sigma_pair = 0.0
    if op.kind.startswith("pool") and len(legs) >= 2:
        a, b = legs[0], legs[1]
        if a.stable and b.stable:
            sigma_pair = 0.0
        elif a.stat and b.stat:
            sigma_pair = data.pair_sigma(a.stat, b.stat)
        else:
            sigma_pair = (a.stat or b.stat).sigma if (a.stat or b.stat) else None
        if sigma_pair is None:
            op.computable, op.reason = False, "2つのコインの比率の値動きが分からない"
            return op
    # ボーナスのコインの値動き（同じチェーンで値段の記録があるときだけ）
    trends, reward_syms = [], set()
    for c in live:
        if (c.get("reward_type") or "TOKEN").upper() != "TOKEN":
            continue
        rs = data.token(data.coin(c.get("distribution_chain_id"), c.get("reward_address")))
        stable = data.is_stable(rs, c.get("reward_symbol"))
        if not stable:
            reward_syms.add(c.get("reward_symbol") or "?")
            if rs is not None and rs.trend_daily is not None:
                trends.append(rs.trend_daily)
    reward_trend = min(trends) if trends else None
    op.unprotected = [f"ボーナスのコイン（{x}）の値下がり" for x in sorted(reward_syms)]
    op.unprotected += [f"{lg.symbol} の値下がり（保険の売り場がない）" for lg in legs if not lg.stable and lg.perp is None]
    if reward_syms and reward_trend is None:
        op.flags.append(Flag("RWD_TREND", LEVEL_INFO, "ボーナスのコインの値動きの記録がなく、値下がりは引いていない"))
    base_apr = (_float(info.get("native_apr")) or 0.0) if op.kind == "hold" else 0.0
    gas = s.gas_usd_per_tx.get(base.chain or "", max(s.gas_usd_per_tx.values(), default=0.2))
    fee = s.pool_fee_pct / 100
    ends = [c["end_ts"] for c in live if c.get("end_ts")]
    days_left = (max(ends) - now_s) / 86400 if ends else None
    stay = max(1 / 24, min(s.stay_days, days_left)) if days_left is not None else s.stay_days
    volatile = [lg for lg in legs if not lg.stable]
    can_hedge = any(lg.perp for lg in volatile)
    if volatile and not can_hedge:
        op.flags.append(Flag("NO_HEDGE", LEVEL_INFO, "保険の売り場（Lighter）がないので、保険なしだけ"))
    op.cap_usd = tvl * s.max_pool_share if tvl else None
    expo = 0.5 if op.kind.startswith("pool") else 1.0
    margin_per_pool = sum(expo * margin_need(lg.perp, s.hedge_withstand_rise_pct / 100) for lg in volatile if lg.perp)
    for amount in s.amounts_usd:
        row: dict[str, dict[str, Variant | None]] = {}
        for case, mult in (("normal", 1.0), ("cautious", s.cautious_tvl_multiple)):
            kw = dict(amount=amount, kind=op.kind, legs=legs, sigma_pair=sigma_pair, campaigns=live,
                      tvl=tvl * mult, base_apr_pct=base_apr, reward_trend=reward_trend, s=s, config=config,
                      gas=gas, fee=fee, stay_days=stay)
            row[case] = {
                "no_hedge": _variant(hedge=False, split=_split(s, config, False), **kw),
                "hedge": _variant(hedge=True, split=_split(s, config, True, margin_per_pool), **kw) if can_hedge else None,
            }
        op.calc[_akey(amount)] = row
    # 残りの日数で入る・出る費用を取り返せない（$1,000・控えめ・良い方で判断）
    b = op.best(1000.0 if 1000.0 in s.amounts_usd else s.amounts_usd[0])
    if b is not None and days_left is not None and (b.payback_days is None or b.payback_days > days_left):
        op.flags.append(Flag("PAYBACK", LEVEL_EXCLUDE, "残りの日数で、入る・出る費用を取り返せない"))
    return op


def _campaign_view(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("campaign_id", "distribution_type", "distribution_method", "reward_symbol",
                                  "reward_type", "daily_rewards", "apr", "start_ts", "end_ts", "restricted",
                                  "distribution_chain_id")}


def _flags(op: Opportunity, info: dict[str, Any], live: list[dict[str, Any]], base: StandardOpportunity,
           s: OpportunitySettings, now_s: int) -> None:
    tvl = base.tvl_usd or 0.0
    starts = [c.get("start_ts") for c in live if c.get("start_ts")]
    started_days = (now_s - min(starts)) / 86400 if starts else None
    op.new_pool = tvl < s.new_pool_tvl_usd and started_days is not None and started_days <= s.new_pool_days
    if op.new_pool:
        op.flags.append(Flag("NEW_POOL", LEVEL_INFO, "始まったばかりの新しいプール（早く入る得がある。自分のお金でも大きく薄まる）"))
    elif tvl < s.min_tvl_usd:
        op.flags.append(Flag("SMALL", LEVEL_EXCLUDE, f"預かり額が小さすぎる（${s.min_tvl_usd:,.0f} 未満）"))
    if _ONLY.search(base.name or ""):
        op.flags.append(Flag("RESTRICTED", LEVEL_EXCLUDE, "名前に参加できる人の条件がある（例: 「Robinhood のアプリの利用者だけ」）"))
    elif live and all(c.get("restricted") for c in live):
        op.flags.append(Flag("RESTRICTED", LEVEL_EXCLUDE, "参加できる人が限られている（名簿つきなど）"))
    elif any(c.get("restricted") for c in live):
        op.flags.append(Flag("RESTRICTED_SOME", LEVEL_WARN, "一部のキャンペーンは参加できる人が限られている（数えていない）"))
    if live and all(c.get("hidden") for c in live):
        op.flags.append(Flag("HIDDEN", LEVEL_EXCLUDE, "Merkl の画面に出ていないキャンペーンだけ"))
    if any((c.get("reward_type") or "TOKEN").upper() != "TOKEN" for c in live):
        op.flags.append(Flag("POINTS", LEVEL_INFO, "ボーナスにポイントやまだ売れないコインがある（0として数えた）"))
    if base.shown_apr_pct is not None and base.shown_apr_pct >= s.too_high_apr_pct:
        op.flags.append(Flag("TOO_HIGH", LEVEL_WARN, f"表示の年利が {s.too_high_apr_pct:,.0f}% 以上（長く続かないことが多い）"))
    if any(c.get("distribution_chain_id") not in (None, base.evm_chain_id) for c in live):
        op.flags.append(Flag("CROSS_CHAIN", LEVEL_INFO, "ボーナスを別のチェーンで受け取る（受け取りのガス代と手間）"))
    dts = {(c.get("distribution_type") or "").upper() for c in live} | {(c.get("distribution_method") or "").upper() for c in live}
    if any(h in d for d in dts for h in TOP_UP_TYPES):
        op.flags.append(Flag("TOP_UP", LEVEL_INFO, "上乗せ型（目標の年利までの差だけもらえる。粗い見込み）"))
    if any((c.get("distribution_method") or "").upper() == "AIRDROP" for c in live):
        op.flags.append(Flag("AIRDROP", LEVEL_INFO, "一度きりの配布があり、数えていない"))
    if op.kind == "pool_range":
        op.flags.append(Flag("RANGE", LEVEL_INFO, f"幅に配るプール。幅 ±{s.merkl_range_pct:g}% で計算（仮。自分で読む N3 までの見込み）"))
    if op.kind.startswith("pool"):
        op.flags.append(Flag("NO_FEES", LEVEL_INFO, "プールの手数料の収入は数えていない（分からないため。安全側）"))


# --- 自分で読んだ会場（up. など） ------------------------------------------------------------------

def evaluate_own(base: StandardOpportunity, conn: sqlite3.Connection, config: Config, data: FeedData) -> Opportunity:
    """今の版の計算（scoring/model.py）を、金額と分け方だけ変えて使い直す。"""
    s = config.opportunities
    op = Opportunity(base=base, kind="pool_range")
    row = conn.execute("""SELECT s.details_json, p.token0_symbol, p.token1_symbol, p.token0_decimals, p.token1_decimals,
                                 (SELECT unstaked_fee FROM pool_snapshots WHERE pool_id=p.id ORDER BY ts DESC LIMIT 1) uf
                          FROM scores s JOIN pools p ON p.id=s.pool_id WHERE s.pool_id=? ORDER BY s.ts DESC LIMIT 1""",
                       (base.key,)).fetchone()
    inp = json.loads(row["details_json"] or "{}").get("inputs") if row else None
    try:
        venue = load_venue(base.venue, config.root)
    except (OSError, ConfigError):
        venue = {}
    if not inp or inp.get("sigma_pair") is None:
        op.computable, op.reason = False, "スコアの計算に必要な数字がそろっていない"
        return op
    syms = (row["token0_symbol"], row["token1_symbol"])
    decs = (row["token0_decimals"], row["token1_decimals"])
    hedge_info = inp.get("hedge") or {}

    def margin_perp(sym: str) -> dict[str, Any] | None:
        h = hedge_info.get(sym) or {}
        return data.perp(h.get("symbol"), alias=False) if h.get("hedge_id") == "lighter" else None

    def side(i: int, hedged: bool) -> m.TokenSide | None:
        sym = syms[i]
        usd, sig = (inp.get("usd") or {}).get(sym), (inp.get("sigma_token") or {}).get(sym)
        if usd is None or sig is None or decs[i] is None:
            return None
        h = hedge_info.get(sym) or {}
        # 保険は、証拠金の割合が分かる売り場（Lighter）のときだけ掛ける（分け方を自動で計算するため）
        fund = h.get("funding_daily") if hedged and h.get("hedge_id") and margin_perp(sym) else None
        return m.TokenSide(usd=usd, decimals=int(decs[i]), sigma_usd=sig, stable=sig == 0,
                           hedgeable=fund is not None, funding_cost_daily=fund or 0.0,
                           taker_fee=(h.get("taker_pct") or 0.0) / 100 if fund is not None else None)

    def pool_inputs(hedged: bool, mult: float) -> m.PoolInputs | None:
        t0, t1 = side(0, hedged), side(1, hedged)
        if t0 is None or t1 is None:
            return None
        lt = int((inp.get("liquidity_latest") or {}).get("total") or inp.get("liquidity_total") or 0)
        ls = int((inp.get("liquidity_latest") or {}).get("staked") or inp.get("liquidity_staked_inrange") or 0)
        med = inp.get("liquidity_median_24h") or {}
        lt, ls = max(lt, int(med.get("total") or 0)), max(ls, int(med.get("staked") or 0))
        return m.PoolInputs(price=float(inp["price"]), token0=t0, token1=t1, fee=float(inp.get("fee") or 0),
                            unstaked_fee=(row["uf"] or 0) / 1e6, liquidity_total=int(lt * mult),
                            liquidity_staked=int(ls * mult), reward_usd_day=float(inp.get("reward_usd_day") or 0),
                            fees_usd_day=inp.get("fees_usd_day"), sigma_pair=float(inp["sigma_pair"]),
                            reward_trend_daily=inp.get("reward_token_trend_daily"),
                            slippage=float(inp.get("slippage") or 0),
                            rewards_only=mechanic_value(venue, "lp_receives_swap_fees", True) is False)

    probe = pool_inputs(True, 1.0)
    if probe is None:
        op.computable, op.reason = False, "コインのドル価格か値動きが分からない"
        return op
    volatile = [t for t in (probe.token0, probe.token1) if not t.stable]
    can_hedge = any(t.hedgeable for t in volatile)
    op.unprotected = [f"ボーナスのコイン（{base.bonus_token or '報酬のコイン'}）の値下がり"]
    op.unprotected += [f"{syms[i]} の値下がり（保険の売り場がない）" for i, t in enumerate((probe.token0, probe.token1))
                       if not t.stable and not t.hedgeable]
    sc = config.scoring
    gas = float(inp.get("gas_usd_per_tx") or 0.0)
    params0 = m.ModelParams(ranges=tuple(x / 100 for x in sc.ranges_pct), rebalance_wait_minutes=sc.rebalance_wait_minutes,
                            gas_usd_per_tx=gas, swap_ratio=sc.swap_ratio, hedge_taker_fee=sc.hedge_taker_fee_pct / 100,
                            count_funding_income=sc.count_funding_income, reward_sell_hours=sc.reward_sell_hours)
    margin_per_pool = sum(0.5 * margin_need(margin_perp(syms[i]), s.hedge_withstand_rise_pct / 100)
                          for i, t in enumerate((probe.token0, probe.token1)) if not t.stable and t.hedgeable)
    flip_days = base.days_left
    stay = max(1 / 24, min(s.stay_days, flip_days)) if flip_days else s.stay_days
    op.cap_usd = (base.tvl_usd or 0) * s.max_pool_share or None
    for amount in s.amounts_usd:
        row_out: dict[str, dict[str, Variant | None]] = {}
        for case, mult in (("normal", 1.0), ("cautious", s.cautious_tvl_multiple)):
            row_out[case] = {}
            for key, hedged in (("no_hedge", False), ("hedge", True)):
                if hedged and not can_hedge:
                    row_out[case][key] = None
                    continue
                split = _split(s, config, hedged, margin_per_pool)
                inputs = pool_inputs(hedged, mult)
                ev = m.evaluate(inputs, replace(params0, c_total=amount, lp_share=split["pool"]))
                b = ev.best
                c_lp = amount * split["pool"]
                move = 2 * c_lp * 0.5 * (inputs.fee + inputs.slippage) + 4 * gas
                after = b.net - move / stay
                row_out[case][key] = Variant(
                    hedge=hedged, amount=amount, split={k: v * amount for k, v in split.items()}, income=b.income,
                    points=False, gamma=b.gamma, rebalance=b.rebalance, hedge_cost=b.hedge, haircut=b.haircut,
                    direction=b.direction_risk, net=b.net, move_cost=move, stay_days=stay, net_after_move=after,
                    apr_pct=after / amount * 365 * 100, payback_days=(move / b.net) if b.net > 0 else None,
                    in_range_ratio=b.in_range_ratio, range_pct=b.r * 100)
        op.calc[_akey(amount)] = row_out
    if volatile and not can_hedge:
        op.flags.append(Flag("NO_HEDGE", LEVEL_INFO, "保険の売り場がないので、保険なしだけ"))
    op.flags += [Flag("VENUE", LEVEL_WARN, n) for n in base.notes]
    return op


# --- まとめ ----------------------------------------------------------------------------------------

def _merkl_rows(fconn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    last = fconn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
    infos: dict[str, dict[str, Any]] = {}
    if last:
        for r in fconn.execute("""SELECT s.opportunity_id, s.native_apr, i.info_json FROM merkl_opportunity_snaps s
                                  JOIN feed_items i ON i.source='merkl_opportunities' AND i.key=s.opportunity_id
                                  WHERE s.ts=?""", (last,)):
            info = json.loads(r["info_json"] or "{}")
            info["native_apr"] = r["native_apr"]
            infos[str(r["opportunity_id"])] = info
    camps: dict[str, list[dict[str, Any]]] = {}
    cols = {r[1] for r in fconn.execute("PRAGMA table_info(merkl_campaigns)")}
    for r in fconn.execute("SELECT * FROM merkl_campaigns"):
        d = {k: r[k] for k in r.keys()}
        for k in ("distribution_method", "settings_json", "restricted", "hidden", "reward_type", "reward_verified"):
            d.setdefault(k, None) if k not in cols else None
        camps.setdefault(str(d["opportunity_id"]), []).append(d)
    return infos, camps


def collect(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> list[Opportunity]:
    """登録したチェーンの機会（自分で読んだ会場と Merkl）を計算する。"""
    now = now or datetime.now(UTC)
    fconn = _feeds_conn(config.feeds.database_path)
    out: list[Opportunity] = []
    try:
        data = FeedData(fconn, config, now)
        for b in from_own(conn, config, now):
            out.append(evaluate_own(b, conn, config, data))
        if fconn is not None:
            infos, camps = _merkl_rows(fconn)
            for b in from_merkl(config.feeds.database_path, config, now, registered_only=True):
                out.append(evaluate_merkl(b, infos.get(b.key, {}), camps.get(b.key, []), data, config))
    finally:
        if fconn is not None:
            fconn.close()
    return out


def rank(ops: list[Opportunity], amount: float, target_apr_pct: float, include_excluded: bool = False
         ) -> list[Opportunity]:
    """並べ替え: 計算できたもの → 狙い利回り以上 → 控えめの見込みの年利の高い順。外したものは include_excluded のときだけ。"""
    def key(o: Opportunity) -> tuple:
        b = o.best(amount)
        apr = b.apr_pct if b else -1e18
        return (not o.computable, o.excluded, not (b is not None and apr >= target_apr_pct), -apr)
    return sorted((o for o in ops if include_excluded or not o.excluded), key=key)


def chain_name(config: Config, cid: str | None) -> str | None:
    try:
        return load_chain(cid, config.root)["name"] if cid else None
    except ConfigError:
        return None


__all__ = ["Opportunity", "Variant", "Flag", "FeedData", "collect", "rank", "campaign_income", "budget_daily",
           "evaluate_merkl", "evaluate_own", "_days_left"]
