"""安全度（仮の3段階。N2c。SPEC 13.3: N4 の危なさの点数ができるまで、今ある印から出す。画面には「仮」と書く）。

3段階: high（高い）・mid（ふつう）・low（低い）。決め方は今ある印だけ（新しい数字は作らない）:
- 会場: 自分で読む会場は会場の登録（未確認の契約・警告 C4・監査の有無）、Merkl の会場は Merkl が載せている
  会場の情報（事件の数・監査の数・載ってからの日数・会場全体の預かり額）。Merkl の会場の情報は DefiLlama のものを
  写していて、名前の似た別の会場のものが混ざることがある（例: Hyperdrive）。仮の目安として使う。
- 入れる先: 会場の安全度から始めて、印で下げる。外す印 → 低い。注意の印・保険で守れない値動き・残りの日数が短い・
  始まったばかり・入れる額が上限より多い → ふつうまで。
区切りの数字（90日・7日）は仮。N4 で危なさの点数に置きかえる。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

LEVELS = ("low", "mid", "high")
LABEL = {"high": "高い", "mid": "ふつう", "low": "低い"}
NEW_VENUE_DAYS = 90          # 載ってからこの日数より短い会場は「ふつう」まで（仮）
SHORT_DAYS_LEFT = 7.0        # 配る期間の残りがこの日数より短いと「ふつう」まで（仮）
SMALL_VENUE_TVL = 1_000_000  # 会場全体の預かり額がこれより小さいと「ふつう」まで（仮）


def _view(level: str, reasons: list[str]) -> dict[str, Any]:
    return {"level": level, "label": LABEL[level], "provisional": True, "reasons": reasons}


def _cap(level: str, at_most: str) -> str:
    return LEVELS[min(LEVELS.index(level), LEVELS.index(at_most))]


def venue_from_merkl(trust: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Merkl の会場（Merkl が載せている会場の情報から）。"""
    if not trust:
        return _view("mid", ["会場の情報がない（Merkl に会場の記録が載っていない）"])
    hacks, audits, listed = trust.get("hacks"), trust.get("audits"), trust.get("listed_at")
    if hacks:
        return _view("low", [f"過去に事件の記録がある（{hacks}件）"])
    level, reasons = "high", []
    if not audits:
        level, reasons = "mid", reasons + ["監査の記録がない"]
    days = (now.timestamp() - listed) / 86400 if listed else None
    if days is None:
        level, reasons = "mid", reasons + ["いつから続いている会場か分からない"]
    elif days < NEW_VENUE_DAYS:
        level, reasons = "mid", reasons + [f"載ってから {days:.0f} 日（{NEW_VENUE_DAYS}日未満）"]
    tvl = trust.get("tvl")
    if tvl is not None and tvl < SMALL_VENUE_TVL:
        # 例: Hyperdrive（2026-10-02）は Merkl の会場の情報の預かり額が $79K。会場の情報が別の会場と混ざっていることもある
        level, reasons = "mid", reasons + [f"会場全体の預かり額が小さい（${tvl / 1e3:,.0f}K。${SMALL_VENUE_TVL / 1e6:g}M 未満）"]
    if level == "high":
        reasons.append(f"監査 {audits}件・事件の記録なし・載ってから {days / 365:.1f} 年")
    return _view(level, reasons)


def venue_from_registry(v: dict[str, Any]) -> dict[str, Any]:
    """自分で読む会場（venues/*.yaml。会場の画面の C4 と同じ材料）。"""
    unverified = [n for n, c in (v.get("contracts") or {}).items() if c.get("unverified", True)]
    if unverified:
        return _view("low", [f"住所を確かめられていない契約がある（{len(unverified)}件）"])
    reasons = [w.get("title_ja") or w.get("key") for w in v.get("warnings") or [] if w.get("code") == "C4"]
    if v.get("audited") is None:
        reasons.append("監査の有無が未確認")
    return _view("mid", reasons) if reasons else _view("high", ["契約はすべて確認済み・警告なし"])


def opportunity(o: dict[str, Any], venue: dict[str, Any] | None) -> dict[str, Any]:
    """入れる先の安全度（Opportunity.to_dict の中身から）。理由は下げたものから順に並べる。"""
    level = (venue or {}).get("level") or "mid"
    reasons: list[str] = []
    if venue and venue["level"] != "high":
        reasons += [f"会場: {r}" for r in venue["reasons"]]
    excl = [f["text"] for f in o.get("flags") or [] if f["level"] == "exclude"]
    if excl:
        level = "low"
        reasons = excl + reasons
    warn = [f["text"] for f in o.get("flags") or [] if f["level"] == "warn"]
    if warn:
        level = _cap(level, "mid")
        reasons += warn
    moves = [u for u in o.get("unprotected") or [] if "預かり証" not in u]
    if moves:
        level = _cap(level, "mid")
        reasons.append("保険で守れない値動きがある: " + "、".join(moves))
    left = o.get("days_left")
    if left is not None and left < SHORT_DAYS_LEFT:
        level = _cap(level, "mid")
        reasons.append(f"配る期間の残りが {left:.1f} 日（{SHORT_DAYS_LEFT:g}日未満）")
    if o.get("new_pool"):
        level = _cap(level, "mid")
        reasons.append("始まったばかりで、預かり額がまだ小さい")
    if o.get("over_cap"):
        level = _cap(level, "mid")
        reasons.append("入れる額が、入れてよい上限より多い")
    if not reasons:
        reasons.append("下げる印がない")
    return _view(level, reasons)
