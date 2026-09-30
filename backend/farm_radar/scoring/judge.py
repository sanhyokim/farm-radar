"""判定（信号機）と理由文（SPEC 4章）。

- 判定に使うのは総資産あたりの純日利（net_daily_pct）。
- 重大な警告（major）があれば🔴、軽微な警告（minor）があれば最高でも🟡。
- ヘッジできないプールは最高でも🟡で、「ヘッジ不可：値動きの損をそのまま受けます」と必ず書く。
- 純日利が too_high_pct（初期値5%）を超えたら「数字が高すぎます」の軽微な警告を付ける（2026-09-29 オーナー指示）。
- 理由文は日本語3行以内。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Evaluation

EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
NO_HEDGE_TEXT = "ヘッジ不可：値動きの損をそのまま受けます"
TOO_HIGH_TEXT = "数字が高すぎます。データや計算を確認してください"
MODE_HOW = {
    "staked": "ステークしてボーナスを受け取る",
    "unstaked": "ステークせず手数料を受け取る",
    "rewards": "ボーナスを受け取る（ステーク不要。取引手数料はもらえない）",   # Alandale の CL（M6）
}


@dataclass(frozen=True)
class Warn:
    code: str                  # SPEC 2章の条件（C1〜C4）や、データの問題（DATA）
    level: str                 # "major"（重大）/ "minor"（軽微）
    message_ja: str


@dataclass(frozen=True)
class SignalParams:
    green_min_pct: float = 0.30
    yellow_min_pct: float = 0.10
    green_min_tvl_usd: float = 200_000
    reward_token_7d_major_pct: float = -30.0
    too_high_pct: float = 5.0


@dataclass(frozen=True)
class Judgement:
    signal: str
    reason_ja: str
    warnings: tuple[Warn, ...] = field(default=())

    @property
    def emoji(self) -> str:
        return EMOJI[self.signal]


def _pct(v: float) -> str:
    return f"{v:.2f}%"


def judge(
    ev: Evaluation | None,
    params: SignalParams,
    *,
    warnings: list[Warn],
    tvl_usd: float | None,
    missing: str | None = None,
    notes: list[str] | None = None,
) -> Judgement:
    notes = list(notes or [])
    warnings = list(warnings)
    if ev is None or missing:
        return Judgement("red", f"データが足りないため判定できません（{missing or '計算できませんでした'}）。",
                         tuple(warnings))
    if ev.net_daily_pct > params.too_high_pct:
        warnings.append(Warn("HIGH", "minor", f"{TOO_HIGH_TEXT}（{params.too_high_pct:g}% 超え）"))
        notes.insert(0, TOO_HIGH_TEXT + "。")
    warns = tuple(warnings)

    best, c_total = ev.best, ev.params.c_total
    income_pct = best.income / c_total * 100
    cost_pct = income_pct - ev.net_daily_pct
    how = MODE_HOW.get(best.mode, MODE_HOW["staked"])
    line1 = (f"純日利 {_pct(ev.net_daily_pct)}（総資産あたり）＝ 収入 {_pct(income_pct)} − 損とコスト {_pct(cost_pct)}。"
             f"最適レンジ ±{best.r * 100:g}%、{how}。")

    majors = [w for w in warns if w.level == "major"]
    minors = [w for w in warns if w.level == "minor"]
    caps: list[str] = []
    if majors:
        signal, line2 = "red", "重大な警告: " + " / ".join(w.message_ja for w in majors)
    elif ev.net_daily_pct < params.yellow_min_pct:
        signal = "red"
        line2 = f"純日利が {_pct(params.yellow_min_pct)} 未満のため見送り。" + _biggest_cost(ev)
    elif ev.net_daily_pct < params.green_min_pct:
        signal = "yellow"
        line2 = f"純日利が {_pct(params.green_min_pct)} に届かないため様子見。" + _biggest_cost(ev)
    else:
        signal = "green"
        if not ev.has_perp:
            caps.append("ヘッジできない")
        if [w for w in minors if w.code != "HIGH"]:
            caps.append("軽微な警告あり（" + " / ".join(w.message_ja for w in minors if w.code != "HIGH") + "）")
        if any(w.code == "HIGH" for w in minors):
            caps.append("数字が高すぎる")
        if tvl_usd is None:
            caps.append("プールの大きさが分からない")
        elif tvl_usd < params.green_min_tvl_usd:
            caps.append(f"プールが小さい（${tvl_usd:,.0f}）")
        line2 = "純日利は十分。" + ("ただし" + "、".join(caps) + "ため様子見。" if caps else "攻め候補。")
        if caps:
            signal = "yellow"

    if not ev.has_perp:
        notes.insert(1 if notes and notes[0].startswith(TOO_HIGH_TEXT) else 0, NO_HEDGE_TEXT + "。")
    line3 = "".join(notes)
    reason = "\n".join(x for x in (line1, line2, line3) if x)
    return Judgement(signal, reason, warns)


def _biggest_cost(ev: Evaluation) -> str:
    b = ev.best
    costs = {"ガンマ損失": b.gamma, "リバランス費用": b.rebalance, "ヘッジ費用": b.hedge,
             "報酬トークンの値下がり": b.haircut, "値動きの損": b.direction_risk}
    name, val = max(costs.items(), key=lambda kv: kv[1])
    if b.income <= 0:
        return "今はボーナスも手数料もほぼ入りません。"
    if val <= 0:
        return "収入が少ないのが主な理由。"
    return f"いちばん大きい損は{name}（日 {val / ev.params.c_total * 100:.2f}%）。"
