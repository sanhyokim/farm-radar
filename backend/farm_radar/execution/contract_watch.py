"""会場プログラムの見張り（2026-09-29 オーナー決定。SPEC 12.2章）。読み取りだけ。

15分ごとの収集のたびに、会場の確認済みのコントラクト（venues/*.yaml）と、練習の建玉のプール・ボーナス（ゲージ）・
2つのトークン・USDG について、次を読み取って前回と比べる。変わっていたら緊急離脱の材料にする（risk/rules.py）。
- プログラムの中身（eth_getCode のハッシュ）          → 変わったら「プログラムの入れ替え」
- 入れ替え式のプログラム（EIP-1967 の実装・管理者・ビーコンの保存場所） → 変わったら「プログラムの入れ替え」「持ち主の変更」
- owner()（持ち主）                                   → 変わったら「持ち主の変更」
- paused()（停止中か）                                → true になったら「停止」
- Voter の governor() / emergencyCouncil() / epochGovernor()（Sourcify の確認済みの ABI にある関数）→ 変わったら「持ち主の変更」
- 建玉のプールのゲージが生きているか（収集で読んでいる Voter.isAlive）  → 止まったら「停止」
その関数がないプログラムは読み取れない（revert する）ので「未確認」として記録し、画面に出す。
通信の失敗は「今回は読めなかった」として比べない（次の回にもう一度読む）。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import contract_address
from ..rpc.abi import selector
from ..rpc.client import RpcCallError, RpcClient
from ..tokens import TokenBook

log = logging.getLogger(__name__)

# EIP-1967 の保存場所（keccak256("eip1967.proxy.implementation") − 1 など。規格で決まっている値）
SLOTS = {
    "impl": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "admin": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
    "beacon": "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",
}
COMMON_CALLS = {"owner": "owner()", "paused": "paused()"}
VOTER_CALLS = {"governor": "governor()", "emergencyCouncil": "emergencyCouncil()", "epochGovernor": "epochGovernor()"}

ITEM_JA = {
    "code": "プログラムの中身", "impl": "入れ替え式プログラムの実装先", "admin": "入れ替え式プログラムの管理者",
    "beacon": "入れ替え式プログラムの参照先", "owner": "持ち主（owner）", "paused": "停止中か（paused）",
    "governor": "管理者（governor）", "emergencyCouncil": "緊急の管理者（emergencyCouncil）",
    "epochGovernor": "週の管理者（epochGovernor）", "gauge_alive": "ボーナスの配布（ゲージ）",
}
KIND_JA = {"code": "プログラムの入れ替え", "impl": "プログラムの入れ替え", "beacon": "プログラムの入れ替え",
           "admin": "持ち主の変更", "owner": "持ち主の変更", "governor": "持ち主の変更",
           "emergencyCouncil": "持ち主の変更", "epochGovernor": "持ち主の変更",
           "paused": "停止", "gauge_alive": "停止"}
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Target:
    address: str
    label: str
    calls: dict[str, str]


def targets(conn: sqlite3.Connection, venue: dict[str, Any], tokens: TokenBook) -> list[Target]:
    """見張る相手の一覧。アドレスは確認済みのもの（venues/*.yaml・tokens-*.yaml・収集の記録）だけを使う。"""
    out: dict[str, Target] = {}

    def add(addr: str | None, label: str, extra: dict[str, str] | None = None) -> None:
        if not addr:
            return
        a = addr.lower()
        if a in out:
            return
        out[a] = Target(a, label, {**COMMON_CALLS, **(extra or {})})

    for name in (venue.get("contracts") or {}):
        add(contract_address(venue, name), f"{venue.get('name', venue['id'])} {name}",
            VOTER_CALLS if name == "voter" else None)
    for addr in sorted(tokens.stablecoins):
        add(addr, _symbol(conn, addr) or "ステーブルコイン")
    rows = conn.execute("""SELECT p.id, p.token0, p.token1, p.token0_symbol, p.token1_symbol, p.gauge_address
                           FROM positions x JOIN pools p ON p.id=x.pool_id
                           WHERE x.is_paper=1 AND x.status='open'""").fetchall()
    for r in rows:
        pair = f"{r['token0_symbol']}/{r['token1_symbol']}"
        add(r["id"], f"{pair} のプール")
        add(r["gauge_address"], f"{pair} のゲージ")
        add(r["token0"], r["token0_symbol"] or "token0")
        add(r["token1"], r["token1_symbol"] or "token1")
    return list(out.values())


def _symbol(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute("""SELECT CASE WHEN lower(token0)=? THEN token0_symbol ELSE token1_symbol END FROM pools
                          WHERE lower(token0)=? OR lower(token1)=? LIMIT 1""", (token, token, token)).fetchone()
    return row[0] if row and row[0] else None


def _word_to_value(item: str, raw: str) -> str:
    data = bytes.fromhex(raw.removeprefix("0x"))
    if len(data) < 32:
        return UNREADABLE
    word = data[:32]
    if item == "paused":
        v = int.from_bytes(word, "big")
        return str(v == 1).lower() if v in (0, 1) else UNREADABLE
    if any(word[:12]):          # アドレスなら上の12バイトは0
        return UNREADABLE
    return "0x" + word[12:].hex()


def read_target(rpc: RpcClient, t: Target) -> dict[str, str | None]:
    """1つの相手を読む。値 / UNREADABLE（関数がない）/ None（通信の失敗。今回は比べない）。"""
    vals: dict[str, str | None] = {}
    try:
        code = rpc.get_code(t.address)
        vals["code"] = hashlib.sha256(code.encode()).hexdigest()[:16] if code and code != "0x" else UNREADABLE
    except Exception:
        vals["code"] = None
    for key, slot in SLOTS.items():
        try:
            w = rpc.get_storage(t.address, slot)
            vals[key] = "0x" + w.removeprefix("0x").rjust(64, "0")[-40:]
        except Exception:
            vals[key] = None
    for key, sig in t.calls.items():
        try:
            vals[key] = _word_to_value(key, rpc.eth_call(t.address, "0x" + selector(sig).hex(), "latest"))
        except RpcCallError:
            vals[key] = UNREADABLE
        except Exception:
            vals[key] = None
    return vals


def _describe(t: Target, item: str, old: str, new: str) -> str:
    what = KIND_JA.get(item, "変化")
    if item == "paused" and new == "true":
        return f"{t.label}: 停止されました（paused）"
    if item == "gauge_alive" and new == "false":
        return f"{t.label}: ボーナスの配布が止められました（ゲージ停止）"
    if new == UNREADABLE:
        return f"{t.label}: {ITEM_JA.get(item, item)}が読めなくなりました（{what}の可能性）"
    return f"{t.label}: {what}（{ITEM_JA.get(item, item)} {old[:10]}… → {new[:10]}…）"


def check(conn: sqlite3.Connection, rpc: RpcClient, venue: dict[str, Any], tokens: TokenBook,
          now: datetime | None = None) -> list[str]:
    """読み取って前回と比べ、変わったことの説明を返す（初めての相手は記録するだけ）。"""
    now = now or datetime.now(UTC)
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    changes: list[str] = []
    tlist = targets(conn, venue, tokens)
    for t in tlist:
        vals = read_target(rpc, t)
        changes += _store(conn, t, vals, ts)
    # 建玉のプールのゲージが生きているか（収集で読んでいる値を使う）
    for r in conn.execute("""SELECT p.id, p.token0_symbol, p.token1_symbol, p.gauge_address,
                                    (SELECT s.gauge_alive FROM pool_snapshots s WHERE s.pool_id=p.id
                                     ORDER BY s.ts DESC LIMIT 1) AS alive
                             FROM positions x JOIN pools p ON p.id=x.pool_id
                             WHERE x.is_paper=1 AND x.status='open' AND p.gauge_address IS NOT NULL"""):
        t = Target(r["gauge_address"].lower(), f"{r['token0_symbol']}/{r['token1_symbol']} のゲージ", {})
        val = None if r["alive"] is None else str(bool(r["alive"])).lower()
        changes += _store(conn, t, {"gauge_alive": val}, ts)
    conn.commit()
    if changes:
        log.warning("contract changes", extra={"data": {"changes": changes}})
    return changes


def _store(conn: sqlite3.Connection, t: Target, vals: dict[str, str | None], ts: str) -> list[str]:
    changes = []
    for item, new in vals.items():
        if new is None:
            continue
        row = conn.execute("SELECT value FROM contract_watch WHERE address=? AND item=?", (t.address, item)).fetchone()
        if row is None:
            conn.execute("INSERT INTO contract_watch(address, item, label, value, first_seen, checked_at) "
                         "VALUES (?,?,?,?,?,?)", (t.address, item, t.label, new, ts, ts))
            if item == "paused" and new == "true":
                changes.append(f"{t.label}: 停止中です（paused）")
            continue
        old = row["value"]
        if old != new:
            # 「関数がない」から値が読めるようになっただけ（同じプログラム）の場合は記録だけ
            if old != UNREADABLE or item in ("code", "paused", "gauge_alive"):
                changes.append(_describe(t, item, old, new))
            conn.execute("UPDATE contract_watch SET value=?, changed_at=?, checked_at=?, label=? "
                         "WHERE address=? AND item=?", (new, ts, ts, t.label, t.address, item))
        else:
            conn.execute("UPDATE contract_watch SET checked_at=?, label=? WHERE address=? AND item=?",
                         (ts, t.label, t.address, item))
    return changes


def status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """画面用: 相手ごとに、読めた項目と「未確認」の項目。"""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM contract_watch ORDER BY label, item"):
        d = out.setdefault(r["address"], {"address": r["address"], "label": r["label"], "checked_at": r["checked_at"],
                                          "ok": [], "unconfirmed": [], "changed_at": None})
        name = ITEM_JA.get(r["item"], r["item"])
        if r["value"] == UNREADABLE:
            d["unconfirmed"].append(name)
        elif r["item"] not in SLOTS or int(r["value"], 16):   # 使われていない保存場所（0）は並べない
            d["ok"].append(name)
        if r["changed_at"] and (d["changed_at"] is None or r["changed_at"] > d["changed_at"]):
            d["changed_at"] = r["changed_at"]
        d["checked_at"] = max(d["checked_at"] or "", r["checked_at"] or "")
    return sorted(out.values(), key=lambda d: d["label"])
