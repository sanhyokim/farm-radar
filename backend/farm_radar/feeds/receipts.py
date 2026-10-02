"""預かり証の中身を確かめる（N2c。SPEC 13.1 の5、2026-10-02 オーナー決定）。

値段の記録がないボーナスのコインのうち、決まった形の金庫（ERC-4626）の預かり証を見分ける。
- 見分け方: チェーンの公開の読み取り口で、預かり証の契約に `asset()`（中身のコインの住所）と
  `convertToAssets(1枚)`（1枚あたりの中身の量）を聞く。答えたら金庫の預かり証とみなす。
- 契約の中身が公開・確認済みか: Blockscout（チェーンの登録の blockscout）→ Sourcify の順に調べる。
- 中身がステーブルかどうかは、ここでは決めない（計算の側で、チェーンの登録の stablecoins と比べる）。

読み取りだけ（eth_call と公開の Web の読み取り）。お金を動かすコードはない。1日1回、コインの値段のあとに読む。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .. import ratelimit
from ..config import RpcSettings
from ..rpc.abi import decode_result, encode_call
from ..rpc.client import Endpoint, RpcCallError, RpcClient

log = logging.getLogger(__name__)
SOURCIFY = "https://sourcify.dev/server/v2/contract"
MAX_PER_RUN = 200                     # 1回に確かめる数の上限（読み取り口の回数制限のため）
CALL_GAP_SECONDS = 0.0                # 1つのコインを確かめたあとに待つ時間（呼び出しごとの間隔で足りる）
CALL_INTERVAL_SECONDS = 1.0           # 読み取り口への呼び出しの間隔


def candidates(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], coin_keys: dict[int, str],
               now: datetime) -> list[tuple[int, str]]:
    """確かめるコイン: 登録したチェーンで受け取る、今動いているキャンペーンのボーナスのコインのうち、値段の記録がないもの。"""
    now_s = int(now.timestamp())
    out: set[tuple[int, str]] = set()
    for r in conn.execute("""SELECT DISTINCT distribution_chain_id, lower(reward_address) FROM merkl_campaigns
                             WHERE reward_address IS NOT NULL AND (end_ts IS NULL OR end_ts > ?)
                               AND upper(coalesce(reward_type, 'TOKEN')) = 'TOKEN'""", (now_s,)):
        cid, addr = r[0], r[1]
        if cid not in chains or not addr:
            continue
        key = coin_keys.get(cid)
        if key and conn.execute("SELECT 1 FROM token_prices WHERE coin=? LIMIT 1", (f"{key}:{addr}",)).fetchone():
            continue                  # 値段の記録がある（記録の値動きで計算する）
        out.add((cid, addr))
    return sorted(out)[:MAX_PER_RUN]


def rpc_for(chain: dict[str, Any], env: dict[str, str] | None = None) -> RpcClient:
    """そのチェーンの読み取り専用のクライアント（Alchemy の鍵があれば先に使う → 公式の公開の読み取り口）。

    config.yaml の rpc.extra_urls は Robinhood Chain 用の予備なので、ほかのチェーンには使わない。
    """
    env = os.environ if env is None else env
    eps = []
    key = (env.get("ALCHEMY_API_KEY") or "").strip()
    if key and chain.get("alchemy_rpc"):
        eps.append(Endpoint("alchemy", str(chain["alchemy_rpc"]).replace("{API_KEY}", key)))
    eps.append(Endpoint("public", str(chain["public_rpc"])))
    client = RpcClient(eps, RpcSettings(max_retries=2, backoff_seconds=5.0, timeout_seconds=15.0))
    client.pace_key = str(chain.get("id"))       # type: ignore[attr-defined]  チェーンごとに間隔を数える
    return client


def _call(rpc: RpcClient, to: str, sig: str, types: tuple[str, ...] = (), args: tuple[Any, ...] = (),
          out: tuple[str, ...] = ("uint256",)) -> Any:
    # 公開の読み取り口は回数制限が厳しい（Base は続けて読むと 429。2026-10-02 に確かめた）ので、1秒に1回まで
    ratelimit.wait_turn(f"rpc:{getattr(rpc, 'pace_key', 'rpc')}", CALL_INTERVAL_SECONDS)
    raw = rpc.eth_call(to, "0x" + encode_call(sig, types, args).hex(), "latest")
    if not raw or raw == "0x":
        raise ValueError(f"{sig}: 空の応答")
    return decode_result(out, raw)[0]


def _text(rpc: RpcClient, to: str, sig: str) -> str | None:
    try:
        return str(_call(rpc, to, sig, out=("string",)))[:80]
    except Exception:  # noqa: BLE001  古い形（bytes32）や無い場合は名前なし
        return None


def probe(rpc: RpcClient, address: str, stables: dict[str, str]) -> dict[str, Any]:
    """預かり証かどうかを聞く。答えなければ is_vault=0。"""
    row: dict[str, Any] = {"is_vault": 0}
    try:
        asset = str(_call(rpc, address, "asset()", out=("address",))).lower()
        dec = int(_call(rpc, address, "decimals()", out=("uint8",)))
        assets = int(_call(rpc, address, "convertToAssets(uint256)", ("uint256",), (10 ** dec,)))
        adec = int(_call(rpc, asset, "decimals()", out=("uint8",)))
    except RpcCallError as exc:     # 金庫の形ではない（呼び出しが戻された）
        row["error"] = f"金庫の形ではない: {exc.message}"[:200]
        return row
    except ValueError as exc:
        row["error"] = str(exc)[:200]
        return row
    row.update(is_vault=1, asset=asset, assets_per_share=assets / 10 ** adec, name=_text(rpc, address, "name()"),
               symbol=_text(rpc, address, "symbol()"),
               asset_symbol=stables.get(asset) or _text(rpc, asset, "symbol()"))
    return row


def verified(get_text: Callable[[str], str], chain: dict[str, Any], address: str) -> tuple[int | None, str | None]:
    """契約の中身が公開・確認済みか（Blockscout → Sourcify）。どちらも答えなければ (None, None)。"""
    answered = False
    bs = chain.get("blockscout")
    if bs:
        try:
            d = json.loads(get_text(f"{str(bs).rstrip('/')}/api/v2/smart-contracts/{address}"))
            answered = True
            if d.get("is_verified"):
                return 1, "blockscout"
        except Exception:  # noqa: BLE001  未確認の契約は 404 のことがある
            pass
    try:
        d = json.loads(get_text(f"{SOURCIFY}/{chain['chain_id']}/{address}"))
        answered = True
        if d.get("match") in ("match", "exact_match"):
            return 1, "sourcify"
    except Exception:  # noqa: BLE001
        pass
    return (0, None) if answered else (None, None)


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], coin_keys: dict[int, str],
         stables: dict[int, dict[str, str]], get_text: Callable[[str], str], now: datetime,
         rpc_factory: Callable[[dict[str, Any]], Any] = rpc_for,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """候補を確かめて {"<チェーン番号>:<住所>": 結果} を返す（書き込みは store.write_receipts）。"""
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    for cid, addr in candidates(conn, chains, coin_keys, now):
        rpc = clients.get(cid) or clients.setdefault(cid, rpc_factory(chains[cid]))
        try:
            row = probe(rpc, addr, stables.get(cid, {}))
        except Exception as exc:  # noqa: BLE001  読み取り口の失敗は、そのコインだけあきらめる
            row = {"is_vault": 0, "error": f"読み取りに失敗: {exc}"[:200], "failed": True}
        if row.get("is_vault"):
            row["verified"], row["verified_by"] = verified(get_text, chains[cid], addr)
        out[f"{cid}:{addr}"] = row
        sleep(CALL_GAP_SECONDS)
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    return out
