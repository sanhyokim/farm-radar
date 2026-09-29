"""仮想ポジションの計算（SPEC 3.2章）。外部とのやりとりはせず、数字だけを計算する。

金額はすべてドル、日あたり。r は片側のレンジ幅（0.01 = ±1%）。σ は日次（0.03 = 1日 3%）。
式の出どころ:
- in_range_ratio（置き直す前提）と参考値（置きっぱなし）: 2026-09-29 オーナー決定（SPEC 3.2章 2.）
- direction_risk = C_lp × 0.5 × 0.4 × (σ_d(A) + σ_d(B)): 2026-09-29 オーナー決定（SPEC 3.2章 5.）
- 両替のずれ（スリッページ）はプールの今の流動性から見積もる: 2026-09-29 オーナー指示（SPEC 3.2章 4.）
- 参考値「報酬をすぐ売る前提」: 報酬トークンの値下がりを、受け取ってから売るまでの時間（初期値1時間）の分だけ引く。
  判定には使わない（2026-09-29 オーナー指示。SPEC 3.2章 6.）
- 報酬はレンジ内のステーク流動性に比例、ステークすると手数料は0、ステークしないと手数料の一部を取られる:
  up. で確認済み（venues/up-robinhood.yaml の mechanics）
- ステークがなく、LPが手数料と報酬の両方を受け取る会場（fees_with_rewards。例: Alandale）では、
  「ボーナスか手数料か」を選ばず、両方を足す（2026-09-30 M6 で追加。venues/alandale-robinhood.yaml の mechanics）。
  式はそれぞれ up. と同じ（取り分 × レンジ内の時間の割合）。報酬トークンの値下がりは報酬の分だけに引く。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MINUTES_PER_DAY = 1440


@dataclass(frozen=True)
class ModelParams:
    c_total: float = 1000.0               # 総資産（SPEC 3.2章）
    lp_share: float = 0.55                # そのうちLPに置く割合
    ranges: tuple[float, ...] = (0.005, 0.01, 0.02, 0.03, 0.05, 0.10, 0.15)
    rebalance_wait_minutes: float = 15.0  # レンジを外れてから置き直すまでの待ち時間
    gas_usd_per_tx: float = 0.0           # 1回の取引のガス代（ドル）
    swap_ratio: float = 0.5               # 置き直すときに両替する割合
    hedge_taker_fee: float = 0.0          # perp の取引手数料（割合）
    count_funding_income: bool = False    # 資金調達を「受け取る」側のとき、それを収入に数えるか
    reward_sell_hours: float = 1.0        # 参考値「すぐ売る前提」: 報酬を受け取ってから売るまでの時間

    @property
    def c_lp(self) -> float:
        return self.c_total * self.lp_share


@dataclass(frozen=True)
class TokenSide:
    usd: float                      # ドル価格
    decimals: int
    sigma_usd: float                # ドル価格の日次ボラ（ステーブルコインは0）
    stable: bool = False
    hedgeable: bool = False         # 確認済みの perp があり、資金調達率も取れている
    funding_cost_daily: float = 0.0 # ショートを持ったときの1日の資金調達の支払い（割合。マイナスは受け取り）
    taker_fee: float | None = None  # 選んだヘッジ先の取引手数料（割合）。None なら ModelParams.hedge_taker_fee


@dataclass(frozen=True)
class PoolInputs:
    price: float                    # token1 / token0（小数点調整済み）
    token0: TokenSide
    token1: TokenSide
    fee: float                      # プールの手数料（割合。0.003 = 0.3%）
    unstaked_fee: float             # ステークしないLPから取られる手数料の割合（0.10 = 10%）
    liquidity_total: int            # 今の価格のところの流動性（プール全体、ステーク分を含む）
    liquidity_staked: int           # そのうちゲージにステークされている分（報酬の取り分の分母）
    reward_usd_day: float           # ゲージが今出している報酬のドル換算（1日あたり）
    fees_usd_day: float | None      # プール全体の1日の手数料（出来高 × 手数料率）。分からなければ None
    sigma_pair: float               # 2つのトークンの比率の日次ボラ
    reward_trend_daily: float | None = None   # 報酬トークンの7日の変化を1日あたりに直したもの（-0.02 = 1日 -2%）
    slippage: float = 0.0           # 両替のときにプールの手数料に加えてかかる価格のずれ（割合。swap_price_impact で見積もる）
    fees_with_rewards: bool = False # ステークがなく、手数料と報酬の両方を受け取る会場（Alandale）。
                                    # このとき unstaked_fee は「LPの手数料から会場が取る割合」、
                                    # liquidity_staked は「報酬を分け合うレンジ内の流動性」の意味になる


@dataclass(frozen=True)
class RangeResult:
    r: float
    liquidity_mine: float
    rebalances_per_day: float
    in_range_ratio: float
    in_range_ratio_hold: float      # 参考値（置きっぱなし）。判定には使わない
    mode: str                       # "staked"（ステークしてボーナス）/ "unstaked"（ステークせず手数料）/ "both"（両方）
    income_staked: float
    income_unstaked: float | None
    income: float
    gamma: float
    rebalance: float
    hedge: float
    haircut: float
    direction_risk: float
    net: float
    # 参考値（判定には使わない）: 報酬をすぐ売る前提。値下がりは売るまでの時間の分だけ引く
    mode_sell_now: str = "staked"
    haircut_sell_now: float = 0.0
    net_sell_now: float = 0.0


@dataclass(frozen=True)
class Evaluation:
    params: ModelParams
    rows: tuple[RangeResult, ...]
    has_perp: bool                  # 値動きのあるトークンがすべてヘッジできる
    best: RangeResult = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "best", max(self.rows, key=lambda x: x.net))

    @property
    def net_daily_pct(self) -> float:
        """総資産あたりの純日利（%）。4章の判定に使う。"""
        return self.best.net / self.params.c_total * 100

    @property
    def net_daily_pct_lp(self) -> float:
        """建玉（LPに置いた額）あたりの純日利（%）。表示用。"""
        return self.best.net / self.params.c_lp * 100

    @property
    def best_sell_now(self) -> RangeResult:
        """参考値「報酬をすぐ売る前提」でいちばん良いレンジ。"""
        return max(self.rows, key=lambda x: x.net_sell_now)

    @property
    def net_daily_pct_sell_now(self) -> float:
        """参考値: 報酬をすぐ売る前提の、総資産あたりの純日利（%）。判定には使わない。"""
        return self.best_sell_now.net_sell_now / self.params.c_total * 100


def liquidity_for_usd(c_lp: float, price: float, r: float, t0: TokenSide, t1: TokenSide) -> float:
    """v3 の式で、c_lp ドルを [P(1−r), P(1+r)] に置いたときの流動性 L（プールの liquidity() と同じ単位）。"""
    p_raw = price * 10 ** (t1.decimals - t0.decimals)
    sp, sa, sb = math.sqrt(p_raw), math.sqrt(p_raw * (1 - r)), math.sqrt(p_raw * (1 + r))
    x_raw = 1 / sp - 1 / sb          # L=1 あたりの token0（最小単位）
    y_raw = sp - sa                  # L=1 あたりの token1（最小単位）
    usd_per_l = x_raw / 10 ** t0.decimals * t0.usd + y_raw / 10 ** t1.decimals * t1.usd
    return c_lp / usd_per_l if usd_per_l > 0 else 0.0


def swap_price_impact(amount_usd: float, liquidity: int, price: float, t0: TokenSide, t1: TokenSide) -> float | None:
    """amount_usd ドル分を両替したときに、プールの価格がどれだけ動くか（割合。0.01 = 1%）。

    今の価格のところの流動性 L が、動く範囲でずっと同じだとして計算する（v3 の式）。
    - token1 を入れる: √P が amount1 / L だけ上がる
    - token0 を入れる: 1/√P が amount0 / L だけ上がる
    向きで結果が少し違うので、大きい方を使う（安全側）。1（100%）を上限にする。
    実際は途中で流動性が変わる（ティックをまたぐ）ことがあるので、目安の値。
    """
    if liquidity <= 0 or amount_usd <= 0 or t0.usd <= 0 or t1.usd <= 0 or price <= 0:
        return None
    sp = math.sqrt(price * 10 ** (t1.decimals - t0.decimals))
    y = amount_usd / t1.usd * 10 ** t1.decimals
    x = amount_usd / t0.usd * 10 ** t0.decimals
    up = (1 + y / liquidity / sp) ** 2 - 1
    down = 1 - 1 / (1 + x * sp / liquidity) ** 2
    return min(1.0, max(up, down))


def rebalances_per_day(sigma_pair: float, r: float) -> float:
    """1日のリバランス回数の見込み ≈ σ² / r²（SPEC 3.2章 4.）。"""
    return (sigma_pair / r) ** 2


def in_range_ratio(sigma_pair: float, r: float, wait_minutes: float) -> float:
    """置き直す前提のレンジ内の時間の割合 = 1 − 回数 × 待ち時間 ÷ 24時間（0未満は0）。"""
    return max(0.0, 1.0 - rebalances_per_day(sigma_pair, r) * wait_minutes / MINUTES_PER_DAY)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def in_range_ratio_hold(sigma_pair: float, r: float, steps: int = 288) -> float:
    """参考値: 置きっぱなしのとき、1日の各時点でレンジ内にいる確率の平均。

    価格の対数が1日で σ のランダムウォークをするとして、時刻 t（日）でレンジ内にいる確率は
    Φ(ln(1+r)/(σ√t)) − Φ(ln(1−r)/(σ√t))。これを1日（5分刻み）で平均する。
    """
    if sigma_pair <= 0:
        return 1.0
    hi, lo = math.log(1 + r), math.log(1 - r)
    total = 0.0
    for i in range(steps):
        s = sigma_pair * math.sqrt((i + 0.5) / steps)
        total += _norm_cdf(hi / s) - _norm_cdf(lo / s)
    return total / steps


def direction_risk(c_lp: float, sigmas_unhedged: list[float]) -> float:
    """ヘッジできないトークンの値下がりの損の見込み = C_lp × 0.5 × 0.4 × Σσ（SPEC 3.2章 5.）。"""
    return c_lp * 0.5 * 0.4 * sum(sigmas_unhedged)


def sell_now_drop(trend_daily: float | None, hours: float) -> float:
    """報酬を受け取ってから hours 時間で売るときの、報酬トークンの値下がりの割合（0以上）。

    7日の変化を1日あたりに直したもの（trend_daily）を、さらに1時間あたりに直して hours 時間分にする。
    値上がりしているときは0（安全側。値上がりは収入に数えない）。
    """
    if trend_daily is None or trend_daily >= 0 or hours <= 0:
        return 0.0
    return 1 - (1 + trend_daily) ** (hours / 24)


def gamma(c_lp: float, sigma_pair: float, r: float) -> float:
    """ガンマ損失 = C_lp × σ² / (4r)（近似。SPEC 3.2章 3.）。"""
    return c_lp * sigma_pair ** 2 / (4 * r)


def evaluate(inp: PoolInputs, params: ModelParams) -> Evaluation:
    c_lp = params.c_lp
    volatile = [t for t in (inp.token0, inp.token1) if not t.stable]
    hedged = [t for t in volatile if t.hedgeable]
    unhedged = [t for t in volatile if not t.hedgeable]
    notional = c_lp * 0.5                                   # 1つのトークンあたりのヘッジ額（LPの約半分ずつ）
    dir_risk = direction_risk(c_lp, [t.sigma_usd for t in unhedged])

    rows = []
    for r in params.ranges:
        l_mine = liquidity_for_usd(c_lp, inp.price, r, inp.token0, inp.token1)
        n = rebalances_per_day(inp.sigma_pair, r)
        irr = in_range_ratio(inp.sigma_pair, r, params.rebalance_wait_minutes)
        irr_hold = in_range_ratio_hold(inp.sigma_pair, r)

        share_staked = l_mine / (inp.liquidity_staked + l_mine) if l_mine > 0 else 0.0
        income_staked = inp.reward_usd_day * share_staked * irr
        income_unstaked = None
        if inp.fees_usd_day is not None:
            share_all = l_mine / (inp.liquidity_total + l_mine) if l_mine > 0 else 0.0
            income_unstaked = inp.fees_usd_day * (1 - inp.unstaked_fee) * share_all * irr
        trend = inp.reward_trend_daily
        if inp.fees_with_rewards:
            # ステークがない会場: 報酬と手数料の両方を受け取る。値下がりは報酬の分だけに引く
            mode, income = "both", income_staked + (income_unstaked or 0.0)
            haircut = income_staked * -trend if trend is not None and trend < 0 else 0.0
        elif income_unstaked is not None and income_unstaked > income_staked:
            mode, income, haircut = "unstaked", income_unstaked, 0.0
        else:
            mode, income = "staked", income_staked
            haircut = income_staked * -trend if trend is not None and trend < 0 else 0.0

        g = gamma(c_lp, inp.sigma_pair, r)
        reb = n * (params.gas_usd_per_tx * 2 + c_lp * params.swap_ratio * (inp.fee + inp.slippage))
        hedge = 0.0
        for t in hedged:
            cost = t.funding_cost_daily if params.count_funding_income else max(0.0, t.funding_cost_daily)
            taker = t.taker_fee if t.taker_fee is not None else params.hedge_taker_fee
            hedge += notional * cost + n * notional * taker
        net = income - g - reb - hedge - haircut - dir_risk
        # 参考値: 報酬をすぐ売る前提。値下がりは売るまでの時間の分だけ引き、ステークするかどうかも選び直す
        haircut_sell = income_staked * sell_now_drop(inp.reward_trend_daily, params.reward_sell_hours)
        if inp.fees_with_rewards:
            mode_sell, income_sell = "both", income
        elif income_unstaked is not None and income_unstaked > income_staked - haircut_sell:
            mode_sell, income_sell, haircut_sell = "unstaked", income_unstaked, 0.0
        else:
            mode_sell, income_sell = "staked", income_staked
        net_sell = income_sell - g - reb - hedge - haircut_sell - dir_risk
        rows.append(RangeResult(r, l_mine, n, irr, irr_hold, mode, income_staked, income_unstaked, income,
                                g, reb, hedge, haircut, dir_risk, net, mode_sell, haircut_sell, net_sell))
    return Evaluation(params, tuple(rows), has_perp=not unhedged)
