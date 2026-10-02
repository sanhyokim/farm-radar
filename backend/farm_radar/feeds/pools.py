"""幅に配るプール（Uniswap v3 / v4 の形）の今の状態を読む（N3。SPEC 13.4 の「読み方の型」）。

Merkl の幅に配るキャンペーン（UNISWAP_V3・UNISWAP_V4）のプールについて、チェーンの公開の読み取り口で
今の値段（sqrtPriceX96・tick）と、今の値段のところの流動性（liquidity。幅の中にいる人の合計）を読む。
これで「自分が ±x% の幅に $X 置いたとき、ボーナスの取り分がどれだけか」を、預かり額の割合ではなく
流動性の割合で出せる（今の版の Alandale と同じ考え方。scoring/model.py）。

- 読むのは公式の住所と確かめたものだけ: v4 は Merkl の書く PoolManager が chains/*.yaml の uniswap.v4_pool_manager と同じとき、
  v3 はプールの factory() が uniswap.v3_factory と同じとき。違えば読まずに理由を残す（絶対ルール3: 住所は推測しない）。
- v4 は PoolManager の extsload で、プールの状態の場所（StateLibrary の決まり）から 4 つの欄をまとめて読む（1回の呼び出し）。
- 1秒に1回まで。今の版（18000）の時刻（毎時 0・15・30・45 分）を避け、15分ごとの Merkl のあとに、1時間に1回だけ読む。
読み取りだけ（eth_call）。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from eth_abi import encode
from eth_hash.auto import keccak

from .. import ratelimit
from ..rpc.abi import decode_result, encode_call
from ..rpc.client import RpcCallError, RpcClient
from .receipts import rpc_for

log = logging.getLogger(__name__)

POOL_TYPES = {"UNISWAP_V3": "v3", "UNISWAP_V4": "v4"}   # 読み方がある型（Merkl のキャンペーンの type）
MAX_PER_RUN = 120
CALL_INTERVAL_SECONDS = 1.0

# Uniswap v4-core の StateLibrary（src/libraries/StateLibrary.sol）の決まり:
# プールの状態の場所 = keccak256(abi.encode(poolId, POOLS_SLOT))。そこから slot0 / feeGrowth0 / feeGrowth1 / liquidity の順
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3


def v4_state_slot(pool_id: str) -> bytes:
    pid = bytes.fromhex(pool_id.removeprefix("0x").rjust(64, "0"))
    return keccak(encode(["bytes32", "uint256"], [pid, POOLS_SLOT]))


def _int24(v: int) -> int:
    v &= 0xFFFFFF
    return v - (1 << 24) if v >= 1 << 23 else v


def parse_v4_slot0(word: bytes) -> tuple[int, int, int]:
    """slot0 の1語: 下から sqrtPriceX96（160ビット）・tick（24）・protocolFee（24）・lpFee（24）。"""
    v = int.from_bytes(word, "big")
    return v & ((1 << 160) - 1), _int24(v >> 160), (v >> 208) & 0xFFFFFF


def price_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """token0 1個あたりの token1 の量（桁を調整）。"""
    return (sqrt_price_x96 / 2**96) ** 2 * 10 ** (dec0 - dec1)


def official(chain: dict[str, Any]) -> dict[str, str]:
    """chains/*.yaml の uniswap（公式の住所。出典つき）を小文字で。"""
    u = chain.get("uniswap") or {}
    return {k: str(v).lower() for k, v in u.items() if isinstance(v, str) and v.startswith("0x")}


def candidates(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """読むプール: 登録したチェーンで今動いている、読み方のある型のキャンペーンのプール（重複なし）。"""
    now_s = int(now.timestamp())
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for r in conn.execute("""SELECT chain_id, type, settings_json FROM merkl_campaigns
                             WHERE type IN ('UNISWAP_V3','UNISWAP_V4') AND settings_json IS NOT NULL
                               AND (end_ts IS NULL OR end_ts > ?) AND (start_ts IS NULL OR start_ts <= ?)""",
                          (now_s, now_s + 86400)):
        if r["chain_id"] not in chains:
            continue
        try:
            s = json.loads(r["settings_json"])
        except ValueError:
            continue
        pid = str(s.get("poolId") or "").lower()
        if not pid.startswith("0x") or s.get("decimalsCurrency0") is None or s.get("decimalsCurrency1") is None:
            continue
        out.setdefault((int(r["chain_id"]), pid), {
            "chain_id": int(r["chain_id"]), "pool_id": pid, "kind": POOL_TYPES[r["type"]],
            "manager": str(s.get("poolManager") or "").lower(),
            "dec0": int(s["decimalsCurrency0"]), "dec1": int(s["decimalsCurrency1"])})
    return [out[k] for k in sorted(out)][:MAX_PER_RUN]


def _call(rpc: RpcClient, to: str, data: bytes) -> str:
    ratelimit.wait_turn(f"rpc:{getattr(rpc, 'pace_key', 'rpc')}", CALL_INTERVAL_SECONDS)
    raw = rpc.eth_call(to, "0x" + data.hex(), "latest")
    if not raw or raw == "0x":
        raise ValueError("空の応答")
    return raw


def _words(raw: str) -> list[int]:
    b = bytes.fromhex(raw.removeprefix("0x"))
    return [int.from_bytes(b[i:i + 32], "big") for i in range(0, len(b), 32)]


ZERO = "0x" + "00" * 20


def pool_key_v4(rpc: RpcClient, pool_id: str, addrs: dict[str, str]) -> tuple[str, int] | None:
    """PositionManager.poolKeys で (フックの住所, tick の間隔)。そこで作られていないプールは分からない（None）。"""
    pm = addrs.get("v4_position_manager")
    if not pm:
        return None
    raw = _call(rpc, pm, encode_call("poolKeys(bytes25)", ("bytes25",), (bytes.fromhex(pool_id[2:])[:25],)))
    c0, c1, _fee, spacing, hooks = decode_result(("address", "address", "uint24", "int24", "address"), raw)
    if int(c0, 16) == 0 and int(c1, 16) == 0 and int(spacing) == 0:
        return None
    return str(hooks).lower(), int(spacing)


def read_v4(rpc: RpcClient, p: dict[str, Any], addrs: dict[str, str],
            keys: dict[str, tuple[str, int] | None] | None = None) -> dict[str, Any]:
    if not addrs.get("v4_pool_manager") or p["manager"] != addrs["v4_pool_manager"]:
        return {"official": 0, "error": "PoolManager が公式の住所と確かめられない（読まない）"}
    keys = {} if keys is None else keys
    if p["pool_id"] not in keys:
        keys[p["pool_id"]] = pool_key_v4(rpc, p["pool_id"], addrs)
    raw = _call(rpc, p["manager"], encode_call("extsload(bytes32,uint256)", ("bytes32", "uint256"),
                                               (v4_state_slot(p["pool_id"]), LIQUIDITY_OFFSET + 1)))
    words = decode_result(("bytes32[]",), raw)[0]
    if len(words) < LIQUIDITY_OFFSET + 1:
        raise ValueError("extsload の答えが短い")
    sqrt_p, tick, lp_fee = parse_v4_slot0(words[0])
    if sqrt_p == 0:
        return {"official": 1, "error": "プールが作られていない（値段が 0）"}
    liq = int.from_bytes(words[LIQUIDITY_OFFSET], "big") & ((1 << 128) - 1)
    key = keys.get(p["pool_id"])
    return {"official": 1, "sqrt_price_x96": sqrt_p, "tick": tick, "liquidity": liq, "lp_fee": lp_fee,
            "hooks": key[0] if key else None, "tick_spacing": key[1] if key else None}


def read_v3(rpc: RpcClient, p: dict[str, Any], addrs: dict[str, str], known: dict[str, str]) -> dict[str, Any]:
    factory = known.get(p["pool_id"])
    if factory is None:
        factory = "0x" + _words(_call(rpc, p["pool_id"], encode_call("factory()")))[0].to_bytes(32, "big")[-20:].hex()
        known[p["pool_id"]] = factory
    if not addrs.get("v3_factory") or factory != addrs["v3_factory"]:
        return {"official": 0, "error": "プールの factory が公式の住所と確かめられない（読まない）"}
    w = _words(_call(rpc, p["pool_id"], encode_call("slot0()")))
    liq = _words(_call(rpc, p["pool_id"], encode_call("liquidity()")))[0]
    fee = _words(_call(rpc, p["pool_id"], encode_call("fee()")))[0]
    return {"official": 1, "sqrt_price_x96": w[0] & ((1 << 160) - 1), "tick": _int24(w[1]), "liquidity": liq,
            "lp_fee": int(fee), "hooks": ZERO}


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], now: datetime,
         rpc_factory: Callable[[dict[str, Any]], Any] = rpc_for,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """候補を読んで {"<チェーン番号>:<プール>": 結果} を返す（書き込みは store.write_pool_states）。"""
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    factories: dict[str, str] = {}
    keys = known_keys(conn)                # v4 のフックと tick の間隔（一度読めたら読み直さない）
    for p in candidates(conn, chains, now):
        cid = p["chain_id"]
        rpc = clients.get(cid) or clients.setdefault(cid, rpc_factory(chains[cid]))
        addrs = official(chains[cid])
        try:
            row = read_v4(rpc, p, addrs, keys) if p["kind"] == "v4" else read_v3(rpc, p, addrs, factories)
        except RpcCallError as exc:
            row = {"official": None, "error": f"読み取りが戻された: {exc.message}"[:200]}
        except Exception as exc:  # noqa: BLE001  読み取り口の失敗は、そのプールだけあきらめる（次の回にもう一度）
            row = {"official": None, "error": f"読み取りに失敗: {exc}"[:200], "failed": True}
        if row.get("sqrt_price_x96"):
            row["price"] = price_from_sqrt(row["sqrt_price_x96"], p["dec0"], p["dec1"])
        out[f"{cid}:{p['pool_id']}"] = {**row, "kind": p["kind"]}
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    return out


def known_keys(conn: sqlite3.Connection) -> dict[str, tuple[str, int] | None]:
    """前の回までに読めた v4 のフックと tick の間隔（プール → (フック, 間隔)）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pool_state_snaps)")}
    if "hooks" not in cols:
        return {}
    return {str(r[0]): (str(r[1]), int(r[2])) for r in conn.execute(
        "SELECT pool_id, hooks, tick_spacing FROM pool_state_snaps WHERE kind='v4' AND hooks IS NOT NULL "
        "AND tick_spacing IS NOT NULL GROUP BY pool_id")}


def latest(conn: sqlite3.Connection, max_age_hours: float, now: datetime) -> dict[tuple[int, str], dict[str, Any]]:
    """プールごとの最新の読み取り（max_age_hours より古いものは使わない）。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pool_state_snaps'").fetchone():
        return {}
    since = datetime.fromtimestamp(now.timestamp() - max_age_hours * 3600, tz=now.tzinfo).isoformat(timespec="seconds")
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for r in conn.execute("""SELECT * FROM pool_state_snaps WHERE checked_at >= ? AND liquidity IS NOT NULL
                             ORDER BY checked_at""", (since,)):
        out[(int(r["chain_id"]), str(r["pool_id"]))] = {k: r[k] for k in r.keys()}
    return out
