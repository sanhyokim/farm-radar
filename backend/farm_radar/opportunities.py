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
- ボーナスのコインの値下がり: ふつうは記録の7日の傾きで1日分を引く（今の版と同じ）。控えめは、記録がないコインに仮の値下がり
  （月 −30%。2026-10-02 オーナー決定）を当て、いる日数のあいだに下がる分も数える（stay_factor。2026-10-02 オーナー決定）。
  中身がステーブルの預かり証（チェーンの記録で確かめたもの）は月 −3%（2026-10-02 オーナー決定。SPEC 13.1 の5）。
- 予備はガス代の分だけ（チェーンごとのドル。2026-10-02 オーナー決定）。残りを機会と保険に分ける。
- 自分で読んだ会場（up. など）は、今の版の計算（scoring/model.py の evaluate）を、金額と分け方だけ変えて使い直す。
- 幅に配るプール（Merkl の UNISWAP_V3・UNISWAP_V4）で、チェーンの記録（feeds/pools.py。公式の住所と確かめたプール）があるときは、
  ボーナスの取り分を預かり額の割合ではなく流動性の割合にする（N3）: 自分の流動性 ÷（今の値段のところの流動性 × 控えめの倍数 ＋ 自分の流動性）。
  幅は今の版の承認済みの候補（±0.5%〜±15%）から、入る・出る費用のあとに残る額がいちばん多いものを選ぶ（今の版と同じ選び方）。
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

from . import safety, venue_match
from .feeds import pools as feed_pools
from .feeds import vaults as feed_vaults
from .feeds import venues as feed_venues
from .config import Config, ConfigError, OpportunitySettings, load_venue, mechanic_value
from .registry import coin_chains, load_chain, receipt_chains, stable_addresses
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
    liquidity_share: float | None = None   # 幅に配るプールで、チェーンの記録の流動性から出した取り分（N3）
    sigma_pct: float | None = None         # 計算に使った1日の値動き（%。幅に配るプールは飛びを除いたもの。N4b）
    jumps_per_day: float | None = None     # 幅を飛び越える飛び（市場が閉まっていたあとなど）の1日あたりの回数（N4b）
    rebalances_per_day: float | None = None   # 置き直しの見込みの回数（1日。補正 rebalance_factor のあと。N6 の比べに使う）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClPool:
    """チェーンの記録で読んだ、幅に配るプールの今の状態（N3。feeds/pools.py）。"""
    chain_id: int
    pool_id: str
    kind: str                        # "v3" / "v4"
    price: float                     # token0 1個あたりの token1
    liquidity: float                 # 今の値段のところの流動性（幅の中にいる人の合計）
    dec0: int
    dec1: int
    usd0: float
    usd1: float
    checked_at: str
    lp_fee: int | None


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
    venue_safety: dict[str, Any] | None = None   # 会場の危なさ（N4a。safety.py・riskscore.py）
    limits: dict[str, Any] | None = None         # 推奨金額に使う上限（config.yaml の limits。変えるのはオーナーだけ）
    vault_watch: dict[str, Any] | None = None    # 金庫の運用先の見張り（N4b。feeds/vaults.py）
    # 保険に使う売り場（コインの記号・Lighter の銘柄と番号）。値動きの大きい銘柄の行を数えるのに使う（2026-10-04 オーナーの質問2）
    hedge_markets: list[dict[str, Any]] = field(default_factory=list)
    # Merkl の分母（2026-10-07 指示書 2）: キャンペーンごとに A（全員）か B（幅の中だけ）か、と理由。split の設定のときだけ
    denominators: list[dict[str, Any]] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return any(f.level == LEVEL_EXCLUDE for f in self.flags)

    @property
    def uncertain_venue(self) -> bool:
        """会場の情報が名前だけで結びついている（「仮・会場の見分けが不確か」。N4 で契約の住所で見分ける）"""
        return bool((self.venue_safety or {}).get("uncertain_match"))

    def recommended(self, amount: float, target_apr_pct: float) -> bool:
        """練習のおすすめに入れるか: 外していない・狙い利回り以上・会場の見分けが確か（オーナー 2026-10-02 23:15 JST）。
        見分けが不確かな行も一覧には出し、自分で選んで練習はできる。"""
        b = self.best(amount)
        return not self.excluded and not self.uncertain_venue and b is not None and b.apr_pct >= target_apr_pct

    def best(self, amount: float, case: str = "cautious") -> Variant | None:
        vs = [v for v in (self.calc.get(_akey(amount)) or {}).get(case, {}).values() if v is not None]
        return max(vs, key=lambda v: v.net_after_move) if vs else None

    def to_dict(self, amount: float | None = None, target_apr_pct: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            **self.base.to_dict(), "kind": self.kind, "computable": self.computable, "reason": self.reason,
            "flags": [asdict(f) for f in self.flags], "excluded": self.excluded, "unprotected": self.unprotected,
            "cap_usd": self.cap_usd, "new_pool": self.new_pool, "vault_watch": self.vault_watch,
            "hedge_markets": self.hedge_markets, "denominators": self.denominators,
        }
        amounts = [amount] if amount is not None else [float(a) for a in self.calc]
        out["calc"] = {_akey(a): {case: {k: (v.to_dict() if v else None) for k, v in vs.items()}
                                  for case, vs in (self.calc.get(_akey(a)) or {}).items()} for a in amounts}
        if amount is not None:
            b = self.best(amount)
            out["best"] = b.to_dict() if b else None
            out["above_target"] = (b.apr_pct >= target_apr_pct) if b and target_apr_pct is not None else None
            out["recommended"] = self.recommended(amount, target_apr_pct) if target_apr_pct is not None else None
            out["over_cap"] = bool(self.cap_usd is not None and amount > self.cap_usd)
        out["venue_safety"] = self.venue_safety
        out["safety"] = safety.opportunity(out, self.venue_safety, self.limits, self.cap_usd)
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
    sigma30: float | None = None     # 30日の値動き（4時間ごとの値段から。N4b）
    grid30: tuple[tuple[int, float], ...] = ()
    sigma24: float | None = None     # 直近24時間の値動き（1日あたり。2026-10-04 オーナー決定 ①A）


FLAT_7D = 6 * 3600                   # 1時間ごとの値段: 6時間止まっていたあとの変化を「飛び」とみなす（N4b。仮）
FLAT_30D = 8 * 3600                  # 4時間ごとの値段: 8時間（2区切り）止まっていたあと
STEP_30D = 4 * 3600


@dataclass(frozen=True)
class Move:
    """2つのコインの比率（片方が値動きしないときはそのコイン）の値動き（N4b。2026-10-03 オーナーの質問1）。

    なめらかな動き（smooth）と、値段が止まっていたあとの飛び（jumps。株のコインの週末など）を分けて持つ。
    ふつうの見込みは7日分、控えめは「7日と30日の大きい方」のなめらかな動きと、飛びの多い方の期間を使う。
    """
    sigma: float                     # 7日・全部（飛びも含む。今までの値と同じ）
    smooth: float                    # 7日・飛びを除いたもの
    jumps: tuple[float, ...]         # 7日の飛び（対数の変化率）
    days: float
    sigma30: float | None = None
    smooth30: float | None = None
    jumps30: tuple[float, ...] = ()
    days30: float | None = None
    sigma24: float | None = None     # 直近24時間（全部）。2026-10-04 オーナー決定 ①A
    smooth24: float | None = None    # 直近24時間（飛びを除いたもの）

    def jumped(self, ratio: float) -> bool:
        """直近24時間の値動きが、7日の値動きの ratio 倍をこえたか（「値動きが急に大きくなった」の印）。"""
        return self.sigma24 is not None and self.sigma > 0 and self.sigma24 > ratio * self.sigma

    def case(self, cautious: bool) -> tuple[float, float, tuple[float, ...], float]:
        """(全部の値動き, なめらかな値動き, 飛び, 飛びを数えた日数)。
        控えめは、7日・30日・直近24時間のうち大きいもの（30日は N4b、24時間は 2026-10-04 オーナー決定 ①A）。"""
        if not cautious:
            return self.sigma, self.smooth, self.jumps, self.days
        sigma = max(self.sigma, self.sigma30 or 0.0, self.sigma24 or 0.0)
        smooth = max(self.smooth, self.smooth30 or 0.0, self.smooth24 or 0.0)
        if self.smooth30 is None or not self.days30:
            return sigma, smooth, self.jumps, self.days
        rate7 = sum(j * j for j in self.jumps) / self.days if self.days else 0.0
        rate30 = sum(j * j for j in self.jumps30) / self.days30
        if rate30 >= rate7:
            return sigma, smooth, self.jumps30, self.days30
        return sigma, smooth, self.jumps, self.days

    @staticmethod
    def flat(sigma: float) -> "Move":
        return Move(sigma=sigma, smooth=sigma, jumps=(), days=7.0)


