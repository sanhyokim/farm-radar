"""Merkl の分母 B（幅の中だけ）の検証のための、プールの預け方の歴史（チェーンの記録。2026-10-04 指示書）。

Uniswap v4 では、預け方を作る・足す・減らす・閉じるたびに、PoolManager が ModifyLiquidity という記録を残す
（v4-core の IPoolManager。PoolId・送った契約・下と上の tick・量の増減・salt）。これをプールができたときから全部読むと、
ある時刻の「プールの預け方すべて（幅と量）」を組み立て直せる（merkl_ab.py が使う）。

- 読むのは、Merkl の配った額を記録しているキャンペーンのプールだけ。PoolManager が公式の住所（chains/*.yaml）と同じときだけ。
- 読んだ範囲を覚えて（pool_liq_progress）、同じ記録を読み直さない。途中で止まっても、次の回に続きから読む。
- 記録を始めた時刻（keep_from_ts）より前の記録は、預け方ごとの合計にまとめて（pool_liq_base）、1件ずつは残さない。
  それより後は1件ずつ残す（pool_liq_events）。区切りの途中で量が変わったかを確かめるため。
- 1回に読む回数は MAX_CALLS_PER_RUN まで。1秒に1回まで（ほかの読み手の分も数える）。
  回数制限と言われたら、その回はそこでやめる。1つのプールの失敗で、ほかのプールを止めない。
- 1回に読める範囲は読み取り口ごとに違う（2026-10-04 に確かめた: Robinhood Chain の公開の読み取り口は
  1回 1,000万ブロック・1万件まで。Base の公開の読み取り口は 2,000 ブロックまで）。広すぎると言われたら半分にして読み直す。
- Base は 1回に読める範囲が狭く、プールの始まりから読むと回数が多すぎる（約2秒に1ブロック = 1日 約22回）。
  HISTORY_MAX_CALLS を超えるプールは読まずに、理由を残す（2026-10-04 時点で、Base にこの形のキャンペーンは 0 件）。
- 読み取りだけ（eth_getLogs・eth_getBlockByNumber）。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from eth_hash.auto import keccak

from .. import ratelimit
from ..rpc.abi import topic
from ..rpc.client import RpcCallError, RpcError
from .pools import CALL_INTERVAL_SECONDS, official
from .receipts import rpc_for

log = logging.getLogger(__name__)

MODIFY_LIQUIDITY = topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
LOG_SPAN = {4663: 10_000_000}      # 1回に読むブロックの幅（ほかのチェーンは DEFAULT_SPAN）
DEFAULT_SPAN = 2_000
MAX_CALLS_PER_RUN = 150             # 1回に読む回数（1秒に1回なので 約2分半）
HISTORY_MAX_CALLS = 3_000           # プールの始まりから読むのにこれより多くかかるなら読まない（理由を残す）
REFRESH_MINUTES = 50                # 読み終えたプールは、前に読んでからこれだけたったら続きを読む
KEEP_MARGIN_S = 86_400              # 記録の始まりの1日前から1件ずつ残す
# ブロックの時刻の目印の間隔（約1時間）。その間は比例で出す（Robinhood Chain で 2026-10-04 に確かめた:
# 10万ブロックごとの目印で、比例で出した時刻と本当の時刻のずれは最大 19.5 秒・まん中 4 秒）
ANCHOR_BLOCKS = {4663: 360_000}
DEFAULT_ANCHOR = 1_800


def _signed(word: bytes) -> int:
    v = int.from_bytes(word, "big")
    return v - (1 << 256) if v >= 1 << 255 else v


def position_key(owner: str, tick_lower: int, tick_upper: int, salt: str) -> str:
    """v4-core の Position.calculatePositionKey: keccak256(abi.encodePacked(owner, tickLower, tickUpper, salt))。"""
    raw = (bytes.fromhex(owner.removeprefix("0x")) + tick_lower.to_bytes(3, "big", signed=True)
           + tick_upper.to_bytes(3, "big", signed=True) + bytes.fromhex(salt.removeprefix("0x")))
    return "0x" + keccak(raw).hex()


def parse_log(lg: dict[str, Any]) -> dict[str, Any]:
    """ModifyLiquidity の1件 → 預け方の印・幅・量の増減。"""
    d = bytes.fromhex(str(lg["data"]).removeprefix("0x"))
    if len(d) < 128:
        raise ValueError("ModifyLiquidity の中身が短い")
    owner = "0x" + str(lg["topics"][2])[-40:].lower()
    lower, upper, delta = _signed(d[0:32]), _signed(d[32:64]), _signed(d[64:96])
    salt = "0x" + d[96:128].hex()
    return {"block": int(lg["blockNumber"], 16), "log_index": int(lg["logIndex"], 16),
            # 読み取り口によっては 0 を返す（Robinhood Chain の公開の読み取り口）。そのときは目印のブロックから出す
            "ts": (int(lg["blockTimestamp"], 16) or None) if lg.get("blockTimestamp") else None,
            "owner": owner, "tick_lower": lower, "tick_upper": upper, "salt": salt, "delta": delta,
            "pos_key": position_key(owner, lower, upper, salt)}


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def candidates(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """読むプール: Merkl の配った額を記録している、幅に配る v4 のキャンペーンのプール（重複なし）。"""
    need = ("merkl_campaigns", "merkl_reward_snaps")
    if not all(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() for t in need):
        return []
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for r in conn.execute("""SELECT c.chain_id, c.settings_json, MIN(t.ts) AS first_ts FROM merkl_campaigns c
                             JOIN merkl_reward_snaps t ON t.campaign_id = c.campaign_id
                             WHERE c.type='UNISWAP_V4' AND c.settings_json LIKE '%weightFees%'
                             GROUP BY c.campaign_id"""):
        cid = int(r["chain_id"])
        if cid not in chains:
            continue
        try:
            s = json.loads(r["settings_json"] or "{}")
        except ValueError:
            continue
        pid = str(s.get("poolId") or "").lower()
        if not pid.startswith("0x") or len(pid) != 66:
            continue
        first = _ts(r["first_ts"]) - KEEP_MARGIN_S
        key = (cid, pid)
        if key in out:
            out[key]["keep_from_ts"] = min(out[key]["keep_from_ts"], first)
            continue
        out[key] = {"chain_id": cid, "pool_id": pid, "manager": str(s.get("poolManager") or "").lower(),
                    "keep_from_ts": first}
    return [out[k] for k in sorted(out)]


def _progress(conn: sqlite3.Connection, p: dict[str, Any]) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO pool_liq_progress(chain_id, pool_id, manager, keep_from_ts) VALUES (?,?,?,?)",
                 (p["chain_id"], p["pool_id"], p["manager"], p["keep_from_ts"]))
    return conn.execute("SELECT * FROM pool_liq_progress WHERE chain_id=? AND pool_id=?",
                        (p["chain_id"], p["pool_id"])).fetchone()


def _note(conn: sqlite3.Connection, p: dict[str, Any], error: str | None, now: datetime) -> None:
    conn.execute("UPDATE pool_liq_progress SET error=?, updated_at=? WHERE chain_id=? AND pool_id=?",
                 (error, now.isoformat(timespec="seconds"), p["chain_id"], p["pool_id"]))
    conn.commit()


class _Stop(Exception):
    """回数制限: この回はここでやめる（読めたところまでは保存してある）。"""


class Reader:
    """1つのチェーンの読み取り（回数を数える・回数制限で止める・ブロックの時刻の目印を覚える）。"""

    def __init__(self, conn: sqlite3.Connection, chain_id: int, rpc: Any, budget: int, sleep: Callable[[float], None]):
        self.conn, self.chain_id, self.rpc, self.budget, self.sleep, self.calls = conn, chain_id, rpc, budget, sleep, 0
        self.grid = ANCHOR_BLOCKS.get(chain_id, DEFAULT_ANCHOR)
        self.head: tuple[int, int] | None = None
        self._ts: dict[int, int] = {}

    def _req(self, method: str, params: list[Any]) -> Any:
        if self.budget <= 0:
            raise _Stop("この回に読む回数を使い切った")
        self.budget -= 1
        self.calls += 1
        ratelimit.wait_turn(f"rpc:{getattr(self.rpc, 'pace_key', 'rpc')}", CALL_INTERVAL_SECONDS, sleep=self.sleep)
        try:
            return self.rpc.request(method, params)
        except RpcCallError as exc:
            if exc.code in (429, -32005) or "too many" in exc.message.lower():
                raise _Stop(f"回数制限: {exc.message}"[:200]) from exc
            raise

    def _save(self, block: int, ts: int) -> None:
        self._ts[block] = ts
        self.conn.execute("INSERT OR IGNORE INTO chain_block_times(chain_id, block, ts) VALUES (?,?,?)",
                          (self.chain_id, block, ts))

    def read_head(self) -> tuple[int, int]:
        b = self._req("eth_getBlockByNumber", ["latest", False])
        self.head = (int(b["number"], 16), int(b["timestamp"], 16))
        self._save(*self.head)
        self.conn.commit()
        return self.head

    def block_ts(self, n: int) -> int:
        """ブロック n の本当の時刻（覚えていなければ読む）。"""
        if n not in self._ts:
            r = self.conn.execute("SELECT ts FROM chain_block_times WHERE chain_id=? AND block=?",
                                  (self.chain_id, n)).fetchone()
            if r:
                self._ts[n] = int(r[0])
            else:
                b = self._req("eth_getBlockByNumber", [hex(n), False])
                self._save(n, int(b["timestamp"], 16))
        return self._ts[n]

    def time_of(self, n: int) -> int:
        """ブロック n の時刻。前後の目印（約1時間ごと）のブロックの本当の時刻から比例で出す。"""
        assert self.head is not None
        g0 = n - n % self.grid
        g1 = min(g0 + self.grid, self.head[0])
        if n in (g0, g1) or g1 <= g0:
            return self.block_ts(n)
        t0, t1 = self.block_ts(g0), self.block_ts(g1)
        return round(t0 + (t1 - t0) * (n - g0) / (g1 - g0))

    def block_at(self, ts: int) -> int:
        """時刻 ts より後の最初のブロック（二分探索。約27回。プールごとに1回だけ）。"""
        assert self.head is not None
        lo, hi = 0, self.head[0]
        if self.head[1] <= ts:
            return hi + 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.block_ts(mid) <= ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def logs(self, manager: str, pool_id: str, lo: int, hi: int) -> tuple[list[dict[str, Any]], int]:
        """[lo, hi] の記録。広すぎる・多すぎると言われたら範囲を半分にする。戻り値 = (記録, 読めた範囲の終わり)。"""
        while True:
            try:
                res = self._req("eth_getLogs", [{"address": manager, "fromBlock": hex(lo), "toBlock": hex(hi),
                                                 "topics": [MODIFY_LIQUIDITY, pool_id]}])
                return list(res or []), hi
            except (RpcCallError, RpcError) as exc:
                # 読み取り口がまったく答えないとき（RpcError）は、狭くしても同じなので早めにあきらめる
                if hi <= lo or (isinstance(exc, RpcError) and hi - lo < 1_000):
                    raise
                log.info("pool history: narrowing", extra={"data": {"pool": pool_id, "lo": lo, "hi": hi,
                                                                    "error": str(exc)[:120]}})
                hi = lo + (hi - lo) // 2


def _load_base(conn: sqlite3.Connection, cid: int, pid: str) -> dict[str, dict[str, Any]]:
    return {r["pos_key"]: {"owner": r["owner"], "tick_lower": r["tick_lower"], "tick_upper": r["tick_upper"],
                           "salt": r["salt"], "liquidity": int(r["liquidity"])}
            for r in conn.execute("SELECT * FROM pool_liq_base WHERE chain_id=? AND pool_id=?", (cid, pid))}


def _write_chunk(conn: sqlite3.Connection, p: dict[str, Any], keep_block: int, base: dict[str, dict[str, Any]],
                 evs: list[dict[str, Any]], next_block: int, done_ts: int | None, calls: int, now: datetime) -> None:
    """1回分の記録を、読んだ範囲と一緒に書く（全部書けるか、何も書かないか）。"""
    cid, pid = p["chain_id"], p["pool_id"]
    changed: set[str] = set()
    try:
        for e in evs:
            if e["block"] < keep_block:
                b = base.setdefault(e["pos_key"], {"owner": e["owner"], "tick_lower": e["tick_lower"],
                                                   "tick_upper": e["tick_upper"], "salt": e["salt"], "liquidity": 0})
                b["liquidity"] += e["delta"]
                changed.add(e["pos_key"])
            else:
                conn.execute("INSERT OR IGNORE INTO pool_liq_events(chain_id, pool_id, block, log_index, ts, pos_key, owner, "
                             "tick_lower, tick_upper, salt, delta) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (cid, pid, e["block"], e["log_index"], e["ts"], e["pos_key"], e["owner"], e["tick_lower"],
                              e["tick_upper"], e["salt"], str(e["delta"])))
        for k in changed:
            b = base[k]
            if b["liquidity"] == 0:
                conn.execute("DELETE FROM pool_liq_base WHERE chain_id=? AND pool_id=? AND pos_key=?", (cid, pid, k))
                del base[k]
            else:
                conn.execute("INSERT OR REPLACE INTO pool_liq_base(chain_id, pool_id, pos_key, owner, tick_lower, tick_upper, "
                             "salt, liquidity) VALUES (?,?,?,?,?,?,?,?)",
                             (cid, pid, k, b["owner"], b["tick_lower"], b["tick_upper"], b["salt"], str(b["liquidity"])))
        conn.execute("UPDATE pool_liq_progress SET next_block=?, done_ts=COALESCE(?, done_ts), events=events+?, "
                     "calls=calls+?, error=NULL, updated_at=? WHERE chain_id=? AND pool_id=?",
                     (next_block, done_ts, len(evs), calls, now.isoformat(timespec="seconds"), cid, pid))
        conn.commit()
    except Exception:
        conn.rollback()
        # 書けなかった分の合計は、表から読み直す（次の回にこの範囲をもう一度読む）
        base.clear()
        base.update(_load_base(conn, cid, pid))
        raise


def read_pool(conn: sqlite3.Connection, reader: Reader, p: dict[str, Any], now: datetime) -> dict[str, Any]:
    """1つのプールを、読めたところから今のブロックまで（回数の残りの分だけ）読む。"""
    cid, pid = p["chain_id"], p["pool_id"]
    prog = _progress(conn, p)
    span = LOG_SPAN.get(cid, DEFAULT_SPAN)
    assert reader.head is not None
    lo, (top, top_ts) = int(prog["next_block"]), reader.head
    if prog["done_ts"] is None and (top - lo) / span > HISTORY_MAX_CALLS:
        err = (f"読む量が多すぎるので読まない（プールの始まりから 約{(top - lo) // span:,} 回。"
               f"1回 {span:,} ブロックまでの読み取り口）")
        _note(conn, p, err, now)
        return {"error": err, "events": 0, "calls": 0, "caught_up": False}
    keep_block = prog["keep_from_block"]
    calls0 = reader.calls
    if keep_block is None:
        keep_block = reader.block_at(int(prog["keep_from_ts"]))
        conn.execute("UPDATE pool_liq_progress SET keep_from_block=? WHERE chain_id=? AND pool_id=?", (keep_block, cid, pid))
        conn.commit()
    base = _load_base(conn, cid, pid)
    n_ev, cur = 0, span
    while lo <= top:
        hi = min(top, lo + cur - 1)
        before = reader.calls
        logs, hi = reader.logs(p["manager"], pid, lo, hi)
        if hi - lo + 1 < cur:                   # 狭くして読めたら、次は少しずつ広げる
            cur = min(span, (hi - lo + 1) * 2)
        evs = []
        for lg in logs:
            if lg.get("removed"):
                continue
            e = parse_log(lg)
            if e["block"] >= keep_block and e["ts"] is None:
                e["ts"] = reader.time_of(e["block"])
            evs.append(e)
        evs.sort(key=lambda e: (e["block"], e["log_index"]))
        done = top_ts if hi == top else None
        _write_chunk(conn, p, keep_block, base, evs, hi + 1, done, reader.calls - before, now)
        n_ev += len(evs)
        lo = hi + 1
    return {"events": n_ev, "calls": reader.calls - calls0, "caught_up": True, "error": None}


def due(row: sqlite3.Row | None, now: datetime) -> bool:
    if row is None or row["done_ts"] is None:
        return True
    return now.timestamp() - int(row["done_ts"]) >= REFRESH_MINUTES * 60


def read(conn: sqlite3.Connection, chains: dict[int, dict[str, Any]], now: datetime,
         rpc_factory: Callable[[dict[str, Any]], Any] | None = None,
         sleep: Callable[[float], None] = time.sleep, budget: int = MAX_CALLS_PER_RUN) -> dict[str, dict[str, Any]]:
    """読むべきプールを、回数の残りの分だけ読む。{"<チェーン番号>:<プール>": 結果} を返す（書き込みは読みながら済ませる）。"""
    factory = rpc_factory or (lambda c: rpc_for(c, cache_size=0))
    out: dict[str, dict[str, Any]] = {}
    clients: dict[int, Any] = {}
    readers: dict[int, Reader] = {}
    left = budget
    stopped: str | None = None
    targets = candidates(conn, chains)
    events0 = _events_total(conn)
    for p in targets:
        cid, key = p["chain_id"], f"{p['chain_id']}:{p['pool_id']}"
        addrs = official(chains[cid])
        if not addrs.get("v4_pool_manager") or p["manager"] != addrs["v4_pool_manager"]:
            out[key] = {"error": "PoolManager が公式の住所と確かめられない（読まない）", "events": 0, "calls": 0}
            continue
        row = conn.execute("SELECT * FROM pool_liq_progress WHERE chain_id=? AND pool_id=?", (cid, p["pool_id"])).fetchone()
        if stopped or not due(row, now):
            out[key] = {"skipped": stopped or "読み終えている（次の回に続きを読む）", "events": 0, "calls": 0}
            continue
        if cid not in readers:
            clients[cid] = factory(chains[cid])
            readers[cid] = Reader(conn, cid, clients[cid], left, sleep)
        reader = readers[cid]
        reader.budget = left
        try:
            if reader.head is None:
                reader.read_head()
            out[key] = read_pool(conn, reader, p, now)
        except _Stop as exc:
            stopped = str(exc)
            _note(conn, p, None, now)
            out[key] = {"skipped": stopped, "events": 0, "calls": 0}
        except Exception as exc:  # noqa: BLE001  そのプールだけあきらめる（次の回に、読めたところから）
            err = f"読み取りに失敗: {type(exc).__name__}: {exc}"[:200]
            _note(conn, p, err, now)
            out[key] = {"error": err, "events": 0, "calls": 0}
        left = reader.budget
    for rpc in clients.values():
        close = getattr(rpc, "close", None)
        if close:
            close()
    # この回の量を1行残す（パソコンの1行の更新のまとめで「今回」を出すため）。途中でやめたプールの分も数える
    conn.execute("INSERT OR REPLACE INTO pool_liq_runs(ts, targets, calls, events, pools_read, errors, stopped) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (now.isoformat(timespec="seconds"), len(targets), sum(r.calls for r in readers.values()),
                  _events_total(conn) - events0, sum(1 for r in out.values() if "caught_up" in r and not r.get("error")),
                  sum(1 for r in out.values() if r.get("error")), stopped))
    conn.commit()
    return out


def _events_total(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(events), 0) FROM pool_liq_progress").fetchone()[0])


def status(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """読み取りの進み具合（画面の「読んだ量」と、パソコンの1行の更新のまとめに使う）。

    - done: 今のブロックまで一度は読み終えたプール。partial: 途中のプール（読めたところ next_block を覚えていて、次の回に続きから）。
    - not_started: 対象なのに、まだ1回も読んでいないプール（最後の回の対象の数から数える）。
    - last_run: 最後の回の量（回数・読めた記録・読んだプール・読めなかったプール・途中でやめた理由）。"""
    empty = {"pools": 0, "caught_up": 0, "events": 0, "calls": 0, "stored_events": 0, "base_positions": 0, "errors": [],
             "done": 0, "partial": 0, "resumable": 0, "not_started": 0, "targets": None, "last_run": None, "runs": 0}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pool_liq_progress'").fetchone():
        return empty
    rows = conn.execute("SELECT * FROM pool_liq_progress").fetchall()
    t = (now or datetime.now(UTC)).timestamp()
    last, runs = None, 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pool_liq_runs'").fetchone():
        r = conn.execute("SELECT * FROM pool_liq_runs ORDER BY ts DESC LIMIT 1").fetchone()
        last = None if r is None else {k: r[k] for k in ("ts", "targets", "calls", "events", "pools_read", "errors",
                                                          "stopped")}
        runs = conn.execute("SELECT COUNT(*) FROM pool_liq_runs").fetchone()[0]
    partial = [r for r in rows if r["done_ts"] is None]
    targets = last["targets"] if last else None
    return {**empty, "pools": len(rows),
            "caught_up": sum(1 for r in rows if r["done_ts"] is not None and t - int(r["done_ts"]) < 6 * 3600),
            "events": sum(int(r["events"]) for r in rows), "calls": sum(int(r["calls"]) for r in rows),
            "stored_events": conn.execute("SELECT COUNT(*) FROM pool_liq_events").fetchone()[0],
            "base_positions": conn.execute("SELECT COUNT(*) FROM pool_liq_base").fetchone()[0],
            "errors": [{"pool_id": r["pool_id"], "error": r["error"]} for r in rows if r["error"]],
            "done": len(rows) - len(partial), "partial": len(partial),
            "resumable": sum(1 for r in partial if int(r["next_block"]) > 0 or r["keep_from_block"] is not None),
            "not_started": max(0, targets - len(rows)) if targets is not None else 0,
            "targets": targets, "last_run": last, "runs": runs}


__all__ = ["read", "status", "parse_log", "position_key", "candidates", "MODIFY_LIQUIDITY"]
