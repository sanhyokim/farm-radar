"""金庫の運用先の見張り（N4b。2026-10-03 オーナー: Hyperdrive の金庫は運営が待ち時間なしで運用先を変えられる）。

会場の登録（venues/known/<id>.yaml）で `watch: morpho_vault_v2` を付けた見分け方の金庫について、
運用先（adapter）の一覧・すぐ引き出せる運用先（liquidityAdapter）・運用者（curator）・持ち主（owner）を読み、
前の回と違えば「変わった」として記録する（vault_changes）。練習の記録は、入っていた間に変わったかをここから引く。

- 関数の名前は Morpho の公開ソース（morpho-org/vault-v2 の IVaultV2.sol: adaptersLength・adapters・liquidityAdapter・
  curator・owner）。どれも読むだけの関数。
- 1時間に1回（pool_states と同じ間隔）。読み取りは1秒に1回まで（receipts と同じ読み取り口の決まり）。
読み取りだけ（eth_call）。お金を動かすコードはない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from ..rpc.client import RpcCallError
from .receipts import _call, rpc_for

log = logging.getLogger(__name__)
KIND = "morpho_vault_v2"
MAX_ADAPTERS = 30            # 運用先の数の上限（これより多ければ「多すぎる」として記録する）


def targets(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], known: dict[str, dict[str, Any]]
            ) -> list[dict[str, Any]]:
    """見張る金庫: (チェーン番号, 住所, 会場)。公式の住所の一覧か、工場が「作った」と答えた金庫だけ。"""
    by_chain = {c.get("id"): cid for cid, c in chains.items()}
    out: dict[tuple[int, str], dict[str, Any]] = {}
    checked: dict[str, list[tuple[int, str]]] = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='venue_checks'").fetchone():
        for r in conn.execute("SELECT chain_id, address, factory FROM venue_checks WHERE verified=1"):
            checked.setdefault(str(r[2]).lower(), []).append((int(r[0]), str(r[1]).lower()))
    for v in known.values():
        for chain, rules in (v.get("match") or {}).items():
            cid = by_chain.get(chain)
            if cid is None:
                continue
            for rule in rules or []:
                if rule.get("watch") != KIND or rule.get("unverified"):
                    continue
                if rule.get("method") == "addresses":
                    addrs = [(cid, str(a).lower()) for a in rule.get("addresses") or []]
                elif rule.get("method") == "factory":
                    addrs = [x for x in checked.get(str(rule.get("factory")).lower(), []) if x[0] == cid]
                else:
                    addrs = []
                for c, a in addrs:
                    out[(c, a)] = {"chain_id": c, "address": a, "venue_id": v["id"]}
    return [out[k] for k in sorted(out)]


def probe(rpc: Any, address: str) -> dict[str, Any]:
    n = int(_call(rpc, address, "adaptersLength()"))
    adapters = [str(_call(rpc, address, "adapters(uint256)", ("uint256",), (i,), out=("address",))).lower()
                for i in range(min(n, MAX_ADAPTERS))]
    return {"adapters": sorted(adapters), "adapters_count": n,
            "liquidity_adapter": str(_call(rpc, address, "liquidityAdapter()", out=("address",))).lower(),
            "curator": str(_call(rpc, address, "curator()", out=("address",))).lower(),
            "owner": str(_call(rpc, address, "owner()", out=("address",))).lower()}


def digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], known: dict[str, dict[str, Any]],
         rpc_factory: Callable[[dict[str, Any]], Any] = rpc_for) -> dict[str, dict[str, Any]]:
    """{"<チェーン番号>:<住所>": {venue_id, kind, state, digest} または {failed, error}}。"""
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    for t in targets(conn, chains, known):
        rpc = clients.get(t["chain_id"]) or clients.setdefault(t["chain_id"], rpc_factory(chains[t["chain_id"]]))
        key = f"{t['chain_id']}:{t['address']}"
        try:
            state = probe(rpc, t["address"])
            out[key] = {"venue_id": t["venue_id"], "kind": KIND, "state": state, "digest": digest(state)}
        except RpcCallError as exc:          # 金庫の形が違う（登録の誤り）。記録して次へ
            out[key] = {"venue_id": t["venue_id"], "kind": KIND, "state": None, "digest": None,
                        "error": f"金庫が答えない: {exc.message}"[:200]}
        except Exception as exc:  # noqa: BLE001  読み取り口の失敗は、次の回に回す
            out[key] = {"failed": True, "error": f"読み取りに失敗: {exc}"[:200]}
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    return out


def changes_between(conn: sqlite3.Connection, chain_id: int, address: str, start: str, end: str | None = None
                    ) -> list[dict[str, Any]]:
    """入っていた間（start〜end。ISO の時刻）に運用先が変わった記録（練習の記録に付ける）。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_changes'").fetchone():
        return []
    q = "SELECT detected_at, before_json, after_json FROM vault_changes WHERE chain_id=? AND address=? AND detected_at>=?"
    args: list[Any] = [int(chain_id), address.lower(), start]
    if end:
        q += " AND detected_at<=?"
        args.append(end)
    return [{"detected_at": r[0], "before": json.loads(r[1] or "null"), "after": json.loads(r[2] or "null")}
            for r in conn.execute(q + " ORDER BY detected_at", args)]


def latest(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    """(チェーン番号, 住所) → {checked_at, state, error, changes（最近30日の変化の数）, last_change}。"""
    out: dict[tuple[int, str], dict[str, Any]] = {}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_states'").fetchone():
        return out
    for r in conn.execute("SELECT chain_id, address, checked_at, state_json, error FROM vault_states"):
        out[(int(r[0]), str(r[1]))] = {"checked_at": r[2], "state": json.loads(r[3] or "null"), "error": r[4],
                                       "changes": 0, "last_change": None}
    for r in conn.execute("""SELECT chain_id, address, COUNT(*), MAX(detected_at) FROM vault_changes
                             WHERE detected_at >= datetime('now', '-30 days') GROUP BY chain_id, address"""):
        if (int(r[0]), str(r[1])) in out:
            out[(int(r[0]), str(r[1]))].update(changes=int(r[2]), last_change=r[3])
    return out
