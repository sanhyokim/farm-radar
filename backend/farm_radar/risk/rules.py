"""危険判定のルール（SPEC 12.2章・付録A 3章。M5b）。

練習の建玉にも、将来の本物の建玉にも同じルールを使えるように、データベースを読まない「判定だけ」の部品にしている。
入力（今の価格・レンジ・スコア・報酬トークンの値動きなど）を受け取り、見つかった危険を強い順に返す。
何をするか（置き直す・閉じる・全部閉じる）は呼ぶ側（execution/risk_job.py）が決める。

レベル（強い順）:
- emergency（緊急離脱）: 全部閉じて、新しく始めるのを止める
- exit（離脱）: その建玉を閉じる
- rebalance（置き直し）: 新しいレンジに移す。置き直し先の純日利が低ければ離脱
- caution（注意）: 記録するだけ

まだ入れていないルール（オーナーに確認中。PROGRESS.md「M5b の実装メモ」）:
- ミームペアの dumping 判定（決め方が SPEC に書かれていない）
- コントラクトの pause・owner 変更・upgrade のイベント（イベントをまだ集めていない）
- USDG が $0.98 未満（USDG のドル価格を、プール以外のどこから取るか未定）
- ヘッジの証拠金維持率・資金調達率の急騰（基準の数字が SPEC に書かれていない）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RiskSettings

LEVELS = ("caution", "rebalance", "exit", "emergency")
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
    liquidity_now: float | None = None           # プールの流動性（今）
    liquidity_1h_ago: float | None = None        # プールの流動性（1時間前）


@dataclass(frozen=True)
class PortfolioInput:
    """建玉全体の状態。"""
    reward_symbol: str = "報酬トークン"
    reward_change_24h: float | None = None       # 報酬トークンの24時間の変化（−0.2 = −20%）
    today_net_usd: float | None = None           # 今日（日本時間）の練習の純損益の合計（ドル）
    open_capital_usd: float = 0.0                # 持っている建玉の合計額（ドル）


def check_position(p: PositionInput, pf: PortfolioInput, s: RiskSettings) -> list[Finding]:
    """1つの建玉の危険を調べる。強い順に並べて返す（何もなければ空）。"""
    out: list[Finding] = []

    # 緊急離脱: プールの流動性が1時間で大きく減った（ラグプルの兆候）
    if p.liquidity_now is not None and p.liquidity_1h_ago and p.liquidity_1h_ago > 0:
        drop = (1 - p.liquidity_now / p.liquidity_1h_ago) * 100
        if drop >= s.emergency_liquidity_drop_1h_pct:
            out.append(Finding("emergency", "liquidity_drop",
                               f"{p.pair} のプールのお金（流動性）が1時間で{drop:.0f}%減りました"
                               f"（基準は{s.emergency_liquidity_drop_1h_pct:g}%）。お金を抜かれる前ぶれの可能性があります。",
                               {"drop_pct": drop}))

    # 離脱: 報酬トークンが24時間で大きく下がった
    if pf.reward_change_24h is not None and pf.reward_change_24h * 100 <= s.exit_reward_token_24h_pct:
        out.append(Finding("exit", "reward_token_drop",
                           f"報酬の {pf.reward_symbol} が24時間で{pf.reward_change_24h * 100:.1f}%下がりました"
                           f"（基準は{s.exit_reward_token_24h_pct:g}%）。",
                           {"change_24h_pct": pf.reward_change_24h * 100}))

    # 離脱: プールの判定が🔴になった（🔴と分かって始めた練習には当てはめない。2026-09-29 オーナー決定）
    if p.signal == "red" and not p.started_red:
        out.append(Finding("exit", "signal_red", f"{p.pair} の判定が🔴（見送り）になりました。"))

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
    return sorted(out, key=lambda f: -f.rank)


def check_portfolio(pf: PortfolioInput, s: RiskSettings) -> list[Finding]:
    """全体の危険（今日の損の大きさ）。"""
    if pf.today_net_usd is None or pf.open_capital_usd <= 0:
        return []
    loss_pct = -pf.today_net_usd / pf.open_capital_usd * 100
    if loss_pct >= s.emergency_daily_loss_pct:
        return [Finding("emergency", "daily_loss",
                        f"今日の練習の損が ${-pf.today_net_usd:,.2f}（持っている額 ${pf.open_capital_usd:,.0f} の{loss_pct:.1f}%）"
                        f"になりました（基準は{s.emergency_daily_loss_pct:g}%）。",
                        {"loss_pct": loss_pct, "today_net_usd": pf.today_net_usd})]
    return []
