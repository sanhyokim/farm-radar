"""ボーナスが減ったときの比べ方（2026-09-30 オーナー決定③。記録と知らせだけ）のテスト。"""

import dataclasses
from datetime import timedelta

from farm_radar.execution import bonus_drop
from farm_radar.execution import views as pviews
from farm_radar.execution.paper import PaperExecutor
from farm_radar.scoring.run import score_venue

from .test_paper import WETH_POOL, FakeFx, _config, _extend, _open, world  # noqa: F401
from .test_scoring_run import NOW, TOKENS, FakeGT, _ctx

E0 = NOW - timedelta(days=2)             # 建玉を始めた週の始まり
E1 = NOW + timedelta(hours=2, minutes=30)  # 持っている間に来た切り替え
PREV = 10 ** 18                          # 前の週の毎秒量（テストの記録と同じ）


def _flip(conn, rate: int, *, fresh_hours: float = 1.0):
    """E0 の週の記録にして、E1 からは新しい週（最初の fresh_hours 時間は切り替え直後の印）でボーナスを rate にする。"""
    conn.execute("UPDATE pool_snapshots SET epoch_start=?, epoch_end=?, epoch_just_flipped=0 WHERE ts<?",
                 (E0.isoformat(timespec="seconds"), E1.isoformat(timespec="seconds"), E1.isoformat(timespec="seconds")))
    conn.execute("""UPDATE pool_snapshots SET epoch_start=?, epoch_end=?, reward_rate_effective_raw=?, reward_rate_raw=?,
                    epoch_just_flipped=CASE WHEN ts<? THEN 1 ELSE 0 END WHERE ts>=?""",
                 (E1.isoformat(timespec="seconds"), (E1 + timedelta(days=7)).isoformat(timespec="seconds"), str(rate),
                  str(rate), (E1 + timedelta(hours=fresh_hours)).isoformat(timespec="seconds"),
                  E1.isoformat(timespec="seconds")))
    conn.commit()


def _setup(world, rate, hours_after=6, action="record"):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    # config.yaml は 2026-10-03 から exit（オーナー決定①A）。ここでは「記録と知らせだけ」の動きを確かめる
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, bonus_drop_action=action))
    _open(conn, path)
    _extend(conn, 2 + hours_after)
    _flip(conn, rate)
    last = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    from datetime import datetime
    now = datetime.fromisoformat(last) + timedelta(minutes=5)
    score_venue(conn, _ctx(FakeGT()), now=now)
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=now)
    return cfg, conn, ex, now


def _pos(conn):
    return conn.execute("SELECT * FROM positions WHERE pool_id=?", (WETH_POOL,)).fetchone()


def test_drop_to_zero_waits_for_distribution_then_compares_and_records_only(world):  # noqa: F811
    cfg, conn, ex, now = _setup(world, 0, hours_after=3)
    # 切り替えから6時間たつまでは、0でも「まだ配られていないだけ」かもしれないので比べない
    assert bonus_drop.compare(conn, cfg, ex, _pos(conn), now)["state"] == "wait"
    assert bonus_drop.run(conn, cfg, ex, now) == []
    # 記録が4時間分増えても0のまま → 切り替えから6時間を過ぎたので比べる
    _extend(conn, 4)
    _flip(conn, 0)
    later = now + timedelta(hours=4)
    score_venue(conn, _ctx(FakeGT()), now=later)
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=later)
    ids = bonus_drop.run(conn, cfg, ex, later)
    assert len(ids) == 1
    ev = conn.execute("SELECT * FROM risk_events WHERE id=?", (ids[0],)).fetchone()
    assert ev["kind"] == "bonus_drop" and ev["level"] == "caution" and ev["action"] == "none"
    assert "0になりました" in ev["message_ja"] and "建玉はそのまま" in ev["message_ja"]
    import json
    data = json.loads(ev["data_json"])
    assert set(data["options"]) >= {"stay", "exit"} and data["best"] in data["options"]
    assert data["options"]["exit"]["usd"] < 0                    # 抜けると閉じる費用の分だけ減る
    assert data["best"] == max(data["options"], key=lambda k: data["options"][k]["usd"])
    assert 0 < data["hours_left"] <= 7 * 24 and data["action_mode"] == "record"
    # 記録だけ: 建玉は開いたまま。同じ週は1回だけ
    p = _pos(conn)
    assert p["status"] == "open"
    assert bonus_drop.run(conn, cfg, ex, later + timedelta(hours=1)) == []
    # 画面の建玉カードに最新の結果が出る
    card = pviews.card(conn, p, later)
    assert card["bonus_drop"]["best_ja"] and card["bonus_drop"]["event"] == ids[0]
    # 通知の箱（アプリの中）にも入る
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='paper_caution'").fetchone()[0] >= 1


def test_half_or_less_compares_but_a_small_drop_does_not(world):  # noqa: F811
    cfg, conn, ex, now = _setup(world, PREV // 3)
    r = bonus_drop.compare(conn, cfg, ex, _pos(conn), now)
    assert r["state"] == "drop" and r["ratio"] < 0.5
    assert "前の週の33%に減りました" in bonus_drop.message_ja("WETH/USDG", r)


def test_small_drop_is_only_marked_as_checked(world):  # noqa: F811
    cfg, conn, ex, now = _setup(world, PREV * 8 // 10)
    assert bonus_drop.run(conn, cfg, ex, now) == []
    import json
    assert json.loads(_pos(conn)["state_json"])["bonus_checked"] == E1.isoformat(timespec="seconds")
    assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE kind='bonus_drop'").fetchone()[0] == 0


def test_right_after_the_flip_it_waits(world):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    _open(conn, path)
    _extend(conn, 3)
    _flip(conn, 0, fresh_hours=24)            # 切り替え直後の印が付いている間は比べない
    ex = PaperExecutor(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3))
    assert bonus_drop.compare(conn, cfg, ex, _pos(conn), NOW + timedelta(hours=3))["state"] == "wait"
