"""N4a: 会場を契約の住所で見分ける・危なさの点数・推奨金額（2026-10-02 23:34 JST オーナー「N4a を始めてください」）。"""

from datetime import UTC, datetime

import pytest

from farm_radar import riskscore, safety, venue_match
from farm_radar.config import ConfigError

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
OLD = NOW.timestamp() - 800 * 86400
VAULT = "0x" + "ab" * 20
FACTORY = "0x" + "cd" * 20
FACTORY2 = "0x" + "ef" * 20
KNOWN = {
    "morpho": {"id": "morpho", "name": "Morpho", "merkl_protocols": ["Morpho"],
               "match": {"base": [
                   {"method": "factory", "factory": FACTORY, "function": "isMetaMorpho(address)",
                    "source_url": "https://example.org/a", "checked_at": "2026-10-02"},
                   {"method": "factory", "factory": FACTORY2, "function": "isVaultV2(address)",
                    "source_url": "https://example.org/b", "checked_at": "2026-10-02"}]}},
    "uniswap": {"id": "uniswap", "name": "Uniswap", "merkl_protocols": ["Uniswap"],
                "match": {"robinhood": [{"method": "uniswap", "source_url": "https://example.org/u",
                                         "checked_at": "2026-10-02"}]}},
}
BY = venue_match.by_protocol(KNOWN)


def _id(protocol="Morpho", chain="base", cid=8453, addr=VAULT, checks=None, cl=None):
    return venue_match.identify(protocol, chain, cid, addr, BY, checks or {}, cl)


def test_identify_by_official_factory_answer():
    assert _id()["status"] == "unchecked"                               # まだ聞いていない
    one = {(8453, VAULT): {FACTORY: {"verified": 0, "checked_at": "t"}}}
    assert _id(checks=one)["status"] == "unchecked"                     # もう1つの工場にまだ聞いていない
    both_no = {(8453, VAULT): {FACTORY: {"verified": 0}, FACTORY2: {"verified": 0}}}
    assert _id(checks=both_no)["status"] == "mismatch"
    yes = {(8453, VAULT): {FACTORY: {"verified": 0}, FACTORY2: {"verified": 1, "checked_at": "t"}}}
    r = _id(checks=yes)
    assert r["status"] == "verified" and r["venue_id"] == "morpho" and r["source_url"] == "https://example.org/b"
    # 名前が登録にない・このチェーンの住所を確かめていない
    assert _id(protocol="Hyperdrive")["status"] == "unregistered"
    assert _id(chain="robinhood", cid=4663)["status"] == "unregistered"
    assert _id(addr=None)["status"] == "no_address"


def test_identify_uniswap_by_official_pool_reads_and_hooks():
    u = dict(protocol="Uniswap", chain="robinhood", cid=4663, addr=None)
    assert _id(**u)["status"] == "unchecked"
    assert _id(**u, cl={"official": 1, "hooks": "0x" + "0" * 40})["status"] == "verified"
    assert _id(**u, cl={"official": 1, "hooks": "0x64e9ae10" + "0" * 32})["status"] == "hooked"
    assert _id(**u, cl={"official": 0, "hooks": None})["status"] == "mismatch"


def test_rules_with_merkl_types_only_apply_to_those_kinds():
    known = {"m": {"id": "m", "name": "M", "merkl_protocols": ["M"], "match": {"base": [
        {"method": "factory", "factory": FACTORY, "function": "isVaultV2(address)", "merkl_types": ["MORPHOVAULT"],
         "source_url": "https://example.org/a", "checked_at": "2026-10-02"}]}}}
    by = venue_match.by_protocol(known)
    yes = {(8453, VAULT): {FACTORY: {"verified": 1, "checked_at": "t"}}}
    assert venue_match.identify("M", "base", 8453, VAULT, by, yes, kind="MORPHOVAULT")["status"] == "verified"
    # 「借りる」型は Merkl の住所がコインの住所なので、工場には聞かず「住所で見分けられない」
    r = venue_match.identify("M", "base", 8453, VAULT, by, yes, kind="MORPHOBORROW")
    assert r["status"] == "no_address" and "MORPHOBORROW" in r["reason"]
    assert venue_match.factory_targets(known["m"], "base", "MORPHOBORROW") == []


def test_real_registry_has_official_venues():
    known = venue_match.load_known()
    assert {"uniswap", "morpho", "hyperdrive", "ipor-fusion"} <= set(known)
    hd = known["hyperdrive"]
    # Merkl が結びつけた DefiLlama の「hyperdrive」（別の会社の終わったプロジェクト）は使わない
    assert "hyperdrive" not in hd["defillama"]["slugs"]
    by = venue_match.by_protocol(known)
    r = venue_match.identify("Hyperdrive", "base", 8453, "0x58a0f600d555eb25b792Ec2c6824D0bff5127B1F", by, {})
    assert r["status"] == "verified" and r["method"] == "addresses"


