"""登録の土台（N1。SPEC 13.4）: 系統 ＞ チェーン ＞ 会場 ＞ プール。

- 系統（family）: チェーンの読み方の大きな違い。今は ETH系（evm）だけ読める。ソラナ系は将来（読み方を1つ足す）。
- チェーン: chains/<id>.yaml に1つずつ。チェーンを足すときは、ファイルを置いて config.yaml の chains に1行足すだけ。
- 会場: venues/<id>.yaml。`chain: <チェーンの id>` でチェーンを指し、`mechanisms` に仕組みの型、`reader` に読み方の名前を書く。
  同じ型の会場を足すときは、読み方（コード）はそのままで、会場のファイルだけを足す。

住所・仕組みの確認のルール（CLAUDE.md の絶対ルール3・4）はチェーンのファイルにも当てはめる。
出典（source_url）と確認日（checked_at）を書き、確かめられないものは unverified: true にする。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT, ConfigError

# 系統。読み方（reader）は系統ごとに違い、そこから先の計算は共通（設計案 1章）
FAMILIES: dict[str, dict[str, Any]] = {
    "evm": {"label_ja": "ETH系", "readable": True},
    "solana": {"label_ja": "ソラナ系（将来。読み方はまだありません）", "readable": False},
}

# 仕組みの型（設計案 1章。お願いの5つに「取引量で配る型」を足し、投票の型は2つに分けた）
MECHANISMS: dict[str, str] = {
    "range": "幅に配る型（今の値段のまわりに置いたお金にだけ配る）",
    "full": "全体に配る型（置いたお金全体に配る）",
    "vote_weekly": "投票で決める型（週ごとに切り替わる）",
    "vote_continuous": "投票で決める型（いつでも変わる）",
    "later": "あとから配る型（Merkl など。あとで計算して配る）",
    "points": "ポイント型（利回りは0として扱う）",
    "volume": "取引量で配る型（預けた額からは利回りを計算できない）",
}

_REQUIRED_CHAIN = ("id", "name", "family", "source_url", "checked_at")


def chains_dir(root: Path = REPO_ROOT) -> Path:
    return root / "chains"


def load_chain(chain_id: str, root: Path = REPO_ROOT) -> dict[str, Any]:
    """chains/<id>.yaml を読み、決まりに合っているか確かめて返す。"""
    path = chains_dir(root) / f"{chain_id}.yaml"
    if not path.exists():
        raise ConfigError(f"チェーン {chain_id} の登録（{path}）がありません。")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    problems = chain_problems(data)
    if data.get("id") != chain_id:
        problems.insert(0, f"id が {chain_id} と一致しません")
    if problems:
        raise ConfigError(f"{path}: " + "、".join(problems))
    return data


def chain_problems(chain: dict[str, Any]) -> list[str]:
    out = [f"{k} がありません" for k in _REQUIRED_CHAIN if not chain.get(k)]
    fam = chain.get("family")
    if fam and fam not in FAMILIES:
        out.append(f"family={fam!r} は使えません（使えるのは {', '.join(FAMILIES)}）")
    if fam == "evm":
        if not isinstance(chain.get("chain_id"), int):
            out.append("ETH系のチェーンには chain_id（数）が要ります")
        if not chain.get("public_rpc") and not chain.get("unverified"):
            out.append("public_rpc がありません（確かめられないなら unverified: true）")
    return out


def list_chains(root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    """登録されているチェーンすべて（ファイル名の順）。"""
    d = chains_dir(root)
    return [load_chain(p.stem, root) for p in sorted(d.glob("*.yaml"))] if d.exists() else []


def resolve_venue_chain(venue: dict[str, Any], root: Path = REPO_ROOT) -> dict[str, Any]:
    """会場の `chain: <id>` をチェーンの登録の中身に置きかえる（ほかのコードは venue["chain"]["id"] などをそのまま使える）。

    昔の形（会場のファイルにチェーンの中身を直接書く）もそのまま受け付ける。
    """
    ref = venue.get("chain")
    if isinstance(ref, str):
        return {**venue, "chain": dict(load_chain(ref, root))}
    return venue


def venue_problems(venue: dict[str, Any], readers: set[str] | None = None) -> list[str]:
    """会場の登録が N1 の形になっているか（チェーンを指している・型が分かる・読み方がある）。"""
    out = []
    chain = venue.get("chain")
    if not isinstance(chain, dict) or not chain.get("id"):
        out.append("chain（チェーンの id）がありません")
    mechs = venue.get("mechanisms") or []
    if not mechs:
        out.append("mechanisms（仕組みの型）がありません")
    out += [f"仕組みの型 {m!r} は使えません" for m in mechs if m not in MECHANISMS]
    reader = venue.get("reader")
    if not reader:
        out.append("reader（読み方の名前）がありません")
    elif readers is not None and reader not in readers:
        out.append(f"読み方 {reader!r} はまだありません")
    return out


def overview(chain_ids: tuple[str, ...], venue_ids: tuple[str, ...], root: Path = REPO_ROOT,
             readers: set[str] | None = None) -> dict[str, Any]:
    """画面と API 用の一覧: 系統 ＞ チェーン ＞ 会場。問題があれば problems に書く（止めない）。"""
    from .config import load_venue

    problems: list[str] = []
    chains: dict[str, dict[str, Any]] = {}
    for cid in chain_ids:
        try:
            c = load_chain(cid, root)
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        chains[cid] = {
            "id": cid, "name": c["name"], "family": c["family"], "chain_id": c.get("chain_id"),
            "unverified": bool(c.get("unverified")), "source_url": c.get("source_url"),
            "checked_at": c.get("checked_at"), "venues": [],
        }
    for vid in venue_ids:
        try:
            v = load_venue(vid, root)
        except (OSError, ConfigError) as exc:
            problems.append(f"会場 {vid}: {exc}")
            continue
        cid = (v.get("chain") or {}).get("id")
        vp = venue_problems(v, readers)
        if cid not in chains:
            vp.append(f"チェーン {cid} が config.yaml の chains にありません")
        problems += [f"会場 {vid}: {p}" for p in vp]
        row = {"id": vid, "name": v.get("name", vid), "mechanisms": list(v.get("mechanisms") or []),
               "mechanisms_ja": [MECHANISMS.get(m, m) for m in v.get("mechanisms") or []],
               "reader": v.get("reader"), "practice": v.get("practice", True) is not False}
        if cid in chains:
            chains[cid]["venues"].append(row)
    families: dict[str, dict[str, Any]] = {}
    for c in chains.values():
        f = families.setdefault(c["family"], {"id": c["family"], "label_ja": FAMILIES[c["family"]]["label_ja"],
                                               "readable": FAMILIES[c["family"]]["readable"], "chains": []})
        f["chains"].append(c)
    return {"families": list(families.values()), "problems": problems}


def chain_by_evm_id(chain_ids: tuple[str, ...], root: Path = REPO_ROOT) -> dict[int, str]:
    """EVM のチェーン番号 → 登録の id（Merkl・送金サービスの一覧はチェーン番号で書かれているため）。"""
    out = {}
    for cid in chain_ids:
        c = load_chain(cid, root)
        if isinstance(c.get("chain_id"), int):
            out[c["chain_id"]] = cid
    return out


def coin_chains(chain_ids: tuple[str, ...], root: Path = REPO_ROOT) -> dict[int, str]:
    """EVM のチェーン番号 → DefiLlama の値段の名前（N2b。コインの値動きを読むため）。書いていないチェーンは入れない。"""
    out = {}
    for cid in chain_ids:
        try:
            c = load_chain(cid, root)
        except ConfigError:
            continue
        if isinstance(c.get("chain_id"), int) and c.get("defillama_coins_key"):
            out[c["chain_id"]] = str(c["defillama_coins_key"])
    return out
