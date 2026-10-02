"""入れる先の契約が、会場の公式の工場（factory）で作られたものかを確かめる（N4a。venue_match の factory）。

会場の登録（venues/known/<id>.yaml）の工場の契約に、`isXxx(住所)` のような「作ったか」を聞く読み取りをする。
- 確かめるのは、登録したチェーンで今動いている Merkl の入れる先のうち、会場の登録に工場が書いてあるものだけ。
- 答え（作った・作っていない）は変わらないので、一度答えが出たら 30日は読み直さない。読み取り口の失敗は次の回に回す。
- 読み取りの間隔は、読み取り口ごとの回数制限（receipts と同じ）に任せる。1日1回、預かり証のあとに読む。

読み取りだけ（eth_call）。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .. import venue_match
from ..rpc.client import RpcCallError
from .receipts import CALL_GAP_SECONDS, _call, rpc_for

log = logging.getLogger(__name__)
MAX_PER_RUN = 200            # 1回に確かめる数の上限（読み取り口の回数制限のため）
RECHECK_DAYS = 30            # 答えが出たものを読み直すまでの日数


def candidates(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], known: dict[str, dict[str, Any]],
               now: datetime) -> list[dict[str, Any]]:
    """確かめるもの: (チェーン番号, 入れる先の住所, 会場, 工場, 聞き方)。"""
    last = conn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
    if not last:
        return []
    by_proto = venue_match.by_protocol(known)
    since = (now - timedelta(days=RECHECK_DAYS)).isoformat(timespec="seconds")
    done = {(int(r[0]), r[1], r[2]) for r in conn.execute(
        "SELECT chain_id, address, factory FROM venue_checks WHERE checked_at >= ? AND error IS NULL", (since,))}
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for r in conn.execute("""SELECT i.info_json FROM merkl_opportunity_snaps s JOIN feed_items i
                             ON i.source='merkl_opportunities' AND i.key=s.opportunity_id WHERE s.ts=?""", (last,)):
        info = json.loads(r[0] or "{}")
        cid = info.get("chain_id")
        addr = str(info.get("explorer_address") or "").lower()
        v = by_proto.get(str(info.get("protocol") or "").lower())
        if cid not in chains or v is None or not addr.startswith("0x") or len(addr) != 42:
            continue
        for rule in venue_match.factory_targets(v, chains[cid].get("id"), info.get("type")):
            fac = str(rule["factory"]).lower()
            if (int(cid), addr, fac) in done:
                continue
            out[(int(cid), addr, fac)] = {"chain_id": int(cid), "address": addr, "venue_id": v["id"], "factory": fac,
                                          "function": rule["function"]}
    return [out[k] for k in sorted(out)][:MAX_PER_RUN]


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], known: dict[str, dict[str, Any]], now: datetime,
         rpc_factory: Callable[[dict[str, Any]], Any] = rpc_for,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """{"<チェーン番号>:<住所>:<工場>": {venue_id, factory, function, verified, error}} を返す（書くのは store）。"""
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    for c in candidates(conn, chains, known, now):
        rpc = clients.get(c["chain_id"]) or clients.setdefault(c["chain_id"], rpc_factory(chains[c["chain_id"]]))
        row = {k: c[k] for k in ("venue_id", "factory", "function")}
        try:
            row["verified"] = int(bool(_call(rpc, c["factory"], c["function"], ("address",), (c["address"],),
                                             out=("bool",))))
        except RpcCallError as exc:          # 呼び出しが戻された = 工場がその聞き方に答えない（登録の誤り）
            row.update(verified=0, error=f"工場が答えない: {exc.message}"[:200])
        except Exception as exc:  # noqa: BLE001  読み取り口の失敗は、次の回に回す
            row.update(failed=True, error=f"読み取りに失敗: {exc}"[:200])
        out[f"{c['chain_id']}:{c['address']}:{c['factory']}"] = row
        sleep(CALL_GAP_SECONDS)          # 間隔は読み取り口ごとの回数制限（ratelimit）に任せる（receipts と同じ）
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    return out


def latest(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, dict[str, Any]]]:
    """(チェーン番号, 住所) → {工場 → {verified, checked_at}}（答えが出たものだけ）。"""
    out: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='venue_checks'").fetchone():
        return out
    for r in conn.execute("SELECT chain_id, address, factory, verified, checked_at, error FROM venue_checks"):
        if r[3] is None:
            continue
        out.setdefault((int(r[0]), str(r[1]).lower()), {})[str(r[2]).lower()] = {
            "verified": int(r[3]), "checked_at": r[4], "error": r[5]}
    return out
