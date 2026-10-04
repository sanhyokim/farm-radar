"""Merkl の答え合わせ（N5c。docs/n5-plan-2026-10-03.md の 2-3）のための、預け方1つごとの幅と量を読む。

Merkl の配った額（merkl_rewards）は、預け方ごと（reason = "UNISWAP_V4_<プール>_<番号>"）に記録している。
その預け方の幅（下と上の tick）と量（流動性）を、チェーンの公開の読み取り口で読む。
これと1時間ごとのプールの状態（pool_states）で、「全員を分母」の見込みと実際に配られた額を比べる（feeds 外の merkl_check）。

- 読むのは Uniswap v4 の、公式の PositionManager（chains/*.yaml の uniswap.v4_position_manager）で作られた預け方だけ。
  読んだ預け方のプールが、キャンペーンのプールと同じか確かめる（違えば使わない）。v3 は公式の住所が登録にないので、まだ読まない。
- 幅は預け方を作ったときに決まり変わらないので、1回読めたら読み直さない。量（流動性）は毎回読む（足したり減らしたりできる）。
- 1秒に1回まで。1回に読む預け方は MAX_PER_RUN まで（配った額の多い順）。読み取りだけ（eth_call）。お金を動かすコードはない。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .. import ratelimit
from ..rpc.abi import decode_result, encode_call
from ..rpc.client import RpcCallError, RpcClient
from .pools import CALL_INTERVAL_SECONDS, _int24, official
from .receipts import rpc_for

log = logging.getLogger(__name__)

MAX_PER_RUN = 120            # 1回に読む預け方（1つあたり 1〜2 回の呼び出し。1秒に1回なので数分）
PER_CAMPAIGN = 15            # キャンペーンごとに、配った額の多い順にこの数まで
REASON = re.compile(r"^UNISWAP_V4_(0x[0-9a-fA-F]{64})_(\d+)$")


def parse_reason(reason: str) -> tuple[str, int] | None:
    """"UNISWAP_V4_<プール>_<番号>" → (プール（小文字）, 預け方の番号)。ほかの形は None。"""
    m = REASON.match(reason or "")
    return (m.group(1).lower(), int(m.group(2))) if m else None


def parse_info(info: int) -> tuple[str, int, int]:
    """PositionInfo（v4-periphery の PositionInfoLibrary）: 上から poolId の先頭25バイト・tickUpper・tickLower・印8ビット。"""
    return "0x" + format(info >> 56, "050x"), _int24(info >> 32), _int24(info >> 8)


def candidates(conn: sqlite3.Connection, chain_ids: set[int], now: datetime) -> list[dict[str, Any]]:
    """読む預け方: 今動いている（終わって1日以内も）幅に配る v4 のキャンペーンの、配った額の多い順の預け方。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='merkl_reward_latest'").fetchone():
        return []
    now_s = int(now.timestamp())
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for c in conn.execute("""SELECT campaign_id, chain_id FROM merkl_campaigns WHERE type='UNISWAP_V4'
                             AND settings_json LIKE '%weightFees%' AND (start_ts IS NULL OR start_ts <= ?)
                             AND (end_ts IS NULL OR end_ts > ?) ORDER BY daily_rewards DESC""",
                          (now_s, now_s - 86400)):
        if c["chain_id"] not in chain_ids:
            continue
        rows = conn.execute("SELECT reason, amount_raw FROM merkl_reward_latest WHERE campaign_id=?",
                            (c["campaign_id"],)).fetchall()
        rows = sorted(rows, key=lambda r: -int(r["amount_raw"] or 0))[:PER_CAMPAIGN]
        for r in rows:
            p = parse_reason(r["reason"])
            if p is None:
                continue
            out.setdefault((int(c["chain_id"]), p[1]), {"chain_id": int(c["chain_id"]), "token_id": p[1], "pool_id": p[0]})
    return list(out.values())[:MAX_PER_RUN]


def known_ranges(conn: sqlite3.Connection) -> dict[tuple[int, int], tuple[str, int, int]]:
    """前の回までに読めた幅（(チェーン, 番号) → (プール, 下, 上)）。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='merkl_position_snaps'").fetchone():
        return {}
    return {(int(r[0]), int(r[1])): (str(r[2]), int(r[3]), int(r[4])) for r in conn.execute(
        "SELECT chain_id, token_id, pool_id, tick_lower, tick_upper FROM merkl_position_snaps "
        "WHERE tick_lower IS NOT NULL GROUP BY chain_id, token_id")}


def _call(rpc: RpcClient, to: str, sig: str, out: tuple[str, ...], token_id: int) -> tuple[Any, ...]:
    ratelimit.wait_turn(f"rpc:{getattr(rpc, 'pace_key', 'rpc')}", CALL_INTERVAL_SECONDS)
    raw = rpc.eth_call(to, "0x" + encode_call(sig, ("uint256",), (token_id,)).hex(), "latest")
    if not raw or raw == "0x":
        raise ValueError(f"{sig}: 空の応答")
    return decode_result(out, raw)


def read_one(rpc: RpcClient, pm: str, p: dict[str, Any], known: tuple[str, int, int] | None) -> dict[str, Any]:
    if known is None:
        res = _call(rpc, pm, "getPoolAndPositionInfo(uint256)",
                    ("address", "address", "uint24", "int24", "address", "uint256"), p["token_id"])
        pid25, upper, lower = parse_info(int(res[5]))
        known = (pid25, lower, upper)
    pid25, lower, upper = known
    if not p["pool_id"].startswith(pid25):
        return {"error": "預け方のプールがキャンペーンのプールと違う（使わない）", "tick_lower": lower, "tick_upper": upper}
    liq = _call(rpc, pm, "getPositionLiquidity(uint256)", ("uint128",), p["token_id"])[0]
    return {"tick_lower": lower, "tick_upper": upper, "liquidity": int(liq)}


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], now: datetime,
         rpc_factory: Callable[[dict[str, Any]], Any] = rpc_for,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """候補を読んで {"<チェーン番号>:<番号>": 結果} を返す（書き込みは store.write_merkl_positions）。"""
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    known = known_ranges(conn)
    for p in candidates(conn, set(chains), now):
        cid = p["chain_id"]
        pm = official(chains[cid]).get("v4_position_manager")
        if not pm:
            continue
        rpc = clients.get(cid) or clients.setdefault(cid, rpc_factory(chains[cid]))
        try:
            row = read_one(rpc, pm, p, known.get((cid, p["token_id"])))
        except RpcCallError as exc:
            row = {"error": f"読み取りが戻された: {exc.message}"[:200]}
        except Exception as exc:  # noqa: BLE001  その預け方だけあきらめる（次の回にもう一度）
            row = {"error": f"読み取りに失敗: {exc}"[:200]}
        out[f"{cid}:{p['token_id']}"] = {**row, "chain_id": cid, "token_id": p["token_id"], "pool_id": p["pool_id"]}
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    return out