def funding_long(conn: sqlite3.Connection, now: datetime, days: float,
                 table: str = "lighter_funding_history") -> dict[int, float]:
    """市場ごとの、days 日の売り（保険）の1日の支払いの平均（割合。プラス = 払う）。Lighter の資金調達率の過去
    （lighter_funding_history。1時間ごと。rate は1時間あたりの%、direction は払う側。backtest.funding_actual と同じ読み方）。
    資金調達料の控えめの見込みに使う（2026-10-04 オーナー決定 B）。"""
    if days <= 0:
        return {}
    start = int(now.timestamp() - days * 86400)
    try:
        rows = conn.execute(f"SELECT market_id, rate, direction FROM {table} WHERE ts >= ? AND ts < ? "
                            "AND rate IS NOT NULL", (start, int(now.timestamp()))).fetchall()
    except sqlite3.OperationalError:
        return {}
    by: dict[int, list[float]] = {}
    for mid, rate, direction in rows:
        by.setdefault(int(mid), []).append((-1 if direction == "long" else 1) * float(rate) / 100)
    # 半分より少ない日数の記録しかない市場は使わない（読み始めたばかりの市場で、数時間の平均にしない）
    return {mid: sum(v) / len(v) * 24 for mid, v in by.items() if len(v) >= days * 24 / 2}


_B_CACHE: dict[tuple[int, str, str, int], tuple[float | None, str | None]] = {}


