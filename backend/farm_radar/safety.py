"""危なさ（N4a で N2c の「安全度（仮の3段階）」を置きかえた。点数と区分は riskscore.py、会場の見分けは venue_match.py）。

- 会場: 契約の住所で見分けた会場だけ、会場の情報（DefiLlama の会場・事件の一覧、会場の登録の運営の鍵・バグ報奨金）を使う。
  見分けられていない会場は、情報を「不明」として点を足す（安全側）。Merkl が名前で結びつけた会場の情報は、
  参考として理由に書くだけ（2026-10-02 17:07 JST オーナー依頼。名前の似た別の会場の情報が混ざることがあるため。例: Hyperdrive）。
- 自分で読む会場（up. など。venues/*.yaml）は、登録の住所がすべて確かめ済みなら「見分け済み」。
- 入れる先: 会場の材料に、入れる先の印（外す印・注意の印・守れない値動きなど）の点を足す。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from . import riskscore

UNCERTAIN_MATCH = "仮・会場の見分けが不確か"


def match_note(trust: dict[str, Any] | None) -> str | None:
    """会場の情報が名前だけで結びついているときの説明（住所で見分けられなかった Merkl の会場）。"""
    if not trust:
        return None
    slug = f"「{trust['slug']}」" if trust.get("slug") else ""
    return (f"{UNCERTAIN_MATCH}: 会場の情報{slug}は Merkl が名前で結びつけたもので、契約の住所では確かめていない。"
            "名前の似た別の会場の情報かもしれない")


def name_hint(trust: dict[str, Any] | None) -> str | None:
    """名前で結びついた会場の情報（参考。点には使わない）。"""
    if not trust:
        return None
    tvl = trust.get("tvl")
    bits = [f"監査の印 {trust.get('audits')}", f"事件 {trust.get('hacks') or 0}件"]
    if tvl is not None:
        bits.append(f"預かり額 ${tvl / 1e6:,.2f}M")
    return f"参考（点には使わない）: Merkl が名前で結びつけた会場の情報「{trust.get('slug') or '?'}」は " + "・".join(bits)


def venue_from_merkl(identity: dict[str, Any], facts: dict[str, Any] | None, trust: dict[str, Any] | None,
                     now: datetime) -> dict[str, Any]:
    """Merkl の会場。facts は住所で見分けた会場の情報（見分けていなければ None）。"""
    verified = identity.get("status") == "verified"
    parts = riskscore.venue_parts(identity, facts if verified else None, now)
    return riskscore.view(parts, identity, name_hint=None if verified else name_hint(trust))


def registry_identity(v: dict[str, Any]) -> dict[str, Any]:
    """自分で読む会場（venues/*.yaml）の見分け: 登録の住所がすべて確かめ済みなら見分け済み。"""
    unverified = [n for n, c in (v.get("contracts") or {}).items() if c.get("unverified", True)]
    base = {"venue_id": v.get("id"), "name": v.get("name"), "method": "registry", "source_url": None}
    if unverified:
        return {**base, "status": "unregistered",
                "reason": f"会場の登録に、住所を確かめられていない契約がある（{len(unverified)}件）"}
    return {**base, "status": "verified", "reason": "会場の登録の住所（すべて出典つきで確かめ済み）"}


def registry_facts(v: dict[str, Any], llama: dict[str, Any] | None) -> dict[str, Any]:
    """自分で読む会場の情報: 登録（監査・始まった日・警告）に、DefiLlama の会場（登録に書いた slug）を足す。"""
    f = dict(llama or {})
    if not f.get("listed_at") and v.get("launch_date"):
        try:
            d = date.fromisoformat(str(v["launch_date"]))
            f["listed_at"] = datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
            f["age_source"] = "会場の登録の始まった日"
        except ValueError:
            pass
    if v.get("audited") is True:
        f["audits"] = max(1, f.get("audits") or 0)
    if v.get("admin"):
        f["admin"] = v["admin"]
    if v.get("bug_bounty"):
        f["bug_bounty"] = v["bug_bounty"]
    return f


def venue_from_registry(v: dict[str, Any], llama: dict[str, Any] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
    """自分で読む会場（up. など）。会場の警告 C4（運営が手で配り先を決めるなど）は運営の鍵の材料に書く。"""
    now = now or datetime.now(UTC)
    identity = registry_identity(v)
    facts = registry_facts(v, llama)
    c4 = [w.get("title_ja") or w.get("key") for w in v.get("warnings") or [] if w.get("code") == "C4"]
    if c4 and not facts.get("admin"):
        facts["admin"] = {"type": "multisig", "note_ja": "運営が手で決める部分がある: " + "、".join(map(str, c4))}
    verified = identity["status"] == "verified"
    return riskscore.view(riskscore.venue_parts(identity, facts if verified else None, now), identity)


def opportunity(o: dict[str, Any], venue: dict[str, Any] | None, limits: dict[str, Any] | None = None,
                pool_cap_usd: float | None = None) -> dict[str, Any]:
    """入れる先の危なさ（Opportunity.to_dict の中身から）: 会場の点 + 入れる先の印の点。"""
    parts = list((venue or {}).get("parts") or riskscore.venue_parts(None, None, datetime.now(UTC)))
    parts += riskscore.opportunity_parts(o)
    hint = [r for r in (venue or {}).get("reasons") or [] if r.startswith("参考（点には使わない）")]
    return riskscore.view(parts, (venue or {}).get("identity"), limits, pool_cap_usd, hint[0] if hint else None)
