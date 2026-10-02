"""会場を契約の住所で見分ける（N4a。SPEC 13.1 の追加の決定 6、2026-10-02 オーナー）。

Merkl の入れる先は「どの会場か」を Merkl が名前で付けている。名前の似た別の会場の情報が混ざることがある（例: Hyperdrive）。
ここでは、入れる先の契約の住所が、会場の登録（venues/known/<id>.yaml）に出典つきで書いた公式の住所と合うかで見分ける。

見分け方（method）:
- uniswap: Uniswap の公式の住所（chains/<id>.yaml の uniswap）で読めたプール（N3 の pool_states で official=1）。
  フック付きのプールは、フックを作った会場がわからないので「見分けられない」にする。
- factory: 会場の公式の工場（factory）の契約に「この住所はあなたが作ったか」を聞く（feeds/venues.py が1日1回読む）。
- addresses: 会場の公式の資料に載っている住所の一覧と比べる。

住所は推測しない（絶対ルール3）。登録に書くのは、公式の資料・エクスプローラー・公開済みの契約の中身で確かめた住所だけ。
読み取りだけ。お金を動かすコードはない。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT, ConfigError

METHODS = ("uniswap", "factory", "addresses")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_FUNC = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(address\)$")

# 見分けの結果（画面の言葉）
STATUS_JA = {
    "verified": "住所で見分け済み",
    "mismatch": "住所が公式のものと合わない",
    "unchecked": "まだ確かめていない",
    "hooked": "フック付きのプール（フックを作った会場が分からない）",
    "unregistered": "会場の登録がない（公式の住所をまだ確かめていない）",
    "no_address": "入れる先の住所が分からない",
}


def known_dir(root: Path = REPO_ROOT) -> Path:
    return root / "venues" / "known"


def problems(v: dict[str, Any]) -> list[str]:
    """登録の決まり（絶対ルール3）: 出典と確認日のない住所は使わない。"""
    out = [f"{k} がありません" for k in ("id", "name", "merkl_protocols") if not v.get(k)]
    for chain, rules in (v.get("match") or {}).items():
        for i, r in enumerate(rules or []):
            where = f"match.{chain}[{i}]"
            if r.get("method") not in METHODS:
                out.append(f"{where}: method は {', '.join(METHODS)} のどれか")
            if not r.get("source_url") or not r.get("checked_at"):
                out.append(f"{where}: source_url と checked_at が要ります")
            if r.get("method") == "factory":
                if not _ADDR.match(str(r.get("factory") or "")):
                    out.append(f"{where}: factory の住所の形が違います")
                if not _FUNC.match(str(r.get("function") or "")):
                    out.append(f"{where}: function は 名前(address) の形")
            if r.get("method") == "addresses":
                bad = [a for a in r.get("addresses") or [] if not _ADDR.match(str(a))]
                if bad or not r.get("addresses"):
                    out.append(f"{where}: addresses の住所の形が違います")
    return out


def load_known(root: Path = REPO_ROOT) -> dict[str, dict[str, Any]]:
    """venues/known/*.yaml を読む（決まりに合わないものは読み込まずに止める）。"""
    out: dict[str, dict[str, Any]] = {}
    d = known_dir(root)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.yaml")):
        v = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        bad = problems(v)
        if bad:
            raise ConfigError(f"{path}: " + "、".join(bad))
        if v["id"] != path.stem:
            raise ConfigError(f"{path}: id が {path.stem} と一致しません")
        out[v["id"]] = v
    return out


def by_protocol(known: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merkl の会場の名前（小文字）→ 登録。"""
    out: dict[str, dict[str, Any]] = {}
    for v in known.values():
        for name in v.get("merkl_protocols") or []:
            out[str(name).lower()] = v
    return out


def rules(v: dict[str, Any], chain: str | None) -> list[dict[str, Any]]:
    return [r for r in (v.get("match") or {}).get(chain or "", []) or [] if not r.get("unverified")]


def factory_targets(v: dict[str, Any], chain: str | None) -> list[dict[str, Any]]:
    return [r for r in rules(v, chain) if r.get("method") == "factory"]


def identify(protocol: str | None, chain: str | None, chain_id: int | None, address: str | None,
             known_by_protocol: dict[str, dict[str, Any]], checks: dict[tuple[int, str], dict[str, Any]],
             cl_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """入れる先がどの会場かを、契約の住所で見分ける。

    戻り値: status（STATUS_JA のどれか）, venue_id, name, method, reason, source_url。
    checks は feeds/venues.py の結果（(チェーン番号, 住所) → {工場の住所（小文字）→ {verified, checked_at, error}}）。
    答えが出ていない（読み取り口の失敗）ものは入れない。
    cl_state は N3 の幅に配るプールの読み取り（official・hooks）。
    """
    v = known_by_protocol.get(str(protocol or "").lower())
    base = {"venue_id": v["id"] if v else None, "name": v["name"] if v else protocol, "method": None,
            "source_url": None}
    if v is None:
        return {**base, "status": "unregistered", "reason": STATUS_JA["unregistered"]}
    rs = rules(v, chain)
    if not rs:
        return {**base, "status": "unregistered",
                "reason": f"このチェーンでの {v['name']} の公式の住所をまだ確かめていない"}
    addr = str(address or "").lower()
    for r in rs:
        m = r["method"]
        if m == "uniswap":
            if cl_state is None:
                continue
            if cl_state.get("official") != 1:
                return {**base, "status": "mismatch", "method": m, "source_url": r["source_url"],
                        "reason": "Uniswap の公式の住所で読めないプール"}
            hooks = str(cl_state.get("hooks") or "")
            if hooks and int(hooks, 16) != 0:
                return {**base, "status": "hooked", "method": m, "source_url": r["source_url"],
                        "reason": f"{STATUS_JA['hooked']}（フック {hooks[:10]}…）"}
            return {**base, "status": "verified", "method": m, "source_url": r["source_url"],
                    "reason": "Uniswap の公式の住所（PoolManager・factory）で読めたプール"}
        if not _ADDR.match(addr):
            continue
        if m == "addresses":
            if addr in {str(a).lower() for a in r["addresses"]}:
                return {**base, "status": "verified", "method": m, "source_url": r["source_url"],
                        "reason": "会場の公式の資料に載っている住所"}
            continue
        if m == "factory" and chain_id is not None:
            c = (checks.get((int(chain_id), addr)) or {}).get(str(r["factory"]).lower())
            if c is not None and c.get("verified") == 1:
                return {**base, "status": "verified", "method": m, "source_url": r["source_url"],
                        "reason": f"会場の公式の工場（{r['factory'][:10]}…）が作った契約（チェーンの記録 {c.get('checked_at')}）"}
    if not _ADDR.match(addr) and not any(r["method"] == "uniswap" for r in rs):
        return {**base, "status": "no_address", "reason": STATUS_JA["no_address"]}
    pending = chain_id is not None and any(
        r["method"] == "factory" and (checks.get((int(chain_id), addr)) or {}).get(str(r["factory"]).lower()) is None
        for r in rs)
    if cl_state is None and any(r["method"] == "uniswap" for r in rs):
        return {**base, "status": "unchecked",
                "reason": "まだチェーンで読めていないプール（1時間に1回読む。読めるのは Uniswap v3・v4 の幅に配るプールだけ）"}
    if pending:
        return {**base, "status": "unchecked", "reason": "まだ確かめていない（1日1回チェーンの記録で確かめる）"}
    return {**base, "status": "mismatch",
            "reason": f"{v['name']} の公式の工場・住所の一覧と合わない（名前だけが同じ別の契約かもしれない）"}


__all__ = ["METHODS", "STATUS_JA", "load_known", "by_protocol", "identify", "factory_targets", "problems", "rules"]