class FeedData:
    """保存した一覧から、計算に使う数字を取り出す（読み取りだけ）。"""

    def __init__(self, conn: sqlite3.Connection | None, config: Config, now: datetime):
        self.conn = conn
        self.now = now
        self.s = config.opportunities
        self.coin_keys = coin_chains(config.chains, config.root)
        self._tokens: dict[str, TokenStat | None] = {}
        self.perps: dict[str, dict[str, Any]] = {}
        self.perps_rh: dict[str, dict[str, Any]] = {}       # Lighter の Robinhood Chain 版（記号ごと）
        self.withstand_table: dict[int, float] = {}
        self.receipts: dict[tuple[int, str], dict[str, Any]] = {}
        self.pools: dict[tuple[int, str], dict[str, Any]] = {}
        # N4a: 会場の登録（住所で見分ける）・工場の確かめ・DefiLlama の会場と事件の一覧
        try:
            self.known = venue_match.load_known(config.root)
        except ConfigError:
            self.known = {}
        self.known_by_protocol = venue_match.by_protocol(self.known)
        self.venue_checks: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
        self.vaults: dict[tuple[int, str], dict[str, Any]] = {}      # N4b: 金庫の運用先の見張り
        self._llama: dict[str, dict[str, Any] | None] = {}
        self.hacks: list[dict[str, Any]] | None = None
        try:
            self.stables = {cid: stable_addresses(c, config.root)
                            for cid, c in receipt_chains(config.chains, config.root).items()}
        except Exception:  # noqa: BLE001  登録が読めなくても計算は止めない（預かり証を見分けないだけ）
            self.stables = {}
        if conn is None:
            return
        self.pools = feed_pools.latest(conn, self.s.pool_state_max_age_hours, now)
        self.venue_checks = feed_venues.latest(conn)
        self.vaults = feed_vaults.latest(conn)
        hacks = [json.loads(r[0] or "{}") for r in conn.execute(
            "SELECT info_json FROM feed_items WHERE source='llama_hacks'")]
        self.hacks = hacks or None             # 一覧がまだ読めていなければ「分からない」
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_checks'").fetchone():
            for r in conn.execute("SELECT * FROM receipt_checks WHERE is_vault=1"):
                self.receipts[(int(r["chain_id"]), str(r["address"]).lower())] = {k: r[k] for k in r.keys()}
        # 耐える上げ幅（市場ごと。2026-10-04 オーナー決定 A）
        from .execution.hedge_guard import withstand_from_conn
        per_market = self.s.hedge_withstand_mode == "per_market"
        self.withstand_table = withstand_from_conn(conn, self.s.stay_days * 86400) if per_market else {}
        self.perps = self._load_perps(conn, now, "lighter_markets", "lighter_funding_snaps", "lighter_funding_history")
        # Lighter の Robinhood Chain 版（2026-10-04 オーナー決定 ②A）。記号で本体の市場と結び、本体の番号も持つ
        if self.s.lighter_rh_chain_ids and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lighter_rh_markets'").fetchone():
            rh_withstand = withstand_from_conn(conn, self.s.stay_days * 86400, "lighter_rh_price_history") \
                if per_market else {}
            for sym, p in self._load_perps(conn, now, "lighter_rh_markets", "lighter_rh_funding_snaps",
                                           "lighter_rh_funding_history").items():
                main = self.perps.get(sym)
                rh_id = int(p["market_id"])
                # 耐える上げ幅は同じ株・コインの値段の動きなので、本体と RH版の過去の長い方も使い、大きい方をとる（安全側）
                w = max(rh_withstand.get(rh_id, 0.0), self.withstand_table.get(int(main["market_id"]), 0.0) if main else 0.0)
                self.perps_rh[sym] = {**p, "book": "rh", "rh_market_id": rh_id,
                                      "market_id": main["market_id"] if main else None, "withstand": w}

    def _load_perps(self, conn: sqlite3.Connection, now: datetime, markets: str, snaps: str,
                    history: str) -> dict[str, dict[str, Any]]:
        """Lighter の1つの取引所（本体か RH版）の、使える市場ごとの資金調達料と証拠金の割合。"""
        since = (now - timedelta(days=7)).isoformat(timespec="seconds")
        funding = {r[0]: r[1] for r in conn.execute(
            f"SELECT market_id, AVG(rate_8h) FROM {snaps} WHERE ts >= ? GROUP BY market_id", (since,))}
        long_funding = funding_long(conn, now, self.s.funding_cautious_days, history)
        out: dict[str, dict[str, Any]] = {}
        for r in conn.execute(f"SELECT market_id, symbol, status, taker_pct, initial_margin_fraction, "
                              f"maintenance_margin_fraction FROM {markets}"):
            if r["status"] != "active":
                continue
            rate = funding.get(r["market_id"])
            # funding-rates の値はプラス = 買いが払う（2026-10-02 に /fundings の direction と照合）。
            # 売り（保険）の1日の支払い = −値 × 3（8時間あたり → 1日）。受け取りは収入に数えない（今の版と同じ。安全側）
            cost = None if rate is None else -rate * 3
            # 証拠金の割合: API の値 ÷ 10000（公式の表 https://docs.lighter.xyz/trading/contract-specifications と
            # 照合: CRV は API 1000 / 600、表は IMR 10% / MMR 6%。2026-10-02）
            imf, mmf = r["initial_margin_fraction"], r["maintenance_margin_fraction"]
            out[str(r["symbol"]).upper()] = {
                "market_id": r["market_id"], "symbol": r["symbol"], "funding_daily": cost, "taker_pct": r["taker_pct"],
                "funding_daily_long": long_funding.get(int(r["market_id"])), "book": "main",
                "imf": imf / 10000 if imf is not None else None, "mmf": mmf / 10000 if mmf is not None else None}
        return out

    def withstand(self, perp: dict[str, Any] | None) -> float:
        """この保険が耐える上げ幅（割合）: max(hedge_withstand_rise_pct, 14日のうちのいちばんの上げ)。fixed なら前と同じ。"""
        floor = self.s.hedge_withstand_rise_pct / 100
        if self.s.hedge_withstand_mode != "per_market" or not perp:
            return floor
        if perp.get("book") == "rh":
            return max(floor, perp.get("withstand") or 0.0)
        if perp.get("market_id") is None:
            return floor
        return max(floor, self.withstand_table.get(int(perp["market_id"]), 0.0))

    def llama_row(self, slug: str) -> dict[str, Any] | None:
        if slug not in self._llama:
            r = self.conn.execute("SELECT info_json FROM feed_items WHERE source='llama_protocols' AND key=?",
                                  (slug,)).fetchone() if self.conn is not None else None
            self._llama[slug] = json.loads(r[0] or "{}") if r else None
        return self._llama[slug]

    def venue_facts(self, slugs: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """会場の情報（危なさの点数の材料）: DefiLlama の会場（登録に書いた slug）と、その番号で結びつけた事件。"""
        rows = [r for r in (self.llama_row(str(x)) for x in slugs or []) if r]
        f: dict[str, Any] = {}
        if rows:
            big = max(rows, key=lambda r: r.get("tvl") or 0)
            listed = [r["listed_at"] for r in rows if r.get("listed_at")]
            f = {"listed_at": min(listed) if listed else None, "age_source": "DefiLlama に載った日",
                 "tvl": sum(r.get("tvl") or 0 for r in rows),
                 "audits": max((r.get("audits") or 0) for r in rows) if any(r.get("audits") is not None for r in rows)
                 else None, "audit_links": sum(r.get("audit_links") or 0 for r in rows),
                 "change_1d": big.get("change_1d"), "change_7d": big.get("change_7d"),
                 "slugs": [str(x) for x in slugs]}
            if self.hacks is not None:
                ids = {r.get("id") for r in rows if r.get("id")}
                parents = {r.get("parent") for r in rows if r.get("parent")}
                f["hacks"] = [h for h in self.hacks if (h.get("defillama_id") and h["defillama_id"] in ids)
                              or (h.get("parent_id") and h["parent_id"] in parents)]
        for k in ("admin", "bug_bounty"):
            if (extra or {}).get(k):
                f[k] = extra[k]
        return f

    def coin(self, chain_id: int | None, address: str | None) -> str | None:
        key = self.coin_keys.get(chain_id) if chain_id is not None else None
        return f"{key}:{address.lower()}" if key and address else None

    def token(self, coin: str | None) -> TokenStat | None:
        if coin is None or self.conn is None:
            return None
        if coin not in self._tokens:
            end = int(self.now.timestamp())
            start = end - 8 * 86400
            allpts = [(r[0], r[1]) for r in self.conn.execute(
                "SELECT ts, price FROM token_prices WHERE coin=? AND ts>=? AND ts<=? ORDER BY ts",
                (coin, end - 31 * 86400, end))]
            pts = [p for p in allpts if p[0] >= start]
            sym = self.conn.execute("SELECT symbol FROM token_meta WHERE coin=?", (coin,)).fetchone()
            if len(pts) < 25:
                self._tokens[coin] = None
            else:
                grid = vol.hourly_grid(pts, pts[0][0], end)
                sigma = vol.daily_sigma([r for _, r in vol.hourly_returns(grid)])
                days = max(1e-9, (grid[-1][0] - grid[0][0]) / 86400)
                trend = (grid[-1][1] / grid[0][1]) ** (1 / days) - 1 if grid[0][1] > 0 else None
                # N4b: 30日分（4時間ごと）。10日分より短ければ使わない
                g30 = vol.step_grid(allpts, allpts[0][0], end, STEP_30D)
                ok30 = len(g30) >= 60
                s30 = vol.daily_sigma([r for _, r in vol.hourly_returns(g30)], per_day=6) if ok30 else None
                s24 = vol.recent_sigma(vol.hourly_returns(grid), end)
                self._tokens[coin] = TokenStat(coin, sym[0] if sym else None, grid[-1][1], sigma, trend, tuple(grid),
                                               s30, tuple(g30) if ok30 else (), s24)
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

    def move(self, a: TokenStat | None, b: TokenStat | None) -> Move | None:
        """値動き（飛びを分けたもの）。b が None なら a だけ（もう片方は値動きしないコイン）。"""
        def one(ga, gb, flat, per_day):
            g = vol.ratio_grid(list(ga), list(gb)) if gb is not None else list(ga)
            rets = vol.hourly_returns(g)
            if len(rets) < (24 if per_day == 24 else 30):
                return None
            jt = vol.jump_times(list(ga), flat) | (vol.jump_times(list(gb), flat) if gb is not None else set())
            smooth, gaps = vol.split_jumps(rets, jt)
            total = vol.daily_sigma([r for _, r in rets], per_day=per_day)
            sm = vol.daily_sigma(smooth, min_count=1, per_day=per_day) if smooth else 0.0
            days = max(1e-9, (g[-1][0] - g[0][0]) / 86400)
            if per_day != 24:
                return total, sm, tuple(gaps), days, None, None
            end = g[-1][0]
            recent = [(t, r) for t, r in rets if t > end - 86400]
            sm24 = vol.recent_sigma([(t, r) for t, r in recent if t not in jt], end)
            return total, sm, tuple(gaps), days, vol.recent_sigma(recent, end), sm24
        if a is None:
            return None
        r7 = one(a.grid, b.grid if b else None, FLAT_7D, 24)
        if r7 is None:
            return None
        r30 = one(a.grid30, b.grid30 if b else None, FLAT_30D, 6) if a.grid30 and (b is None or b.grid30) else None
        mv = Move(sigma=r7[0], smooth=r7[1], jumps=r7[2], days=r7[3], sigma24=r7[4], smooth24=r7[5])
        if r30 is not None:
            mv = replace(mv, sigma30=r30[0], smooth30=r30[1], jumps30=r30[2], days30=r30[3])
        return mv

    def perp(self, symbol: str | None, alias: bool = True, chain_id: int | None = None) -> dict[str, Any] | None:
        """保険に使える銘柄（資金調達率と証拠金の割合が分かるものだけ）。

        chain_id が opportunities.lighter_rh_chain_ids（Robinhood Chain）なら、Lighter の Robinhood Chain 版に
        その市場があり数字がそろっていれば、その版を使う（2026-10-04 オーナー決定 ②A）。なければ本体。
        """
        if not symbol:
            return None
        aliases = {k.upper(): v for k, v in self.s.perp_alias.items()}
        sym = (aliases.get(symbol.upper(), symbol) if alias else symbol).upper()

        def usable(p: dict[str, Any] | None) -> dict[str, Any] | None:
            return p if p and p.get("funding_daily") is not None and p.get("mmf") is not None else None

        if chain_id is not None and int(chain_id) in self.s.lighter_rh_chain_ids:
            rh = usable(self.perps_rh.get(sym))
            if rh is not None:
                return rh
        return usable(self.perps.get(sym))

    def b_ratio(self, chain_id: int, pool_id: str) -> tuple[float | None, str | None]:
        """分母 B に使う「幅の中の預け方の額 ÷ 全部の預け方の額」（最新のプールの状態のとき）と、使えない理由。

        預け方の歴史（feeds/pool_history.py）を読み終えたプールだけ。組み立て直した幅の中の流動性が、チェーンで読んだ
        今の値段のところの流動性と 1% 以内で合うときだけ使う（merkl_ab と同じ確かめ方）。同じ状態の答えはとっておく。"""
        if self.conn is None:
            return None, "記録がない"
        try:
            s = self.conn.execute("SELECT checked_at, tick, liquidity, sqrt_price_x96 FROM pool_state_snaps WHERE chain_id=? "
                                  "AND pool_id=? AND liquidity IS NOT NULL AND sqrt_price_x96 IS NOT NULL AND tick IS NOT NULL "
                                  "ORDER BY checked_at DESC LIMIT 1", (int(chain_id), pool_id)).fetchone()
            prog = self.conn.execute("SELECT done_ts FROM pool_liq_progress WHERE chain_id=? AND pool_id=?",
                                     (int(chain_id), pool_id)).fetchone()
        except sqlite3.OperationalError:
            return None, "預け方の歴史の記録がない"
        if s is None:
            return None, "プールの状態の記録がない"
        if prog is None or prog["done_ts"] is None:
            return None, "預け方の歴史をまだ読み終えていない"
        key = (int(chain_id), pool_id, s["checked_at"], int(prog["done_ts"]))
        if key not in _B_CACHE:
            from .merkl_ab import Book, _denoms
            book = Book(self.conn, int(chain_id), pool_id)
            if not book.ok:
                _B_CACHE[key] = (None, "預け方の歴史をまだ読み終えていない")
            else:
                d = _denoms(book, s)
                if not d["l_ok"]:
                    _B_CACHE[key] = (None, "組み立て直した幅の中の流動性がチェーンの値と合わない")
                elif not d["ratio_v"]:
                    _B_CACHE[key] = (None, "幅の中の預け方の額が分からない")
                else:
                    _B_CACHE[key] = (float(d["ratio_v"]), None)
            if len(_B_CACHE) > 500:
                _B_CACHE.pop(next(iter(_B_CACHE)))
        return _B_CACHE[key]

    def stable_receipt(self, chain_id: int | None, address: str | None) -> dict[str, Any] | None:
        """中身がステーブルの預かり証なら、その確かめの記録（SPEC 13.1 の5）。

        条件: チェーンの記録で asset() と convertToAssets() に答えた（金庫の形）、中身の住所が登録したステーブルコイン、
        契約の中身が公開・確認済み（Blockscout か Sourcify）。どれかが欠けたら None（ほかのコインと同じく仮の値下がり）。
        """
        if chain_id is None or not address:
            return None
        r = self.receipts.get((int(chain_id), address.lower()))
        if not r or not r.get("asset") or r.get("verified") != 1:
            return None
        sym = self.stables.get(int(chain_id), {}).get(str(r["asset"]).lower())
        return dict(r, stable_symbol=sym) if sym else None

    def receipt_note(self, chain_id: int | None, address: str | None) -> str | None:
        """金庫の預かり証だが、中身がステーブルと言えない理由（印に書く）。預かり証でなければ None。"""
        if chain_id is None or not address:
            return None
        r = self.receipts.get((int(chain_id), address.lower()))
        if not r:
            return None
        if r.get("asset") and str(r["asset"]).lower() not in self.stables.get(int(chain_id), {}):
            return f"金庫の預かり証だが、中身（{r.get('asset_symbol') or '不明なコイン'}）がステーブルではない"
        if r.get("verified") != 1:
            return "金庫の預かり証だが、契約の中身が公開・確認済みか分からない"
        return None

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
    from .execution.hedge_guard import margin_need as need      # 練習（paper）と同じ式（2026-10-03）

    return need(perp.get("imf"), perp.get("mmf"), withstand_rise)


def reserve_usd(s: OpportunitySettings, chain: str | None) -> float:
    """予備（ガス代の分）のドル。チェーンごと（2026-10-02 13:44 JST オーナー決定）。書いていないチェーンは一番大きい値。"""
    return s.reserve_usd.get(chain or "", max(s.reserve_usd.values(), default=0.0))


def _split(hedge: bool, margin_per_pool: float = 0.0, reserve: float = 0.0) -> dict[str, float]:
    """総額を、機会に置く分・保険に預ける分・予備に分ける（割合）。reserve は予備の割合（ガス代のドル ÷ 総額）。

    保険あり: 予備を引いた残りを P ×（1 ＋ 保険に預ける割合）に分ける（2026-10-02 オーナー決定「自動で計算」）。
    margin_per_pool = 機会に置く1ドルあたりに保険へ預けるお金（売る量の割合 × margin_need の合計）。
    保険なし: 予備のほかは全部、機会に置く。予備は保険が要らない場所でも残す（2026-10-02 13:44 JST オーナー決定）。
    """
    reserve = min(max(reserve, 0.0), 1.0)
    if hedge:
        pool = (1 - reserve) / (1 + margin_per_pool)
        return {"pool": pool, "hedge_margin": pool * margin_per_pool, "reserve": reserve}
    return {"pool": 1 - reserve, "hedge_margin": 0.0, "reserve": reserve}


def provisional_trend(s: OpportunitySettings) -> float:
    """値段の記録がないボーナスのコインの、仮の1日の変化（月 −30% → 1日 約 −1.18%）。"""
    return (1 - s.unknown_reward_drop_monthly_pct / 100) ** (1 / 30) - 1


def receipt_trend(s: OpportunitySettings) -> float:
    """中身がステーブルの預かり証の仮の値下がり（1日あたり）。月 −3%（2026-10-02 14:00 JST オーナー決定）。"""
    return (1 - s.receipt_stable_drop_monthly_pct / 100) ** (1 / 30) - 1


def stay_factor(trend_daily: float | None, days: float) -> float:
    """いる日数のあいだのボーナスのコインの平均の値段 ÷ 今の値段（下がるときだけ。2026-10-02 オーナー決定待ちの案）。

    毎日受け取って売るとき、t 日目の値打ちは (1 + r)^t 倍。0〜days 日の平均 = ((1+r)^days − 1) ÷ (days × ln(1+r))。
    """
    if trend_daily is None or trend_daily >= 0 or days <= 0:
        return 1.0
    g = math.log1p(trend_daily)
    return math.expm1(g * days) / (g * days)


def cl_share(c_pos: float, r: float, cl: ClPool, crowd: float) -> tuple[float, float]:
    """幅 ±r に c_pos ドル置いたときの (自分の流動性, ボーナスの取り分)。取り分 = 自分 ÷ (今の流動性 × crowd ＋ 自分)。

    今の版の Alandale（幅の中の流動性で分ける会場）と同じ考え方（scoring/model.py の evaluate）。
    """
    t0 = m.TokenSide(usd=cl.usd0, decimals=cl.dec0, sigma_usd=0.0)
    t1 = m.TokenSide(usd=cl.usd1, decimals=cl.dec1, sigma_usd=0.0)
    mine = m.liquidity_for_usd(c_pos, cl.price, r, t0, t1)
    return mine, (mine / (cl.liquidity * crowd + mine) if mine > 0 else 0.0)


def _variant(*, hedge: bool, amount: float, split: dict[str, float], kind: str, legs: list[Leg], move: Move | None,
             campaigns: list[dict[str, Any]], tvl: float, base_apr_pct: float, cautious: bool,
             s: OpportunitySettings, config: Config, gas: float, fee: float, stay_days: float,
             cl: ClPool | None = None, r: float | None = None, crowd: float = 1.0) -> Variant:
    """campaigns の各キャンペーンには evaluate_merkl が `_volatile`（ボーナスのコインが値動きする）と
    `_trend`（そのコインの7日の傾き。記録がなければ None）を付けておく。

    ボーナスのコインの値下がり: ふつうは記録の傾きで1日分を引く（今の版の承認済みの式）。
    控えめは、記録がなければ仮の値下がり（月 −30%）を当て、いる日数のあいだに下がる分も数える（stay_factor）。

    値動き（N4b。2026-10-03 オーナーの質問1）: 幅に配るプールは、なめらかな動きで置き直す回数と目減りを数え、
    値段が止まっていたあとの飛び（株のコインの週末など）は1回ずつ数える（幅を飛び越えたら置き直し1回と、その損）。
    控えめは、なめらかな動きを7日と30日の大きい方にする。
    """
    c_pos = amount * split["pool"]
    r = r if r is not None else s.merkl_range_pct / 100
    lshare = cl_share(c_pos, r, cl, crowd)[1] if cl is not None and kind == "pool_range" else None
    income, points, haircut = 0.0, False, 0.0
    for c in campaigns:
        if (c.get("reward_type") or "TOKEN").upper() != "TOKEN":
            points = True
            continue
        if lshare is not None and c.get("_cl") and (c.get("distribution_type") or "").upper() == "DUTCH_AUCTION":
            # 幅に配る山分け（N3。Merkl の資料 concentrated-liquidity-mechanisms）: 予算を重みで3つに分ける。
            # 手数料の重み（流動性への貢献 = 今の値段のところの流動性の割合）は流動性の割合で分ける。
            # コイン0・コイン1の重みは「プールの中のそのコインの量の割合」。分母（幅の中の人だけか、全員か）が資料で分からないので、
            # 控えめに預かり額の割合（全員の中の割合。幅が狭い人には小さく出る）で分ける
            d = c.get("daily_rewards") if c.get("daily_rewards") is not None else budget_daily(c)
            st = _settings(c)
            w_fee = float(st.get("weightFees") or 0) / 10000
            w_tok = float(st.get("weightToken0") or 0) / 10000 + float(st.get("weightToken1") or 0) / 10000
            if w_fee + w_tok <= 0:
                w_fee = 1.0
            # 分母 B（2026-10-07 指示書 2）: 条件がそろったキャンペーンだけ、幅の中の預け方の額（預かり額 × 幅の中の割合）で分ける
            denom = max(tvl, 0.0) * float(c["_b_ratio"]) if c.get("_b_ratio") is not None else max(tvl, 0.0)
            dshare = c_pos / (denom + c_pos) if c_pos > 0 else 0.0
            inc = (d or 0.0) * (w_fee * lshare + w_tok * dshare) / (w_fee + w_tok)
        else:
            inc, _ = campaign_income(c, c_pos, tvl)
        if c.get("_volatile"):
            trend = c.get("_trend")
            if cautious:
                if trend is None:
                    trend = receipt_trend(s) if c.get("_receipt") else provisional_trend(s)
                inc *= stay_factor(trend, stay_days)
            if trend is not None and trend < 0:
                haircut += inc * -trend
        income += inc
    irr = None
    gamma_day = reb = n_reb = 0.0
    swap_ratio = config.scoring.swap_ratio
    sig_total, sig_smooth, jumps, jdays = move.case(cautious) if move is not None else (None, None, (), 1.0)
    forced = None
    if kind == "pool_range" and move is not None:
        forced = sum(1 for j in jumps if abs(j) > r) / jdays
        # 置き直しの見込みの補正（2026-10-07 指示書 3。探すは 1、N6 の練習は仮 0.25）。飛びの回数には掛けない
        n_reb = m.rebalances_per_day(sig_smooth, r) * s.rebalance_factor + forced
        irr = max(0.0, 1.0 - n_reb * config.scoring.rebalance_wait_minutes / m.MINUTES_PER_DAY)
        income *= irr
        gamma_day = m.gamma(c_pos, sig_smooth, r) + sum(m.jump_loss(c_pos, j, r) for j in jumps) / jdays
        reb = n_reb * (gas * 2 + c_pos * swap_ratio * fee)
    elif kind == "pool_full" and move is not None:
        gamma_day = c_pos * sig_total ** 2 / 8
    if kind == "pool_range" and irr is not None:
        haircut *= irr                                     # 幅の外にいる間はボーナスも値下がりの分もない
    income += c_pos * base_apr_pct / 100 / 365            # 元の利回り（貸し出しの利息など。分かるときだけ）
    exposure = c_pos * 0.5 if kind.startswith("pool") else c_pos      # 1つのコインあたりの値動きの量
    hedge_cost = direction = 0.0
    hedged_notional = 0.0
    for leg in legs:
        if leg.stable:
            continue
        if hedge and leg.perp is not None:
            cost = leg.perp["funding_daily"]
            if cautious and leg.perp.get("funding_daily_long") is not None:
                cost = max(cost, leg.perp["funding_daily_long"])   # 控えめは 7日と30日の悪い方（2026-10-04 オーナー決定 B）
            cost = cost if config.scoring.count_funding_income else max(0.0, cost)
            taker = (leg.perp.get("taker_pct") or 0.0) / 100
            hedge_cost += exposure * cost + n_reb * exposure * taker
            hedged_notional += exposure
        else:
            sig = leg.stat.sigma if leg.stat and leg.stat.sigma is not None else 0.0
            if cautious and leg.stat:
                # 控えめは 7日・30日・直近24時間の大きい方（30日は N4b、24時間は 2026-10-04 オーナー決定 ①A）
                sig = max(sig, leg.stat.sigma30 or 0.0, leg.stat.sigma24 or 0.0)
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
                   in_range_ratio=irr, range_pct=r * 100 if kind == "pool_range" else None, liquidity_share=lshare,
                   sigma_pct=(sig_smooth if kind == "pool_range" else sig_total) * 100 if move is not None else None,
                   jumps_per_day=forced, rebalances_per_day=n_reb if kind == "pool_range" and move is not None else None)


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


