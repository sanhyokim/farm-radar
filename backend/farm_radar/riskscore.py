"""危なさの点数（N4a。SPEC 13.2「守る」・13.1 の追加の決定 6。2026-10-02 オーナー「N4a を始めてください」）。

N2c の「安全度（仮の3段階）」を置きかえる。0点から始めて、材料ごとに点を足す。点が多いほど危ない。
読めない材料は「不明」として点を足す（安全側）。点の重みと区切りはすべて仮（SPEC 13.3。N5 の試しで決め直す）。

材料:
- 会場の見分け（契約の住所。venue_match）。見分けられていないときは、会場の情報（監査・事件など）を使わない
  （名前の似た別の会場の情報かもしれないため。Merkl が名前で結びつけた情報は参考として理由に書くだけ）。
- 会場の年齢、監査、事件（DefiLlama の会場の番号で結びつけた事件の一覧）、会場全体の預かり額と、その1日・7日の減り方、
  運営の鍵（会場の登録に出典つきで書いたもの）、バグ報奨金（同じ）。
- ボーナスのコイン（値段の記録がない・値動きするコインで売り場の深さが分からない）。
- 入れる先の印（外す印・注意の印・保険で守れない値動き・残りの日数・始まったばかり・入れる額が上限より多い）。

区分（仮）: 0〜19 低い・20〜39 中くらい・40〜59 高い・60〜 とても高い。
推奨金額（仮。SPEC 13.2 の案）: 資金（config.yaml の limits.total_usd）の 低い 25%・中くらい 15%・高い 7.5%（高いものの合計は 20% まで）・
とても高いは練習だけ（本番は $0）。小資金（$2,000 未満）は、警告つきで全額でもよい。1つの建玉の上限・会場の上限・
プールの上限（1プールでの自分の割合）も超えない。上限の数字を変えるのはオーナーだけ（絶対ルール5）。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

LEVELS = ("low", "mid", "high", "very_high")
LABEL = {"low": "低い", "mid": "中くらい", "high": "高い", "very_high": "とても高い"}
BOUNDS = ((20, "low"), (40, "mid"), (60, "high"))        # この点より少なければ、その区分（仮）
SHARE_PCT = {"low": 25.0, "mid": 15.0, "high": 7.5, "very_high": 0.0}   # 資金のうち1か所に置いてよい割合（仮）
HIGH_TOTAL_PCT = 20.0              # 「高い」の場所の合計（仮。守るの画面で数える）
SMALL_CAPITAL_USD = 2000.0         # 小資金（SPEC 13.2。仮。設定で変えられるようにするのは N4b）

ID_POINTS = {"verified": 0, "unchecked": 15, "hooked": 20, "unregistered": 25, "no_address": 25, "mismatch": 40}
ADMIN_POINTS = {"immutable": 0, "timelock": 3, "multisig": 8, "single": 15}
SHORT_DAYS_LEFT = 7.0
RECENT_HACK_DAYS = 365


def level_of(score: float) -> str:
    for bound, lv in BOUNDS:
        if score < bound:
            return lv
    return "very_high"


def _part(key: str, label: str, points: float, note: str) -> dict[str, Any]:
    return {"key": key, "label": label, "points": round(points, 1), "note": note}


def venue_parts(identity: dict[str, Any] | None, facts: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    """会場の材料。facts は住所で見分けた会場の情報だけ（見分けていないときは None を渡す）。"""
    out: list[dict[str, Any]] = []
    status = (identity or {}).get("status") or "unregistered"
    out.append(_part("identity", "会場の見分け", ID_POINTS.get(status, 25), (identity or {}).get("reason") or status))
    f = facts or {}
    # 年齢
    listed = f.get("listed_at")
    if listed:
        days = (now.timestamp() - float(listed)) / 86400
        pts = 20 if days < 30 else 10 if days < 90 else 0
        out.append(_part("age", "会場の年齢", pts, f"動いて {days:.0f} 日（{f.get('age_source') or '記録'}）"))
    else:
        out.append(_part("age", "会場の年齢", 10, "分からない"))
    # 監査
    audits, links = f.get("audits"), f.get("audit_links")
    if audits is None and links is None:
        out.append(_part("audits", "監査", 10, "分からない"))
    elif (audits or 0) > 0 or (links or 0) > 0:
        out.append(_part("audits", "監査", 0, f"監査の記録あり（資料 {links or 0} 件・DefiLlama）"))
    else:
        out.append(_part("audits", "監査", 15, "監査の記録がない"))
    # 事件
    hacks = f.get("hacks")
    if hacks is None:
        out.append(_part("hacks", "事件の記録", 5, "分からない（会場の見分けができていない、または一覧が読めていない）"))
    elif hacks:
        last = max(h.get("date") or 0 for h in hacks)
        recent = (now.timestamp() - last) / 86400 < RECENT_HACK_DAYS
        total = sum(h.get("amount") or 0 for h in hacks)
        out.append(_part("hacks", "事件の記録", 25 if recent else 10,
                         f"{len(hacks)}件（合計 ${total / 1e6:,.1f}M。いちばん新しいのは "
                         f"{datetime.fromtimestamp(last).strftime('%Y-%m-%d')}）"))
    else:
        out.append(_part("hacks", "事件の記録", 0, "記録なし（DefiLlama の事件の一覧）"))
    # 会場全体の預かり額と減り方
    tvl = f.get("tvl")
    if tvl is None:
        out.append(_part("tvl", "会場全体の預かり額", 8, "分からない"))
    else:
        pts = 0 if tvl >= 100e6 else 4 if tvl >= 10e6 else 8 if tvl >= 1e6 else 15
        out.append(_part("tvl", "会場全体の預かり額", pts, f"${tvl / 1e6:,.1f}M"))
        d1, d7 = f.get("change_1d"), f.get("change_7d")
        drop = 15 if d1 is not None and d1 <= -20 else 8 if d1 is not None and d1 <= -10 else 0
        drop += 10 if d7 is not None and d7 <= -30 else 0
        if drop:
            out.append(_part("tvl_drop", "預かり額の急な減り", drop,
                             f"1日 {d1:+.1f}%・7日 {d7:+.1f}%" if d1 is not None and d7 is not None
                             else f"1日 {d1}%・7日 {d7}%"))
    # 運営の鍵
    admin = f.get("admin") or {}
    typ = admin.get("type")
    if typ in ADMIN_POINTS:
        out.append(_part("admin", "運営の鍵", ADMIN_POINTS[typ], admin.get("note_ja") or typ))
    else:
        out.append(_part("admin", "運営の鍵", 8, "分からない"))
    # バグ報奨金
    bb = f.get("bug_bounty") or {}
    if bb.get("url"):
        top = f"（最大 ${float(bb['max_usd']) / 1e6:,.1f}M）" if bb.get("max_usd") else ""
        out.append(_part("bounty", "バグ報奨金", 0, f"あり{top}"))
    else:
        out.append(_part("bounty", "バグ報奨金", 5, "分からない"))
    return out


def opportunity_parts(o: dict[str, Any]) -> list[dict[str, Any]]:
    """入れる先の材料（Opportunity.to_dict の中身から）。"""
    out: list[dict[str, Any]] = []
    flags = o.get("flags") or []
    codes = {f["code"] for f in flags}
    excl = [f["text"] for f in flags if f["level"] == "exclude"]
    if excl:
        out.append(_part("exclude", "外す印", 40, "、".join(excl)))
    if "RWD_GUESS" in codes:
        out.append(_part("reward", "ボーナスのコイン", 10, "値段の記録がない（仮の値下がりで計算）"))
    elif any(str(u).startswith("ボーナスのコイン") for u in o.get("unprotected") or []):
        out.append(_part("reward", "ボーナスのコイン", 3, "値動きするコイン。売り場の深さはまだ見ていない"))
    warn = [f["text"] for f in flags if f["level"] == "warn" and f["code"] != "RWD_GUESS"]
    if warn:
        out.append(_part("warn", "注意の印", min(12, 4 * len(warn)), "、".join(warn)))
    moves = [u for u in o.get("unprotected") or [] if not str(u).startswith("ボーナス")]
    if moves:
        out.append(_part("unprotected", "保険で守れない値動き", 5, "、".join(moves)))
    left = o.get("days_left")
    if left is not None and left < SHORT_DAYS_LEFT:
        out.append(_part("days_left", "配る期間の残り", 5, f"{left:.1f} 日"))
    if o.get("new_pool"):
        out.append(_part("new_pool", "始まったばかり", 10, "預かり額がまだ小さい"))
    if o.get("over_cap"):
        out.append(_part("over_cap", "入れる額", 5, "入れてよい上限（1プールでの自分の割合）より多い"))
    return out


def recommend(level: str, limits: dict[str, Any], pool_cap_usd: float | None) -> dict[str, Any]:
    """推奨金額（本番で1か所に置いてよい額の目安）と理由。"""
    total = float(limits["total_usd"]) if limits.get("total_usd") is not None else None
    if total is None:
        return {"usd": None, "pct": None, "small_capital": False, "note": "資金の上限（limits.total_usd）がない"}
    caps = [(total, "資金")]
    if limits.get("position_usd") is not None:
        caps.append((float(limits["position_usd"]), "1つの建玉の上限"))
    if limits.get("per_venue_share") is not None:
        caps.append((total * float(limits["per_venue_share"]), "1つの会場の上限"))
    if pool_cap_usd is not None:
        caps.append((pool_cap_usd, "1プールでの自分の割合の上限"))
    small = total < SMALL_CAPITAL_USD
    pct = 100.0 if small else SHARE_PCT[level]
    usd, why = total * pct / 100, f"資金 ${total:,.0f} の {pct:g}%（危なさ {LABEL[level]}）"
    for cap, name in caps[1:]:
        if cap < usd:
            usd, why = cap, f"{name} ${cap:,.0f}"
    note = f"上限 ${usd:,.0f}: {why}"
    if small and level in ("high", "very_high"):
        note += "。小資金なので全額でもよいが、危なさが高い（警告）"
    elif level == "very_high":
        note = "本番は $0（練習だけ）。危なさがとても高い"
    elif level == "high":
        note += f"。危なさが高い場所の合計は資金の {HIGH_TOTAL_PCT:g}% まで"
    return {"usd": round(usd, 2), "pct": pct, "small_capital": small, "note": note}


def view(parts: list[dict[str, Any]], identity: dict[str, Any] | None, limits: dict[str, Any] | None = None,
         pool_cap_usd: float | None = None, name_hint: str | None = None) -> dict[str, Any]:
    """画面に出す形（N2c の safety と同じ場所に入れる）。理由は点の多い順。"""
    score = sum(p["points"] for p in parts)
    level = level_of(score)
    status = (identity or {}).get("status") or "unregistered"
    reasons = [f"{p['label']}: {p['note']}（+{p['points']:g}）" for p in sorted(parts, key=lambda x: -x["points"])
               if p["points"] > 0]
    if name_hint:
        reasons.append(name_hint)
    if not reasons:
        reasons = ["点を足す材料がない"]
    out = {"level": level, "label": LABEL[level], "score": round(score, 1), "provisional": True, "parts": parts,
           "reasons": reasons, "identity": identity, "uncertain_match": status != "verified"}
    if limits is not None:
        out["recommend"] = recommend(level, limits, pool_cap_usd)
    return out


__all__ = ["LEVELS", "LABEL", "level_of", "venue_parts", "opportunity_parts", "recommend", "view"]
