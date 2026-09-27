from datetime import UTC, datetime, timedelta

import pytest

from farm_radar.adapters.base import AdapterNotReady, PoolInfo, PoolState, RawCall, RewardInfo
from farm_radar.collectors.completeness import check
from farm_radar.collectors.snapshot import collect_venue, slot_for
from farm_radar.config import load_venue
from farm_radar.db import database as db


class FakeRpc:
    last_endpoint = "public"

    def __init__(self, block=1000):
        self.block = block

    def block_number(self):
        return self.block


def pool(i):
    return PoolInfo(f"fake:0x{i:040x}", "fake", f"0x{i:040x}", "0xa", "0xb", fee_tier=500, tick_spacing=10)


class FakeAdapter:
    venue_id = "fake"
    chain = "test"

    def __init__(self, n=2, fail_state=(), fail_rewards=()):
        self.pools = [pool(i) for i in range(n)]
        self.fail_state, self.fail_rewards = fail_state, fail_rewards
        self.blocks_seen = set()

    def list_pools(self, block):
        self.blocks_seen.add(block)
        return self.pools

    def pool_state(self, p, block):
        self.blocks_seen.add(block)
        if p.pool_id in self.fail_state:
            raise RuntimeError("boom")
        return PoolState(p.pool_id, block, 2**96, 0, 1.0, 500, 10**30, 4 * 10**29,
                         raw=(RawCall("pool_state", {"to": p.address}, {"result": "0x01"}),))

    def gauge_rewards(self, p, block):
        if p.pool_id in self.fail_rewards:
            raise RuntimeError("no gauge")
        return RewardInfo(p.pool_id, block, "0xUP", 10**18, 86400.0, None,
                          raw=(RawCall("gauge_rewards", {"to": "0xgauge"}, {"result": "0x02"}),))

    def epoch_info(self, block):
        return None


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO venues(id, name, chain) VALUES ('fake','fake','test')")
    return c


def test_slot_for():
    t = datetime(2026, 9, 27, 12, 29, 59, tzinfo=UTC)
    assert slot_for(t, 15) == datetime(2026, 9, 27, 12, 15, tzinfo=UTC)


def test_snapshot_saves_block_both_liquidities_and_raw(conn):
    adapter = FakeAdapter()
    r = collect_venue(conn, adapter, FakeRpc(4242), snapshot_minutes=15)
    assert r.status == "ok" and r.pools_ok == 2 and r.block_number == 4242
    assert adapter.blocks_seen == {4242}   # 全部同じブロックで読んでいる
    rows = conn.execute("SELECT * FROM pool_snapshots").fetchall()
    assert len(rows) == 2
    row = rows[0]
    assert row["block_number"] == 4242
    assert int(row["liquidity_total"]) == 10**30
    assert int(row["liquidity_staked_inrange"]) == 4 * 10**29
    assert row["source"] == "rpc:public"
    kinds = [x["kind"] for x in conn.execute("SELECT kind FROM raw_rpc")]
    assert sorted(kinds) == ["gauge_rewards"] * 2 + ["pool_state"] * 2


def test_partial_failure(conn):
    adapter = FakeAdapter(n=3, fail_state={pool(0).pool_id}, fail_rewards={pool(1).pool_id})
    r = collect_venue(conn, adapter, FakeRpc(), snapshot_minutes=15)
    assert (r.status, r.pools_ok, r.pools_failed) == ("partial", 1, 2)
    # 報酬だけ失敗したプールも、状態のスナップショットは残す
    assert conn.execute("SELECT COUNT(*) FROM pool_snapshots").fetchone()[0] == 2


def test_adapter_not_ready_is_skipped(conn):
    class NotReady(FakeAdapter):
        def list_pools(self, block):
            raise AdapterNotReady("factory unverified")

    r = collect_venue(conn, NotReady(), FakeRpc(), snapshot_minutes=15)
    assert r.status == "skipped" and "factory" in r.error


def test_completeness(conn):
    start = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    adapter = FakeAdapter(n=1)
    for i in range(96):
        if i == 10:
            continue  # 1枠だけ欠けさせる
        t = start + timedelta(minutes=15 * i, seconds=3)
        collect_venue(conn, adapter, FakeRpc(i), snapshot_minutes=15, now=lambda t=t: t)
    r = check(conn, "fake", now=start + timedelta(hours=24, minutes=1))
    assert r.expected == 96 and r.ok == 95
    assert r.missing == [start + timedelta(minutes=150)]
    assert not r.passed

    t = start + timedelta(minutes=150, seconds=30)
    collect_venue(conn, adapter, FakeRpc(), snapshot_minutes=15, now=lambda: t)
    assert check(conn, "fake", now=start + timedelta(hours=24, minutes=1)).passed


def test_gap_is_recorded_after_sleep(conn):
    adapter = FakeAdapter(n=1)
    t0 = datetime(2026, 9, 27, 0, 0, 5, tzinfo=UTC)
    collect_venue(conn, adapter, FakeRpc(), snapshot_minutes=15, now=lambda: t0)
    collect_venue(conn, adapter, FakeRpc(), snapshot_minutes=15, now=lambda: t0 + timedelta(minutes=15))
    assert conn.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0] == 0

    # 00:15 の後、パソコンがスリープして 01:37 に復帰した
    wake = datetime(2026, 9, 27, 1, 37, tzinfo=UTC)
    collect_venue(conn, adapter, FakeRpc(), snapshot_minutes=15, now=lambda: wake)
    gaps = db.list_gaps(conn, "fake", t0 - timedelta(days=1))
    assert len(gaps) == 1
    g = gaps[0]
    assert g["start_slot"] == "2026-09-27T00:30:00+00:00"
    assert g["end_slot"] == "2026-09-27T01:15:00+00:00"
    assert g["missed_slots"] == 4
    assert db.in_gap(conn, "fake", datetime(2026, 9, 27, 1, 0, tzinfo=UTC))
    assert not db.in_gap(conn, "fake", datetime(2026, 9, 27, 1, 30, tzinfo=UTC))


def test_failed_run_before_collection_is_recorded(conn):
    from farm_radar.collectors.snapshot import record_failed_run

    t = datetime(2026, 9, 27, 3, 0, 1, tzinfo=UTC)
    record_failed_run(conn, "fake", 15, "RPCにつながらない", now=lambda: t)
    row = conn.execute("SELECT status, error FROM collection_runs").fetchone()
    assert row["status"] == "failed" and "RPC" in row["error"]