def _pool_state(campaigns: list[dict[str, Any]], base: StandardOpportunity, data: FeedData) -> dict[str, Any] | None:
    """幅に配るキャンペーンのプールの、チェーンの記録（N3）。会場の見分け（Uniswap の公式の住所か）に使う。"""
    if base.evm_chain_id is None:
        return None
    for c in campaigns:
        pid = _settings(c).get("poolId")
        if pid:
            st = data.pools.get((int(base.evm_chain_id), str(pid).lower()))
            if st is not None:
                return st
    return None


def vol_jump_flag(move: Move | None, legs: list[Leg], ratio: float) -> Flag | None:
    """「値動きが急に大きくなった」の印（2026-10-04 オーナー決定 ①A）: 直近24時間の値動きが7日の ratio 倍をこえた。
    控えめの見込みは、7日・30日・直近24時間の大きいもので計算している（Move.case・_variant）。"""
    parts = []
    if move is not None and move.jumped(ratio):
        parts.append(f"2つのコインの比率は直近24時間 {move.sigma24 * 100:.1f}%／7日 {move.sigma * 100:.1f}%")
    for lg in legs:
        st = lg.stat
        if st and st.sigma24 is not None and st.sigma and st.sigma24 > ratio * st.sigma:
            parts.append(f"{lg.symbol} は直近24時間 {st.sigma24 * 100:.1f}%／7日 {st.sigma * 100:.1f}%")
    if not parts:
        return None
    return Flag("VOL_JUMP", LEVEL_WARN, f"値動きが急に大きくなった（1日あたりの値動き。{ratio:g}倍をこえた）: "
                f"{'、'.join(parts)}。控えめの見込みは大きい方で計算")


