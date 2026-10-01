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
    epoch_fresh_minutes: int = 120,
    reward_drop_alert_pct: float = 30.0,
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

        # まとめ読みができるアダプターなら、先に全プールを Multicall で読む
        prefetch = getattr(adapter, "prefetch", None)
        if prefetch is not None:
            _save_raw(conn, run_id, now(), block, None, prefetch(pools, block))
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
                "block_time": _iso_or_none(getattr(rewards, "block_time", None)),
                "epoch_start": _iso_or_none(getattr(rewards, "epoch_start", None)),
                "period_finish": _iso_or_none(getattr(rewards, "period_finish", None)),
                "reward_rate_effective_raw": _str_or_none(getattr(rewards, "reward_rate_effective_raw", None)),
                "gauge_alive": _bool_or_none(getattr(rewards, "gauge_alive", None)),
                "unstaked_fee": getattr(state, "unstaked_fee", None),
                "reward_epoch_total_raw": _str_or_none(getattr(rewards, "epoch_total_raw", None)),
                "reward_manual_raw": _str_or_none(getattr(rewards, "manual_raw", None)),
                "balance0_raw": _str_or_none(getattr(state, "balance0_raw", None)),
                "balance1_raw": _str_or_none(getattr(state, "balance1_raw", None)),
                "epoch_just_flipped": epoch_just_flipped(rewards, epoch_fresh_minutes),
                "source": f"rpc:{rpc.last_endpoint}",
            })
            conn.commit()
            if rewards is not None:
                check_reward_rate_drop(conn, adapter.venue_id, pool.pool_id, ts, reward_drop_alert_pct)
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


def _save_raw(conn: sqlite3.Connection, run_id: int, ts: datetime, block: int, pool_id: str | None,
              raw: tuple[RawCall, ...]) -> None:
    for r in raw:
        db.insert_raw(conn, run_id=run_id, ts=ts, block_number=block, pool_id=pool_id,
                      kind=r.kind, request=r.request, response=r.response)


def _str_or_none(v: int | None) -> str | None:
    return None if v is None else str(v)


def _iso_or_none(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat(timespec="seconds")


def _bool_or_none(v: bool | None) -> int | None:
    return None if v is None else int(v)


def epoch_just_flipped(rewards, fresh_minutes: int) -> int | None:
    """「エポック更新直後」の印。1 なら、報酬の値がまだ今週の値に切り替わっていない可能性がある。

    エポックの切り替えから fresh_minutes 分以内なら 1。
    （今週の報酬がまだ配られていないゲージは、印ではなく reward_rate_effective_raw = 0 と period_finish で分かる。
      実データでは週の途中でも配られていないゲージが約2割あり、それを「直後」と呼ぶと誤解を招くため分けている）
    エポックをまたいだ予測はしない（オーナー指示 2026-09-27）ので、この印の付いた値は判定に注意して使う。
    """
    start = getattr(rewards, "epoch_start", None)
    at = getattr(rewards, "block_time", None)
    if rewards is None or start is None or at is None:
        return None
    return 1 if at - start < timedelta(minutes=fresh_minutes) else 0


def check_reward_rate_drop(conn: sqlite3.Connection, venue_id: str, pool_id: str, ts: datetime,
                           drop_pct: float) -> bool:
    """同じエポックの中で、報酬の毎秒量が最大値から drop_pct% 以上減っていたら alerts に記録する。

    比べるのは「今実際に出ている量」（reward_rate_effective_raw）。
    エポック更新直後の印が付いた値は比較に使わない。1つのプールにつき1エポック1回だけ記録する。
    """
    rows = conn.execute(
        """SELECT ts, epoch_start, reward_rate_effective_raw, epoch_just_flipped FROM pool_snapshots
           WHERE pool_id=? AND epoch_start=(SELECT epoch_start FROM pool_snapshots WHERE pool_id=? AND ts=?)
             AND reward_rate_effective_raw IS NOT NULL AND COALESCE(epoch_just_flipped, 0)=0
           ORDER BY ts""",
        (pool_id, pool_id, ts.isoformat(timespec="seconds")),
    ).fetchall()
    if len(rows) < 2 or rows[-1]["ts"] != ts.isoformat(timespec="seconds"):
        return False
    peak = max(int(r["reward_rate_effective_raw"]) for r in rows[:-1])
    now_rate = int(rows[-1]["reward_rate_effective_raw"])
    if peak <= 0 or now_rate > peak * (1 - drop_pct / 100):
        return False
    drop = (1 - now_rate / peak) * 100
    added = db.insert_alert(
        conn, ts=ts, venue_id=venue_id, pool_id=pool_id, kind="reward_rate_drop", level="warning",
        message_ja=f"エポックの途中で報酬の毎秒量が {drop:.0f}% 減りました（最大値との比較）。",
        data={"peak_raw": str(peak), "now_raw": str(now_rate), "drop_pct": round(drop, 1),
              "epoch_start": rows[-1]["epoch_start"]},
        dedupe_key=f"reward_rate_drop:{pool_id}:{rows[-1]['epoch_start']}",
    )
    conn.commit()
    if added:
        log.warning("reward rate dropped mid-epoch", extra={"data": {"pool": pool_id, "drop_pct": round(drop, 1)}})
    return added
