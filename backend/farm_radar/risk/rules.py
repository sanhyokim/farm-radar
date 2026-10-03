"""危険判定のルール（SPEC 12.2章・付録A 3章。M5b）。

練習の建玉にも、将来の本物の建玉にも同じルールを使えるように、データベースを読まない「判定だけ」の部品にしている。
入力（今の価格・レンジ・スコア・報酬トークンの値動きなど）を受け取り、見つかった危険を強い順に返す。
何をするか（置き直す・閉じる・全部閉じる）は呼ぶ側（execution/risk_job.py）が決める。

レベル（強い順）:
- emergency（緊急離脱）: 1つのプールだけの危険（check_position）は、その建玉だけ閉じる。
  会場全体の危険（check_portfolio）は、全部閉じて新しく始めるのを止める（2026-10-01 オーナー決定 C）
- exit（離脱）: その建玉を閉じる
- rebalance（置き直し）: 新しいレンジに移す。置き直し先の純日利が低ければ離脱
- caution（注意）: 記録するだけ

2026-09-29 オーナー決定で加えたルール（SPEC 12.2章）:
- 投げ売り（離脱）、USDG の外部価格（緊急離脱）、ヘッジの費用（注意）、会場プログラムの変化（緊急離脱。
  読み取りは execution/contract_watch.py、ここでは変化の一覧を受け取るだけ）
- ヘッジの証拠金維持率・資金調達率の急騰は、Phase 3 で実際の perp を使うときに決める

2026-10-01 オーナー決定 A（9/30 16:45 JST の USDG/NVDA の読み違いから）:
- 緊急離脱の「お金が抜けた」判定は、プールのお金（プールが持っているコインの量を、今と1時間前のどちらも今の値段で数えたもの）で比べる。
  値段が動いただけでは変わらず、引き出されたときだけ減る
- 今の値段のところの流動性（pool.liquidity()）は、値段が大きな建玉の範囲の端をまたぐだけで半分以下になるので、緊急離脱には使わない。
  ボーナスの取り分の計算に関わるので、急に減ったら「注意」（記録と表示だけ）

2026-10-04 オーナー決定 A（段階1を2段にする。さかのぼりで、31回のうち27回が5万ドル未満の小さいプールだったため）:
- プールのお金が1時間で大きく減った → risk.pool_funds_drop_action が notify なら「知らせる」だけ（level は caution、
  知らせの強さは major）。そのプールに新しく入るのを止めるのは paper.py、出るのは手で。exit なら前と同じく緊急離脱
- 自分の建玉の値打ちが1時間で大きく減った → 緊急離脱（その建玉だけ閉じる）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RiskSettings

LEVELS = ("caution", "rebalance", "exit", "emergency")

# 早く出る4段階（N4b。SPEC 13.2「早く出る4段階」と 13.1 の追加の決定 9）。どの決まりがどの段階かを記録と画面に出す
# 段階1 すぐ逃げる（ガス代が高くても）/ 段階2 利益が消えた / 段階3 予定どおり / 段階4 もっと良い場所へ
STAGE = {
    "pool_funds_drop": 1, "own_value_drop": 1, "contract_change": 1, "usdg_depeg": 1, "hedge_liquidation": 1,
    "reward_token_drop": 2, "dump": 2, "signal_red": 2, "out_of_range_low_score": 2, "below_target": 2,
    "bonus_drop": 3,
    "better_place": 4,
}
STAGE_JA = {1: "段階1 すぐ逃げる", 2: "段階2 利益が消えた", 3: "段階3 予定どおり", 4: "段階4 もっと良い場所へ"}
LEVEL_JA = {"caution": "注意", "rebalance": "置き直し", "exit": "離脱", "emergency": "緊急離脱", "info": "お知らせ"}


@dataclass(frozen=True)
class Finding:
    level: str              # caution / rebalance / exit / emergency
    kind: str
    message_ja: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def rank(self) -> int:
        return LEVELS.index(self.level)


@dataclass(frozen=True)
class PositionInput:
    """1つの建玉の、今の状態（呼ぶ側が記録から集める）。"""
    pair: str
    lower: float
    upper: float
    price: float
    started_red: bool
    minutes_out_of_range: float | None = None    # レンジの外に出てからの分数（中にいれば None）
    signal: str | None = None                    # 最新のスコアの判定（green / yellow / red）
    rebalance_net_pct: float | None = None       # 置き直すとしたら、その先の純日利（総資産あたり%。最新のスコア）
    predicted_income_day: float | None = None    # 予測の1日の収入（ドル）
    actual_income_day: float | None = None       # 実績の1日あたりの収入（ドル）
    income_hours: float = 0.0                    # 実績の収入を数えた時間（時間）
    liquidity_now: float | None = None           # 今の値段のところの流動性（レンジ内。今）。注意の表示だけに使う
    liquidity_1h_ago: float | None = None        # 同じ（1時間前）
    funds_now: float | None = None               # プールのお金（今。コインの量 × 今の値段。単位は funds_unit）
    funds_1h_ago: float | None = None            # プールのお金（1時間前のコインの量 × 今の値段）
    funds_unit: str = ""                         # プールのお金の単位（token1 の記号）
    value_now: float | None = None               # 自分の建玉の値打ち（今。ドル。position_pnl の value_usd）
    value_1h_ago: float | None = None            # 同じ（1時間前）
    # 値動きする側のトークンの変化（記号, 1時間の変化, 24時間の変化。−0.15 = −15%。分からなければ None）
    token_moves: tuple[tuple[str, float | None, float | None], ...] = ()
    hedge_cost_day: float | None = None          # ヘッジの1日あたりの費用（資金調達料。ドル）
    # 段階2（N4b）: 残る利回り（最新のスコアの純日利 × 365。年%）が狙い利回りを下回った回数（続けて）
    net_apr_pct: float | None = None
    target_apr_pct: float | None = None
    below_target_count: int = 0
    below_target_needed: int = 3
    started_below_target: bool = False           # 狙い利回りより低いと分かって始めた練習（この決まりは当てはめない）


@dataclass(frozen=True)
class PortfolioInput:
    """建玉全体の状態。"""
    reward_symbol: str = "報酬トークン"
    reward_change_24h: float | None = None       # 報酬トークンの24時間の変化（−0.2 = −20%）
    today_net_usd: float | None = None           # 今日（日本時間）の練習の純損益の合計（ドル）
    open_capital_usd: float = 0.0                # 総資産（持っている建玉の投入額の合計。ドル）
    usdg_prices: tuple[float, ...] = ()          # USDG の外部の価格（新しい順。直近の数回）
    contract_changes: tuple[str, ...] = ()       # 会場プログラムで前回から変わったこと（日本語の説明）


def check_position(p: PositionInput, pf: PortfolioInput, s: RiskSettings) -> list[Finding]:
    """1つの建玉の危険を調べる。強い順に並べて返す（何もなければ空）。"""
    out: list[Finding] = []

    # 段階1: プールのお金が1時間で大きく減った（ラグプルの兆候。2026-10-01 オーナー決定 A+C）。
    # 2026-10-04 オーナー決定 A: ほかの人の引き出しでも出るので、ふつうは知らせて新しく入るのを止めるだけ（出るのは手で）
    funds_change = None
    if p.funds_now is not None and p.funds_1h_ago and p.funds_1h_ago > 0:
        funds_change = (p.funds_now / p.funds_1h_ago - 1) * 100
        if -funds_change >= s.emergency_pool_funds_drop_1h_pct:
            auto = s.pool_funds_drop_action == "exit"
            what = ("" if auto else
                    f"このプールに新しく入るのを{s.pool_funds_block_hours:g}時間止めます。建玉は自動では閉じません。"
                    "様子を見て、出るときは「出る」ボタンで出てください。")
            out.append(Finding("emergency" if auto else "caution", "pool_funds_drop",
                               f"{p.pair} のプールのお金（プールが持っているコインの量。1時間前も今の値段で数えて比べます）が"
                               f"1時間で{-funds_change:.0f}%減りました（基準は{s.emergency_pool_funds_drop_1h_pct:g}%）。"
                               "お金が引き出された可能性があります（ほかの人が大きく引き出しただけのこともあります）。" + what,
                               {"drop_pct": -funds_change, "funds_now": p.funds_now, "funds_1h_ago": p.funds_1h_ago,
                                "unit": p.funds_unit, "action": s.pool_funds_drop_action}))

    # 緊急離脱（その建玉だけ閉じる）: 自分の建玉の値打ちが1時間で大きく減った（2026-10-04 オーナー決定 A）
    if p.value_now is not None and p.value_1h_ago and p.value_1h_ago > 0:
        vch = (p.value_now / p.value_1h_ago - 1) * 100
        if -vch >= s.emergency_own_value_drop_1h_pct:
            out.append(Finding("emergency", "own_value_drop",
                               f"{p.pair} の自分の建玉の値打ちが1時間で{-vch:.1f}%減りました"
                               f"（${p.value_1h_ago:,.2f} → ${p.value_now:,.2f}。基準は{s.emergency_own_value_drop_1h_pct:g}%）。",
                               {"drop_pct": -vch, "value_now": p.value_now, "value_1h_ago": p.value_1h_ago}))

    # 注意（記録と表示だけ）: 今の値段のところの流動性が1時間で大きく減った（2026-10-01 オーナー決定。緊急離脱には使わない）
    if p.liquidity_now is not None and p.liquidity_1h_ago and p.liquidity_1h_ago > 0:
        drop = (1 - p.liquidity_now / p.liquidity_1h_ago) * 100
        if drop >= s.caution_active_liquidity_drop_1h_pct:
            funds_txt = ("プールのお金は読めていません" if funds_change is None
                         else f"プールのお金は{funds_change:+.1f}%")
            out.append(Finding("caution", "active_liquidity_drop",
                               f"{p.pair} の今の値段のところに置かれたお金（レンジ内の流動性）が1時間で{drop:.0f}%減りました"
                               f"（基準は{s.caution_active_liquidity_drop_1h_pct:g}%。{funds_txt}）。"
                               "値段が大きな建玉の範囲の端をまたいだときによく起きます。ボーナスの取り分の見込みが変わることがあります"
                               "（表示だけで、建玉は動かしません）。",
                               {"drop_pct": drop, "funds_change_pct": funds_change}))

    # 離脱: 報酬トークンが24時間で大きく下がった
    if pf.reward_change_24h is not None and pf.reward_change_24h * 100 <= s.exit_reward_token_24h_pct:
        out.append(Finding("exit", "reward_token_drop",
                           f"報酬の {pf.reward_symbol} が24時間で{pf.reward_change_24h * 100:.1f}%下がりました"
                           f"（基準は{s.exit_reward_token_24h_pct:g}%）。",
                           {"change_24h_pct": pf.reward_change_24h * 100}))

    # 離脱: 値動きする側のトークンの投げ売り（2026-09-29 オーナー決定）
    for sym, ch1, ch24 in p.token_moves:
        hit = []
        if ch1 is not None and ch1 * 100 <= s.exit_dump_1h_pct:
            hit.append(f"1時間で{ch1 * 100:.1f}%（基準は{s.exit_dump_1h_pct:g}%）")
        if ch24 is not None and ch24 * 100 <= s.exit_dump_24h_pct:
            hit.append(f"24時間で{ch24 * 100:.1f}%（基準は{s.exit_dump_24h_pct:g}%）")
        if hit:
            out.append(Finding("exit", "dump", f"{sym} がプールの値段で大きく下がりました（{'、'.join(hit)}）。投げ売りの可能性があります。",
                               {"symbol": sym, "change_1h_pct": None if ch1 is None else ch1 * 100,
                                "change_24h_pct": None if ch24 is None else ch24 * 100}))

    # 離脱: プールの判定が🔴になった（🔴と分かって始めた練習には当てはめない。2026-09-29 オーナー決定）
    if p.signal == "red" and not p.started_red:
        out.append(Finding("exit", "signal_red", f"{p.pair} の判定が🔴（見送り）になりました。"))

    # 離脱（段階2。N4b）: 残る利回りが狙い利回りを続けて下回った（狙いより低いと分かって始めた練習には当てはめない）
    if (p.net_apr_pct is not None and p.target_apr_pct is not None and not p.started_below_target
            and p.below_target_count >= p.below_target_needed):
        out.append(Finding("exit", "below_target",
                           f"{p.pair} の残る利回り（年{p.net_apr_pct:.1f}%）が、狙い利回り（年{p.target_apr_pct:g}%）を"
                           f"{p.below_target_count}回続けて下回りました（基準は{p.below_target_needed}回）。",
                           {"net_apr_pct": p.net_apr_pct, "target_apr_pct": p.target_apr_pct,
                            "times": p.below_target_count}))

    # 置き直し: レンジの外に一定時間いた
    if p.minutes_out_of_range is not None and p.minutes_out_of_range >= s.rebalance_after_minutes:
        side = "上" if p.price > p.upper else "下"
        low_score = p.rebalance_net_pct is None or p.rebalance_net_pct <= s.rebalance_min_net_pct
        if low_score:
            net_txt = "計算できない" if p.rebalance_net_pct is None else f"{p.rebalance_net_pct:+.2f}%"
            out.append(Finding("exit", "out_of_range_low_score",
                               f"{p.pair} がレンジの{side}に{p.minutes_out_of_range:.0f}分出ています。"
                               f"置き直しても純日利が{net_txt}で、基準（{s.rebalance_min_net_pct:+.2f}%）以下なので、置き直さずに閉じます。",
                               {"minutes_out": p.minutes_out_of_range, "rebalance_net_pct": p.rebalance_net_pct}))
        else:
            out.append(Finding("rebalance", "out_of_range",
                               f"{p.pair} がレンジの{side}に{p.minutes_out_of_range:.0f}分出ています"
                               f"（基準は{s.rebalance_after_minutes:g}分）。今の価格を中心に置き直します。",
                               {"minutes_out": p.minutes_out_of_range, "rebalance_net_pct": p.rebalance_net_pct}))

    # 注意: レンジの端に近い（レンジの中にいるときだけ）
    if p.lower <= p.price <= p.upper and p.price > 0:
        to_lower = (p.price / p.lower - 1) * 100
        to_upper = (p.upper / p.price - 1) * 100
        edge = min(to_lower, to_upper)
        if edge < s.caution_edge_pct:
            side = "下" if to_lower < to_upper else "上"
            out.append(Finding("caution", "edge_near",
                               f"{p.pair} がレンジの{side}の端まで{edge:.2f}%です（基準は{s.caution_edge_pct:g}%未満）。",
                               {"edge_pct": edge, "side": side}))

    # 注意: 報酬の実績が予測より大きく少ない
    if (p.predicted_income_day and p.predicted_income_day > 0 and p.actual_income_day is not None
            and p.income_hours >= s.caution_min_hours):
        short = (1 - p.actual_income_day / p.predicted_income_day) * 100
        if short >= s.caution_reward_shortfall_pct:
            out.append(Finding("caution", "reward_shortfall",
                               f"{p.pair} の報酬の実績（1日 ${p.actual_income_day:,.2f} 相当）が、予測（1日 "
                               f"${p.predicted_income_day:,.2f}）より{short:.0f}%少ないです（基準は{s.caution_reward_shortfall_pct:g}%）。",
                               {"shortfall_pct": short, "hours": p.income_hours}))
    # 注意: ヘッジの費用が報酬に比べて大きい（2026-09-29 オーナー決定。記録だけ）
    if (p.hedge_cost_day is not None and p.hedge_cost_day > 0 and p.actual_income_day
            and p.actual_income_day > 0 and p.income_hours >= s.caution_min_hours):
        ratio = p.hedge_cost_day / p.actual_income_day * 100
        if ratio > s.caution_hedge_cost_pct:
            out.append(Finding("caution", "hedge_cost",
                               f"{p.pair} のヘッジの費用（1日 ${p.hedge_cost_day:,.2f} 相当）が、報酬（1日 "
                               f"${p.actual_income_day:,.2f}）の{ratio:.0f}%です（基準は{s.caution_hedge_cost_pct:g}%超）。",
                               {"hedge_cost_day": p.hedge_cost_day, "ratio_pct": ratio}))
    return sorted(out, key=lambda f: -f.rank)


def check_portfolio(pf: PortfolioInput, s: RiskSettings) -> list[Finding]:
    """全体の危険（会場プログラムの変化・USDG の値段・今日の損の大きさ）。強い順に返す（どれも緊急離脱）。"""
    out: list[Finding] = []
    if pf.contract_changes:
        out.append(Finding("emergency", "contract_change",
                           "会場のプログラムに変化がありました: " + " / ".join(pf.contract_changes),
                           {"changes": list(pf.contract_changes)}))
    n = s.emergency_usdg_times
    recent = pf.usdg_prices[:n]
    if n > 0 and len(recent) >= n and all(x < s.emergency_usdg_below for x in recent):
        out.append(Finding("emergency", "usdg_depeg",
                           f"USDG の外部の価格が{n}回続けて ${s.emergency_usdg_below:g} 未満です"
                           f"（直近 ${recent[0]:.4f}）。ドルとの連動が崩れた可能性があります。",
                           {"prices": list(recent)}))
    if pf.today_net_usd is not None and pf.open_capital_usd > 0:
        loss_pct = -pf.today_net_usd / pf.open_capital_usd * 100
        if loss_pct >= s.emergency_daily_loss_pct:
            out.append(Finding("emergency", "daily_loss",
                               f"今日の練習の損が ${-pf.today_net_usd:,.2f}（総資産 ${pf.open_capital_usd:,.0f} の{loss_pct:.1f}%）"
                               f"になりました（基準は{s.emergency_daily_loss_pct:g}%）。",
                               {"loss_pct": loss_pct, "today_net_usd": pf.today_net_usd}))
    return out