def evaluate_merkl(base: StandardOpportunity, info: dict[str, Any], campaigns: list[dict[str, Any]], data: FeedData,
                   config: Config) -> Opportunity:
    s = config.opportunities
    op = Opportunity(base=base, kind=_kind(info), campaigns=[_campaign_view(c) for c in campaigns],
                     limits=dict(config.limits))
    # N4a: どの会場かを契約の住所で見分ける。見分けた会場だけ、その会場の情報（DefiLlama・登録）を危なさの点数に使う
    identity = venue_match.identify(info.get("protocol"), base.chain, base.evm_chain_id, info.get("explorer_address"),
                                    data.known_by_protocol, data.venue_checks, _pool_state(campaigns, base, data),
                                    kind=info.get("type"))
    known = data.known.get(identity.get("venue_id") or "")
    facts = data.venue_facts(((known or {}).get("defillama") or {}).get("slugs") or [], known) \
        if identity["status"] == "verified" and known else None
    op.venue_safety = safety.venue_from_merkl(identity, facts, info.get("trust"), data.now)
    now_s = int(data.now.timestamp())
    live = [c for c in campaigns if (c.get("start_ts") or 0) <= now_s and (c.get("end_ts") is None or c["end_ts"] > now_s)]
    tvl = base.tvl_usd or 0.0
    _flags(op, info, live, base, s, now_s)
    _vault_flags(op, base, info, data)
    if identity["status"] != "verified":
        note = safety.match_note(info.get("trust"))
        op.flags.append(Flag("VENUE_MATCH", LEVEL_INFO, f"{safety.UNCERTAIN_MATCH}: {identity['reason']}"
                             + (f"。{note.split(': ', 1)[1]}" if note else "")))
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
        legs.append(Leg(sym, stat, stable, None if stable else data.perp(sym, chain_id=base.evm_chain_id)))
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
    move: Move | None = Move.flat(0.0)
    if op.kind.startswith("pool") and len(legs) >= 2:
        a, b = legs[0], legs[1]
        if a.stable and b.stable:
            move = Move.flat(0.0)
        elif a.stat and b.stat:
            move = data.move(a.stat, b.stat)
        else:
            move = data.move(a.stat or b.stat, None)
        if move is None:
            op.computable, op.reason = False, "2つのコインの比率の値動きが分からない"
            return op
    jump = vol_jump_flag(move if op.kind.startswith("pool") and len(legs) >= 2 else None,
                         [lg for lg in legs if not lg.stable], s.vol_jump_ratio)
    if jump is not None:
        op.flags.append(jump)
    # ボーナスのコインの値動き（同じチェーンで値段の記録があるときだけ）。キャンペーンごとに印を付けて _variant で使う
    reward_syms, guessed, receipt_syms = set(), set(), set()
    guess_notes: dict[str, str] = {}
    receipt_flags: dict[str, list[Flag]] = {}
    marked = []
    for c in live:
        c = dict(c, _volatile=False, _trend=None, _receipt=False)
        if (c.get("reward_type") or "TOKEN").upper() == "TOKEN":
            dcid, raddr = c.get("distribution_chain_id"), c.get("reward_address")
            rs = data.token(data.coin(dcid, raddr))
            if not data.is_stable(rs, c.get("reward_symbol")):
                sym = c.get("reward_symbol") or "?"
                c["_volatile"] = True
                c["_trend"] = rs.trend_daily if rs is not None else None
                rec = data.stable_receipt(dcid, raddr) if c["_trend"] is None else None
                if rec is not None:
                    c["_receipt"] = True
                    receipt_syms.add(sym)
                    receipt_flags.setdefault(sym, _receipt_flags(sym, rec, c.get("reward_price"), s))
                else:
                    reward_syms.add(sym)
                    if c["_trend"] is None:
                        guessed.add(sym)
                        note = data.receipt_note(dcid, raddr)
                        if note:
                            guess_notes[sym] = note
        marked.append(c)
    live = marked
    op.unprotected = [f"ボーナスのコイン（{x}）の値下がり" for x in sorted(reward_syms)]
    op.unprotected += [f"ボーナスの預かり証（{x}）の金庫の損・引き出しの待ち" for x in sorted(receipt_syms)]
    for sym in sorted(receipt_flags):
        op.flags.extend(receipt_flags[sym])

    op.unprotected += [f"{lg.symbol} の値下がり（保険の売り場がない）" for lg in legs if not lg.stable and lg.perp is None]
    if guessed:
        notes = "".join(f"（{x}: {guess_notes[x]}）" for x in sorted(guess_notes))
        op.flags.append(Flag("RWD_GUESS", LEVEL_WARN, f"値下がり未計算（仮の値で計算）: ボーナスのコイン（{'・'.join(sorted(guessed))}）の"
                             f"値段の記録がない。控えめの見込みは月 −{s.unknown_reward_drop_monthly_pct:g}% とみなした{notes}"))
    base_apr = (_float(info.get("native_apr")) or 0.0) if op.kind == "hold" else 0.0
    gas = s.gas_usd_per_tx.get(base.chain or "", max(s.gas_usd_per_tx.values(), default=0.2))
    fee = s.pool_fee_pct / 100
    ends = [c["end_ts"] for c in live if c.get("end_ts")]
    days_left = (max(ends) - now_s) / 86400 if ends else None
    stay = max(1 / 24, min(s.stay_days, days_left)) if days_left is not None else s.stay_days
    volatile = [lg for lg in legs if not lg.stable]
    can_hedge = any(lg.perp for lg in volatile)
    op.hedge_markets = [{"coin": lg.symbol, "symbol": lg.perp.get("symbol"), "market_id": lg.perp.get("market_id"),
                         "book": lg.perp.get("book", "main"), "rh_market_id": lg.perp.get("rh_market_id")}
                        for lg in volatile if lg.perp]
    if volatile and not can_hedge:
        op.flags.append(Flag("NO_HEDGE", LEVEL_INFO, "保険の売り場（Lighter）がないので、保険なしだけ"))
    op.cap_usd = tvl * s.max_pool_share if tvl else None
    cl = _cl_pool(op, base, live, legs, data) if op.kind == "pool_range" else None
    if s.merkl_denominator == "split" and op.kind == "pool_range":
        _mark_denominators(op, base, live, cl, data)
    ranges = RANGES if cl is not None else (s.merkl_range_pct / 100,)
    if cl is not None and cl.lp_fee is not None and 0 < cl.lp_fee < 1_000_000:
        fee = cl.lp_fee / 1_000_000            # このプールの手数料の段（チェーンの記録。両替・置き直しの費用に使う）
    expo = 0.5 if op.kind.startswith("pool") else 1.0
    margin_per_pool = sum(expo * margin_need(lg.perp, data.withstand(lg.perp)) for lg in volatile if lg.perp)
    for amount in s.amounts_usd:
        row: dict[str, dict[str, Variant | None]] = {}
        for case, mult in (("normal", 1.0), ("cautious", s.cautious_tvl_multiple)):
            kw = dict(amount=amount, kind=op.kind, legs=legs, move=move, campaigns=live,
                      tvl=tvl * mult, base_apr_pct=base_apr, cautious=case == "cautious", s=s, config=config,
                      gas=gas, fee=fee, stay_days=stay)
            res = reserve_usd(s, base.chain) / amount
            kw.update(cl=cl, crowd=mult)

            def best_range(**v: Any) -> Variant:
                # 幅は承認済みの候補から、入る・出る費用のあとに残る額がいちばん多いもの（記録がなければ ±15% だけ）
                return max((_variant(r=r, **v) for r in ranges), key=lambda x: x.net_after_move)
            row[case] = {
                "no_hedge": best_range(hedge=False, split=_split(False, reserve=res), **kw),
                "hedge": best_range(hedge=True, split=_split(True, margin_per_pool, res), **kw) if can_hedge else None,
            }
        op.calc[_akey(amount)] = row
    # 残りの日数で入る・出る費用を取り返せない（$1,000・控えめ・良い方で判断）
    b = op.best(1000.0 if 1000.0 in s.amounts_usd else s.amounts_usd[0])
    if b is not None and days_left is not None and (b.payback_days is None or b.payback_days > days_left):
        op.flags.append(Flag("PAYBACK", LEVEL_EXCLUDE, "残りの日数で、入る・出る費用を取り返せない"))
    return op