def test_registry_rules_need_sources_and_address_shapes(tmp_path):
    d = tmp_path / "venues" / "known"
    d.mkdir(parents=True)
    (d / "x.yaml").write_text("id: x\nname: X\nmerkl_protocols: [X]\nmatch:\n  base:\n"
                              "    - {method: factory, factory: '0x12', function: 'isX(address)'}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="source_url"):
        venue_match.load_known(tmp_path)
    (d / "x.yaml").write_text("id: x\nname: X\nmerkl_protocols: [X]\n", encoding="utf-8")
    assert venue_match.load_known(tmp_path)["x"]["name"] == "X"


def test_real_registry_files_follow_the_rules():
    known = venue_match.load_known()
    for v in known.values():
        assert venue_match.problems(v) == []


def _facts(**kw):
    f = {"listed_at": OLD, "audits": 2, "audit_links": 3, "hacks": [], "tvl": 5e8, "change_1d": 1.0,
         "change_7d": 2.0, "admin": {"type": "timelock", "note_ja": "時間を置く仕組み"},
         "bug_bounty": {"url": "https://immunefi.com/x", "max_usd": 2_500_000}}
    f.update(kw)
    return f


def test_points_add_up_and_unknown_counts_as_risky():
    ok = {"status": "verified", "reason": "ok"}
    v = riskscore.view(riskscore.venue_parts(ok, _facts(), NOW), ok)
    assert v["score"] == 3 and v["level"] == "low" and v["uncertain_match"] is False
    # 見分けていない会場は、情報を「分からない」として数える（名前で結びついた情報は使わない）
    s = safety.venue_from_merkl({"status": "unregistered", "reason": "登録がない"}, None,
                                {"audits": 2, "hacks": 0, "tvl": 78_672.0, "slug": "hyperdrive"}, NOW)
    assert s["uncertain_match"] is True and s["score"] == 25 + 10 + 10 + 5 + 8 + 8 + 5
    assert s["level"] == "very_high" and any("参考（点には使わない）" in r and "hyperdrive" in r for r in s["reasons"])
    # 最近の事件・小さい会場・急な減り
    bad = riskscore.venue_parts(ok, _facts(hacks=[{"date": NOW.timestamp() - 30 * 86400, "amount": 7.8e5}],
                                           tvl=5e5, change_1d=-25.0), NOW)
    pts = {p["key"]: p["points"] for p in bad}
    assert pts["hacks"] == 25 and pts["tvl"] == 15 and pts["tvl_drop"] == 15
    assert sum(pts.values()) == 58 and riskscore.level_of(58) == "high" and riskscore.level_of(60) == "very_high"


def test_opportunity_marks_add_points_and_show_reasons_by_size():
    ok = {"status": "verified", "reason": "ok"}
    venue = riskscore.view(riskscore.venue_parts(ok, _facts(), NOW), ok)
    o = {"flags": [{"code": "RWD_GUESS", "level": "warn", "text": "値下がり未計算"},
                   {"code": "X", "level": "warn", "text": "注意"}],
         "unprotected": ["ボーナスのコイン（X）の値下がり", "WETH の値下がり（保険の売り場がない）"], "days_left": 3}
    s = safety.opportunity(o, venue, {"total_usd": 3000, "position_usd": 1000, "per_venue_share": 0.5}, None)
    pts = {p["key"]: p["points"] for p in s["parts"]}
    assert pts["reward"] == 10 and pts["warn"] == 4 and pts["unprotected"] == 5 and pts["days_left"] == 5
    assert s["score"] == 3 + 24 and s["level"] == "mid" and s["reasons"][0].startswith("ボーナスのコイン")
    excl = safety.opportunity({"flags": [{"code": "P", "level": "exclude", "text": "取り返せない"}],
                               "unprotected": []}, venue)
    assert excl["level"] == "high" and "recommend" not in excl


@pytest.mark.parametrize("level,total,cap,usd,small", [
    ("low", 3000, None, 750, False), ("mid", 3000, None, 450, False), ("high", 3000, None, 225, False),
    ("very_high", 3000, None, 0, False), ("low", 3000, 300.0, 300, False),
    ("low", 10000, None, 1000, False),                 # 1つの建玉の上限 $1,000 が先に効く
    ("high", 1000, None, 500, True),                   # 小資金: 全額でもよいが、会場の上限（50%）は超えない
])
def test_recommended_amount(level, total, cap, usd, small):
    r = riskscore.recommend(level, {"total_usd": total, "position_usd": 1000, "per_venue_share": 0.5}, cap)
    assert r["usd"] == pytest.approx(usd) and r["small_capital"] is small
    if level == "very_high" and not small:
        assert "練習だけ" in r["note"]
    if small:
        assert "警告" in r["note"]
