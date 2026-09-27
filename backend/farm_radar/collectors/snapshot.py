"""プールのスナップショット収集（15分ごと）。

1回の収集では、最初にブロック番号を1つ決め、すべての読み取りをそのブロックで行う。
こうすると、同じ回のデータがすべて同じ時点のものになる。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..adapters.base import AdapterNotReady, RawCall, VenueAdapter
from ..db import database as db
from ..rpc.client import RpcClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    run_id: int
    status: str
    block_number: int | None
    pools_ok: int
    pools_failed: int
    error: str | None = None


def slot_for(now: datetime, minutes: int) -> datetime:
    """予定時刻（15分刻みなど）に切り捨てる。"""
    now = now.astimezone(UTC).replace(second=0, microsecond=0)
    return now - timedelta(minutes=now.minute % minutes) if 60 % minutes == 0 else now


def collect_venue(
    conn: sqlite3.Connection,
    adapter: VenueAdapter,
    rpc: RpcClient,
    *,
    snapshot_minutes: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunResult:
    started = now()
    slot = slot_for(started, snapshot_minutes)
    _record_gap_if_any(conn, adapter.venue_id, slot, snapshot_minutes, started)
    run_id = db.start_run(conn, adapter.venue_id, slot, started)
    block: int | None = None
    ok = failed = 0
    try:
        block = rpc.block_number()
        pools = adapter.list_pools(block)
        for pool in pools:
            db.upsert_pool(conn, pool, started)
        conn.commit()

        for pool in pools:
            ts = now()
            try:
                state = adapter.pool_state(pool, block)
            except Exception as exc:
                failed += 1
                log.warning("pool_state failed", extra={"data": {"pool": pool.pool_id, "error": str(exc)}})
                continue
            _save_raw(conn, run_id, ts, block, pool.pool_id, state.raw)

            rewards = None
            try:
                rewards = adapter.gauge_rewards(pool, block)
                _save_raw(conn, run_id, ts, block, pool.pool_id, rewards.raw)
            except Exception as exc:
                log.warning("gauge_rewards failed", extra={"data": {"pool": pool.pool_id, "error": str(exc)}})

            db.insert_snapshot(conn, {
                "pool_id": pool.pool_id,
                "ts": ts.isoformat(timespec="seconds"),
                "block_number": block,
                "run_id": run_id,
                "price": state.price,
                "tick": state.tick,
                "sqrt_price_x96": str(state.sqrt_price_x96),
                "fee": state.fee,
                "liquidity_total": str(state.liquidity_total),
                "liquidity_staked_inrange": _str_or_none(state.liquidity_staked_inrange),
                "reward_rate_raw": _str_or_none(rewards.reward_rate_raw) if rewards else None,
                "reward_token": rewards.reward_token if rewards else None,
                "epoch_end": rewards.epoch_end.isoformat() if rewards and rewards.epoch_end else None,
                "source": f"rpc:{rpc.last_endpoint}",
            })
            conn.commit()
            if rewards is None:
                failed += 1
            else:
                ok += 1

        status = "ok" if failed == 0 else ("partial" if ok else "failed")
        result = RunResult(run_id, status, block, ok, failed)
    except AdapterNotReady as exc:
        result = RunResult(run_id, "skipped", block, ok, failed, str(exc))
    except Exception as exc:
        log.exception("collection failed", extra={"data": {"venue": adapter.venue_id}})
        result = RunResult(run_id, "failed", block, ok, failed, str(exc))

    db.finish_run(conn, run_id, now=now(), status=result.status, block_number=result.block_number,
                  pools_ok=result.pools_ok, pools_failed=result.pools_failed, error=result.error)
    log.info("collection finished", extra={"data": {
        "venue": adapter.venue_id, "status": result.status, "block": result.block_number,
        "pools_ok": result.pools_ok, "pools_failed": result.pools_failed, "error": result.error,
    }})
    return result


def record_failed_run(conn: sqlite3.Connection, venue_id: str, snapshot_minutes: int, error: str,
                      now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
    """収集に入る前に失敗したとき（RPCにつながらないなど）の記録。"""
    started = now()
    slot = slot_for(started, snapshot_minutes)
    _record_gap_if_any(conn, venue_id, slot, snapshot_minutes, started)
    run_id = db.start_run(conn, venue_id, slot, started)
    db.finish_run(conn, run_id, now=now(), status="failed", block_number=None, pools_ok=0, pools_failed=0, error=error)
    log.error("collection failed before start", extra={"data": {"venue": venue_id, "error": error}})


def _record_gap_if_any(conn: sqlite3.Connection, venue_id: str, slot: datetime, minutes: int,
                       now: datetime) -> None:
    """前回の収集から予定時刻が飛んでいたら、その間を「欠損」として記録する。"""
    last = db.last_slot_before(conn, venue_id, slot)
    if last is None:
        return
    step = timedelta(minutes=minutes)
    missed = int((slot - last) / step) - 1
    if missed <= 0:
        return
    start, end = last + step, slot - step
    db.record_gap(conn, venue_id, start, end, missed, now)
    log.warning("collection gap detected", extra={"data": {
        "venue": venue_id, "start": start.isoformat(), "end": end.isoformat(), "missed_slots": missed,
    }})


def _save_raw(conn: sqlite3.Connection, run_id: int, ts: datetime, block: int, pool_id: str,
              raw: tuple[RawCall, ...]) -> None:
    for r in raw:
        db.insert_raw(conn, run_id=run_id, ts=ts, block_number=block, pool_id=pool_id,
                      kind=r.kind, request=r.request, response=r.response)


def _str_or_none(v: int | None) -> str | None:
    return None if v is None else str(v)