VAULT_RECENT_DAYS = 7          # この日数のうちに運用先が変わったら注意の印（仮）


def _vault_flags(op: Opportunity, base: StandardOpportunity, info: dict[str, Any], data: FeedData) -> None:
    """金庫の運用先の見張り（N4b。2026-10-03 オーナー）: 見張っている金庫なら印を付け、最近変わったら注意にする。"""
    addr = str(info.get("explorer_address") or "").lower()
    v = data.vaults.get((int(base.evm_chain_id), addr)) if base.evm_chain_id is not None and addr else None
    if v is None:
        return
    st = v.get("state") or {}
    op.vault_watch = {"checked_at": v.get("checked_at"), "adapters": st.get("adapters_count"),
                      "changes_30d": v.get("changes") or 0, "last_change": v.get("last_change"), "error": v.get("error")}
    last = v.get("last_change")
    recent = False
    if last:
        try:
            recent = (data.now - datetime.fromisoformat(last)).total_seconds() < VAULT_RECENT_DAYS * 86400
        except ValueError:
            recent = False
    if recent:
        op.flags.append(Flag("VAULT_CHANGED", LEVEL_WARN, f"金庫の運用先が変わった（{last[:16]}。チェーンの記録）。"
                             "運営が仕組みを変えた合図（早く出る段階1）"))
    else:
        op.flags.append(Flag("VAULT_WATCH", LEVEL_INFO, f"金庫の運用先を1時間に1回見張っている（運用先 {st.get('adapters_count', '?')}つ・"
                             f"最近30日の変化 {v.get('changes') or 0}回。チェーンの記録）"))


def _mark_denominators(op: Opportunity, base: StandardOpportunity, live: list[dict[str, Any]], cl: ClPool | None,
                       data: FeedData) -> None:
    """Merkl の分母の使い分け（2026-10-07 指示書 2）。B（幅の中だけ）は次の全部がそろったキャンペーンだけ:
    幅の外には配らない設定（isOutOfRangeIncentivized: false）・除外する人／対象の人の決まりがない・プールのチェーンの記録
    （今の状態と、預け方の歴史を読み終えて組み立て直した幅の中の流動性がチェーンの値と合う）を読めている。ほかは A。
    コインの重みが中心のキャンペーンは、N5 で比べた組が0件（B の根拠がまだない）なので、印にそう書く。"""
    from .merkl_ab import main_weight, out_class, restricted, weights

    for c in live:
        if not c.get("_cl"):
            continue
        st = _settings(c)
        why = None
        oc = out_class(st)
        if oc != "in":
            why = "幅の外にも配る設定" if oc == "out" else "幅の外の扱いが設定にない"
        elif restricted(st):
            why = restricted(st)
        elif cl is None:
            why = "プールのチェーンの記録を使えない"
        ratio = None
        if why is None:
            ratio, why = data.b_ratio(cl.chain_id, cl.pool_id)
        mw = main_weight(weights(st))
        if ratio is not None:
            c["_b_ratio"] = ratio
        op.denominators.append({"campaign_id": c.get("campaign_id"), "use": "B" if ratio is not None else "A",
                                "why": why, "in_range_share": ratio, "main_weight": mw,
                                "unverified": mw == "コインが中心"})
    used = [d for d in op.denominators if d["use"] == "B"]
    if used:
        coin = any(d["unverified"] for d in used)
        op.flags.append(Flag("DENOM_B", LEVEL_INFO,
                             f"Merkl の分母は B（幅の中の預け方だけ）で計算: 幅の中の割合 {used[0]['in_range_share'] * 100:.0f}%"
                             "（チェーンの記録）" + ("。コインの重みが中心のキャンペーンは、N5 で比べた組が0件（B はまだ確かめていない）"
                                                 if coin else "")))


RANGES = m.ModelParams().ranges          # 幅の候補（今の版で承認済み: ±0.5%〜±15%）


def _cl_pool(op: Opportunity, base: StandardOpportunity, live: list[dict[str, Any]], legs: list[Leg],
             data: FeedData) -> ClPool | None:
    """幅に配るキャンペーンのプールの、チェーンの記録（N3）。使えないときは印に理由を書いて None。"""
    params = None
    for c in live:
        st = _settings(c)
        if (str(c.get("type") or "").upper() in feed_pools.POOL_TYPES and st.get("poolId")
                and st.get("decimalsCurrency0") is not None and "weightFees" in st):
            c["_cl"] = True
            params = params or st
    if params is None or base.evm_chain_id is None:
        op.flags.append(Flag("RANGE", LEVEL_INFO, f"幅に配るプール。幅 ±{data.s.merkl_range_pct:g}% で計算（仮。"
                             "このプールの読み方がまだ無いので、預かり額の割合で分けた）"))
        return None
    state = data.pools.get((int(base.evm_chain_id), str(params["poolId"]).lower()))
    why = None
    if state is None:
        why = "チェーンの記録がまだ無い（1時間に1回読む）"
    elif state.get("official") != 1:
        why = state.get("error") or "公式の住所と確かめられない"
    elif state.get("hooks") is None:
        why = "このプールに追加の仕組み（フック）があるか分からない"
    elif str(state["hooks"]).lower() != feed_pools.ZERO:
        # フック付きのプールは、流動性を自動で置き直すなど置き方が違うことがあるので、流動性の割合では計算しない
        why = "このプールには追加の仕組み（フック）があり、置き方が違うことがある"
    if why is None:
        usd = {}
        for i in (0, 1):
            addr, sym = params.get(f"currency{i}"), params.get(f"symbolCurrency{i}")
            stat = data.token(data.coin(base.evm_chain_id, addr)) or data.token_by_symbol(base.evm_chain_id, sym)
            stable = data.is_stable(stat, sym)
            usd[i] = stat.price if stat is not None else (1.0 if stable else None)
        if usd[0] is None or usd[1] is None:
            why = "コインのドルの値段が分からない"
    if why is not None:
        op.flags.append(Flag("RANGE", LEVEL_INFO, f"幅に配るプール。幅 ±{data.s.merkl_range_pct:g}% で計算（仮。{why}ので、"
                             "預かり額の割合で分けた）"))
        return None
    cl = ClPool(chain_id=int(base.evm_chain_id), pool_id=str(params["poolId"]).lower(), kind=state["kind"],
                price=float(state["price"]), liquidity=float(state["liquidity"]), dec0=int(params["decimalsCurrency0"]),
                dec1=int(params["decimalsCurrency1"]), usd0=float(usd[0]), usd1=float(usd[1]),
                checked_at=state["checked_at"], lp_fee=state.get("lp_fee"))
    w = [params.get(k) for k in ("weightFees", "weightToken0", "weightToken1")]
    weights = "・".join(f"{x / 100:g}%" for x in w if x is not None)
    op.flags.append(Flag("RANGE_CHAIN", LEVEL_INFO,
                         f"幅に配るプール。ボーナスの取り分は、チェーンの記録の流動性（{cl.checked_at[11:16]} UTC に読んだ、"
                         f"今の値段のところの流動性）で計算。幅は ±0.5%〜±15% から選んだ。Merkl の重み（手数料・コイン0・コイン1）{weights}"))
    return cl


def _receipt_flags(sym: str, rec: dict[str, Any], reward_price: float | None, s: OpportunitySettings) -> list[Flag]:
    """中身がステーブルの預かり証の印（見分けた理由）と、値段が中身から外れたときの知らせ（SPEC 13.1 の5）。"""
    per = rec.get("assets_per_share")
    day = str(rec.get("checked_at") or "")[:10]
    by = {"blockscout": "Blockscout", "sourcify": "Sourcify"}.get(rec.get("verified_by") or "", "公開の記録")
    name = rec.get("name") or sym
    inside = f"1枚 = {per:.4f} {rec['stable_symbol']}" if per else rec["stable_symbol"]
    out = [Flag("RECEIPT", LEVEL_INFO,
                f"預かり証と見分けた: {sym} は「{name}」の金庫の預かり証。中身は {rec['stable_symbol']}（{inside}）。"
                f"チェーンの記録で確かめた（{day}。契約の中身は {by} で公開・確認済み）。"
                f"控えめの見込みは月 −{s.receipt_stable_drop_monthly_pct:g}% とみなした")]
    if per and reward_price:
        gap = (reward_price - per) / per * 100      # 中身のステーブルは $1 とみなす
        if abs(gap) >= s.receipt_price_alert_pct:
            out.append(Flag("RECEIPT_OFF", LEVEL_WARN,
                            f"値段が中身から外れている: {sym} の Merkl の値段 ${reward_price:.4f} と中身の値段 ${per:.4f} の差が "
                            f"{gap:+.1f}%（知らせの線 {s.receipt_price_alert_pct:g}%）"))
    return out


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
    if op.kind.startswith("pool"):
        op.flags.append(Flag("NO_FEES", LEVEL_INFO, "プールの手数料の収入は数えていない（分からないため。安全側）"))


# --- 自分で読んだ会場（up. など） ------------------------------------------------------------------

def evaluate_own(base: StandardOpportunity, conn: sqlite3.Connection, config: Config, data: FeedData) -> Opportunity:
    """今の版の計算（scoring/model.py）を、金額と分け方だけ変えて使い直す。"""
    s = config.opportunities
    op = Opportunity(base=base, kind="pool_range", limits=dict(config.limits))
    row = conn.execute("""SELECT s.details_json, p.token0_symbol, p.token1_symbol, p.token0_decimals, p.token1_decimals,
                                 (SELECT unstaked_fee FROM pool_snapshots WHERE pool_id=p.id ORDER BY ts DESC LIMIT 1) uf
                          FROM scores s JOIN pools p ON p.id=s.pool_id WHERE s.pool_id=? ORDER BY s.ts DESC LIMIT 1""",
                       (base.key,)).fetchone()
    inp = json.loads(row["details_json"] or "{}").get("inputs") if row else None
    try:
        venue = load_venue(base.venue, config.root)
    except (OSError, ConfigError):
        venue = {}
    llama = data.venue_facts(list((venue.get("listings") or {}).get("defillama") or [])) if venue else None
    op.venue_safety = safety.venue_from_registry(venue, llama, data.now) if venue else None
    if not inp or inp.get("sigma_pair") is None:
        op.computable, op.reason = False, "スコアの計算に必要な数字がそろっていない"
        return op
    syms = (row["token0_symbol"], row["token1_symbol"])
    decs = (row["token0_decimals"], row["token1_decimals"])
    hedge_info = inp.get("hedge") or {}

    def margin_perp(sym: str) -> dict[str, Any] | None:
        h = hedge_info.get(sym) or {}
        return data.perp(h.get("symbol"), alias=False, chain_id=base.evm_chain_id) if h.get("hedge_id") == "lighter" else None

    def side(i: int, hedged: bool, cautious: bool = False) -> m.TokenSide | None:
        sym = syms[i]
        usd, sig = (inp.get("usd") or {}).get(sym), (inp.get("sigma_token") or {}).get(sym)
        if usd is None or sig is None or decs[i] is None:
            return None
        stable = sig == 0
        if cautious:
            sig = max(sig, (inp.get("sigma_token_24h") or {}).get(sym) or 0.0)   # 控えめは直近24時間とも比べる（①A）
        h = hedge_info.get(sym) or {}
        # 保険は、証拠金の割合が分かる売り場（Lighter）のときだけ掛ける（分け方を自動で計算するため）
        mp = margin_perp(sym)
        fund = h.get("funding_daily") if hedged and h.get("hedge_id") and mp else None
        taker = h.get("taker_pct")
        if fund is not None and mp.get("book") == "rh":
            fund, taker = mp["funding_daily"], mp.get("taker_pct")   # RH版の資金調達料と手数料（②A）
        long_f = (mp or {}).get("funding_daily_long") if fund is not None and cautious else None
        if long_f is not None:
            fund = max(float(fund), long_f)             # 控えめは 7日と30日の悪い方（2026-10-04 オーナー決定 B）
        return m.TokenSide(usd=usd, decimals=int(decs[i]), sigma_usd=sig, stable=stable,
                           hedgeable=fund is not None, funding_cost_daily=fund or 0.0,
                           taker_fee=(taker or 0.0) / 100 if fund is not None else None)

    trend_rec = inp.get("reward_token_trend_daily")

    def pool_inputs(hedged: bool, mult: float, cautious: bool = False) -> m.PoolInputs | None:
        t0, t1 = side(0, hedged, cautious), side(1, hedged, cautious)
        if t0 is None or t1 is None:
            return None
        # 控えめ: 記録がなければ仮の値下がり（月 −30%）を当て、いる日数のあいだに下がる分も数える（evaluate_merkl と同じ）
        trend = (trend_rec if trend_rec is not None else provisional_trend(s)) if cautious else trend_rec
        reward_day = float(inp.get("reward_usd_day") or 0) * (stay_factor(trend, stay) if cautious else 1.0)
        lt = int((inp.get("liquidity_latest") or {}).get("total") or inp.get("liquidity_total") or 0)
        ls = int((inp.get("liquidity_latest") or {}).get("staked") or inp.get("liquidity_staked_inrange") or 0)
        med = inp.get("liquidity_median_24h") or {}
        lt, ls = max(lt, int(med.get("total") or 0)), max(ls, int(med.get("staked") or 0))
        return m.PoolInputs(price=float(inp["price"]), token0=t0, token1=t1, fee=float(inp.get("fee") or 0),
                            unstaked_fee=(row["uf"] or 0) / 1e6, liquidity_total=int(lt * mult),
                            liquidity_staked=int(ls * mult), reward_usd_day=reward_day,
                            fees_usd_day=inp.get("fees_usd_day"),
                            sigma_pair=max(float(inp["sigma_pair"]), float(inp.get("sigma_pair_24h") or 0.0))
                            if cautious else float(inp["sigma_pair"]),
                            reward_trend_daily=trend,
                            slippage=float(inp.get("slippage") or 0),
                            rewards_only=mechanic_value(venue, "lp_receives_swap_fees", True) is False)

    flip_days = base.days_left
    stay = max(1 / 24, min(s.stay_days, flip_days)) if flip_days else s.stay_days
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
    margin_per_pool = sum(0.5 * margin_need(margin_perp(syms[i]), data.withstand(margin_perp(syms[i])))
                          for i, t in enumerate((probe.token0, probe.token1)) if not t.stable and t.hedgeable)
    op.hedge_markets = [{"coin": syms[i], "symbol": hp.get("symbol"), "market_id": hp.get("market_id"),
                         "book": hp.get("book", "main"), "rh_market_id": hp.get("rh_market_id")}
                        for i, t in enumerate((probe.token0, probe.token1))
                        if not t.stable and t.hedgeable and (hp := margin_perp(syms[i]))]
    op.cap_usd = (base.tvl_usd or 0) * s.max_pool_share or None
    for amount in s.amounts_usd:
        row_out: dict[str, dict[str, Variant | None]] = {}
        for case, mult in (("normal", 1.0), ("cautious", s.cautious_tvl_multiple)):
            row_out[case] = {}
            for key, hedged in (("no_hedge", False), ("hedge", True)):
                if hedged and not can_hedge:
                    row_out[case][key] = None
                    continue
                split = _split(hedged, margin_per_pool, reserve_usd(s, base.chain) / amount)
                inputs = pool_inputs(hedged, mult, case == "cautious")
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
    jumps = []
    sp, sp24 = inp.get("sigma_pair"), inp.get("sigma_pair_24h")
    if sp and sp24 is not None and sp24 > s.vol_jump_ratio * sp:
        jumps.append(f"2つのコインの比率は直近24時間 {sp24 * 100:.1f}%／7日 {sp * 100:.1f}%")
    for sym in syms:
        a, b = (inp.get("sigma_token") or {}).get(sym), (inp.get("sigma_token_24h") or {}).get(sym)
        if a and b is not None and b > s.vol_jump_ratio * a:
            jumps.append(f"{sym} は直近24時間 {b * 100:.1f}%／7日 {a * 100:.1f}%")
    if jumps:
        op.flags.append(Flag("VOL_JUMP", LEVEL_WARN, f"値動きが急に大きくなった（1日あたりの値動き。{s.vol_jump_ratio:g}倍をこえた）: "
                             f"{'、'.join(jumps)}。控えめの見込みは大きい方で計算"))
    if trend_rec is None:
        op.flags.append(Flag("RWD_GUESS", LEVEL_WARN, f"値下がり未計算（仮の値で計算）: ボーナスのコイン（{base.bonus_token or '報酬のコイン'}）の"
                             f"値動きの記録がない。控えめの見込みは月 −{s.unknown_reward_drop_monthly_pct:g}% とみなした"))
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


def _change(now_v: float | None, vals: list[float]) -> tuple[float, float] | None:
    """24時間の中のいちばん高い値・低い値と今を比べて、大きいほうの変化（%）と比べた相手を返す。"""
    vals = [v for v in vals if v is not None and v > 0]
    if now_v is None or not vals:
        return None
    hi, lo = max(vals), min(vals)
    drop, rise = (now_v / hi - 1) * 100, (now_v / lo - 1) * 100
    return (drop, hi) if -drop >= rise else (rise, lo)


def sudden_changes(fconn: sqlite3.Connection, now: datetime, s: OpportunitySettings) -> dict[str, str]:
    """年利が急に変わった Merkl の機会（オーナー依頼 2026-10-03 17:59 JST。Hyperdrive の金庫で 35.9% → 62.5%）。

    預かり額か1日のボーナスの額が、直近 sudden_change_hours 時間の高い値・低い値から sudden_change_pct %以上動いたら印を付ける。
    年利はボーナス ÷ 預かり額なので、どちらが動いたかを書く（原因の見分け）。印はおすすめから外さない（注意だけ）。"""
    last = fconn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
    if not last:
        return {}
    since = (now - timedelta(hours=s.sudden_change_hours)).astimezone(UTC).isoformat(timespec="seconds")
    hist: dict[str, list[tuple[float | None, float | None]]] = {}
    cur: dict[str, tuple[float | None, float | None]] = {}
    for r in fconn.execute("""SELECT opportunity_id, ts, tvl, daily_rewards FROM merkl_opportunity_snaps
                              WHERE ts >= ? ORDER BY ts""", (min(since, last),)):
        key = str(r["opportunity_id"])
        if r["ts"] == last:
            cur[key] = (r["tvl"], r["daily_rewards"])
        else:
            hist.setdefault(key, []).append((r["tvl"], r["daily_rewards"]))
    out: dict[str, str] = {}
    h = f"{s.sudden_change_hours:g}時間"
    for key, (tvl, rew) in cur.items():
        past = hist.get(key)
        if not past:
            continue
        t = _change(tvl, [p[0] for p in past])
        w = _change(rew, [p[1] for p in past])
        big_t = t is not None and abs(t[0]) >= s.sudden_change_pct
        big_w = w is not None and abs(w[0]) >= s.sudden_change_pct
        if not (big_t or big_w):
            continue
        parts = []
        if big_t:
            parts.append(f"預かり額が{h}で {t[0]:+.0f}%（${t[1]:,.0f} → ${tvl:,.0f}）")
        if big_w:
            parts.append(f"1日のボーナスの額が{h}で {w[0]:+.0f}%（${w[1]:,.0f} → ${rew:,.0f}）")
        elif rew:
            parts.append(f"ボーナスの額は大きく変わっていない（1日 ${rew:,.0f}）")
        why = ("ほかの人が大きく引き出したので、同じボーナスを少ない人数で分けている。戻ってくると年利は下がる"
               if big_t and t[0] < 0 and not big_w else
               "ほかの人が大きく入れたので、ボーナスを多い人数で分けている" if big_t and t[0] > 0 and not big_w else
               "配る額が変わった")
        out[key] = f"年利が急に変わった: {'。'.join(parts)}。{why}（Merkl の15分ごとの記録）"
    return out


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
            sudden = sudden_changes(fconn, now, config.opportunities)
            for b in from_merkl(config.feeds.database_path, config, now, registered_only=True):
                op = evaluate_merkl(b, infos.get(b.key, {}), camps.get(b.key, []), data, config)
                if b.key in sudden:
                    op.flags.append(Flag("SUDDEN_CHANGE", LEVEL_WARN, sudden[b.key]))
                out.append(op)
    finally:
        if fconn is not None:
            fconn.close()
    return out


def rank(ops: list[Opportunity], amount: float, target_apr_pct: float, include_excluded: bool = False
         ) -> list[Opportunity]:
    """並べ替え: 計算できたもの → おすすめ（狙い利回り以上で会場の見分けが確か）→ 狙い利回り以上 →
    控えめの見込みの年利の高い順。外したものは include_excluded のときだけ。"""
    def key(o: Opportunity) -> tuple:
        b = o.best(amount)
        apr = b.apr_pct if b else -1e18
        return (not o.computable, o.excluded, not o.recommended(amount, target_apr_pct),
                not (b is not None and apr >= target_apr_pct), -apr)
    return sorted((o for o in ops if include_excluded or not o.excluded), key=key)


def chain_name(config: Config, cid: str | None) -> str | None:
    try:
        return load_chain(cid, config.root)["name"] if cid else None
    except ConfigError:
        return None


__all__ = ["Opportunity", "Variant", "Flag", "FeedData", "collect", "rank", "campaign_income", "budget_daily",
           "evaluate_merkl", "evaluate_own", "_days_left"]
