"""Merkl の分母 B（幅の中だけ）の検証（2026-10-04 指示書）。

- チェーンの記録（PoolManager の ModifyLiquidity）から、その時の預け方を組み立て直す（feeds/pool_history.py）。
- 同じ区切り・同じ預け方で A（全員が分母）・B（幅の中だけ）・実際を並べる（merkl_ab.py）。
- 実際 = 預け方の累計（確定した額＋まだ確定していない額）の増え方 ÷ 全部の行の累計の増え方（merkl_check.boundaries）。
"""

import json
import math

import pytest

from farm_radar import merkl_ab, merkl_check, ratelimit
from farm_radar.feeds import pool_history as ph
from farm_radar.feeds import store, trial
from farm_radar.rpc.client import RpcCallError

from .test_n5a import CTX, NOW, Fake, _campaign

PM = "0x" + "11" * 20                 # 公式の PositionManager（テストの住所）
MGR = "0x" + "22" * 20                # 公式の PoolManager（テストの住所）
OTHER = "0x" + "33" * 20
PID = "0x" + "cd" * 32
CHAINS = {4663: {"id": 4663, "uniswap": {"v4_pool_manager": MGR, "v4_position_manager": PM}},
          8453: {"id": 8453, "uniswap": {"v4_pool_manager": MGR, "v4_position_manager": PM}}}


def test_position_key_matches_a_real_merkl_reason():
    """2026-10-04 に Robinhood Chain の記録で確かめた: Merkl の預け方の名前の末尾（0x…）は v4 の預け方の印。
    SPY/NVDA のプール・預けた契約 0x1149…・幅 11895〜11898・salt 0。"""
    key = ph.position_key("0x1149133ed99dd48a2029eb27c15edbb38f572fb1", 11895, 11898, "0x" + "00" * 32)
    assert key == "0x685fc8015a94e93db14540ea20a731e38c9ba5c979cd75110847dada9d380710"


def _word(v: int) -> str:
    return format(v % (1 << 256), "064x")


def _log(block: int, idx: int, owner: str, lo: int, hi: int, delta: int, salt: int, ts: int | None = 0) -> dict:
    return {"blockNumber": hex(block), "logIndex": hex(idx), "blockTimestamp": hex(ts) if ts is not None else None,
            "topics": [ph.MODIFY_LIQUIDITY, PID, "0x" + "00" * 12 + owner[2:]],
            "data": "0x" + _word(lo) + _word(hi) + _word(delta) + _word(salt), "removed": False}


def test_parse_log_reads_negative_ticks_and_decreases():
    e = ph.parse_log(_log(10, 2, PM, -600, 600, -5, 7, ts=0))
    assert (e["tick_lower"], e["tick_upper"], e["delta"], e["owner"], int(e["salt"], 16)) == (-600, 600, -5, PM, 7)
    assert e["ts"] is None                                    # 0 は「時刻なし」（Robinhood Chain の公開の読み取り口）
    assert e["pos_key"] == ph.position_key(PM, -600, 600, e["salt"])


class FakeChain:
    """読み取り専用の偽のチェーン。ブロック n の時刻 = 1,000,000 + 10n。広い範囲は「多すぎる」と答える。"""

    pace_key = "test"

    def __init__(self, logs, head=1000, max_span=10**9, limit_after=None):
        self.logs, self.head, self.max_span, self.limit_after = logs, head, max_span, limit_after
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if self.limit_after is not None and len(self.calls) > self.limit_after:
            raise RpcCallError(429, "Too Many Requests")
        if method == "eth_getBlockByNumber":
            n = self.head if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(n), "timestamp": hex(1_000_000 + 10 * n)}
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            if hi - lo + 1 > self.max_span:
                raise RpcCallError(-32602, "logs matched by query exceeds limit of 10000")
            return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]
        raise AssertionError(method)


def _world(tmp_path, first_ts="1970-01-12T13:53:20+00:00", st_extra=None):
    """キャンペーン1つ（記録の始まり = first_ts）。first_ts は 1,000,000 + 10 × 1000 秒 = ブロック 1000 のころ。"""
    ratelimit.reset()
    conn = store.connect(tmp_path / "f.sqlite3")
    st = {"poolId": PID, "poolManager": MGR, "weightFees": 7000, "weightToken0": 1500, "weightToken1": 1500,
          "isOutOfRangeIncentivized": False, "blacklist": [], "whitelist": [], "symbolCurrency0": "ETH",
          "symbolCurrency1": "USDG", **(st_extra or {})}
    conn.execute("INSERT INTO merkl_campaigns(campaign_id, chain_id, type, settings_json, opportunity_id, first_seen, "
                 "last_seen) VALUES ('c1', 4663, 'UNISWAP_V4', ?, 'o1', 'x', 'x')", (json.dumps(st),))
    conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw) VALUES (?, 'c1', 'r', 'x', '0')",
                 (first_ts,))
    conn.commit()
    return conn


def test_history_is_folded_before_the_records_and_kept_after_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "KEEP_MARGIN_S", 0)
    # 記録の始まり = 時刻 1,010,000 = ブロック 1000。そこより前は合計にまとめ、後は1件ずつ残す
    conn = _world(tmp_path, first_ts="1970-01-12T16:33:20+00:00")
    logs = [_log(100, 0, PM, -60, 60, 500, 7), _log(200, 0, OTHER, 0, 120, 300, 0), _log(300, 0, PM, -60, 60, -100, 7),
            _log(1500, 0, PM, -60, 60, 50, 7), _log(1600, 1, OTHER, 0, 120, -300, 0)]
    fake = FakeChain(logs, head=2000, max_span=700)
    out = ph.read(conn, CHAINS, NOW, rpc_factory=lambda c: fake, sleep=lambda s: None, budget=16)
    r = out[f"4663:{PID}"]
    assert r.get("skipped") == "この回に読む回数を使い切った"          # 回数を使い切ったら、そこでやめる
    prog = conn.execute("SELECT * FROM pool_liq_progress").fetchone()
    # 記録の始まりの時刻より後の最初のブロック（ブロックの時刻を二分探索）
    assert prog["keep_from_block"] == 1001 and 0 < prog["next_block"] < 2001 and prog["done_ts"] is None
    # 次の回は続きから（同じ範囲を読み直さない）
    fake.calls.clear()
    out = ph.read(conn, CHAINS, NOW, rpc_factory=lambda c: fake, sleep=lambda s: None, budget=50)
    assert out[f"4663:{PID}"]["caught_up"] is True
    first_from = min(int(p[0]["fromBlock"], 16) for m, p in fake.calls if m == "eth_getLogs")
    assert first_from == prog["next_block"]
    base = {r["owner"]: int(r["liquidity"]) for r in conn.execute("SELECT * FROM pool_liq_base")}
    assert base == {PM: 400, OTHER: 300}
    evs = conn.execute("SELECT block, ts, delta FROM pool_liq_events ORDER BY block").fetchall()
    assert [(e["block"], e["ts"], e["delta"]) for e in evs] == [(1500, 1_015_000, "50"), (1600, 1_016_000, "-300")]
    prog = conn.execute("SELECT * FROM pool_liq_progress").fetchone()
    assert prog["next_block"] == 2001 and prog["done_ts"] == 1_020_000
    # 読み終えたプールは、しばらく読まない
    out = ph.read(conn, CHAINS, NOW.replace(year=1970, month=1, day=12, hour=17, minute=0), rpc_factory=lambda c: fake,
                  sleep=lambda s: None)
    assert "読み終えている" in out[f"4663:{PID}"]["skipped"]


def test_rate_limit_stops_the_round_and_keeps_what_was_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "KEEP_MARGIN_S", 0)
    conn = _world(tmp_path)
    fake = FakeChain([_log(100, 0, PM, -60, 60, 500, 7)], head=2000, max_span=300, limit_after=4)
    out = ph.read(conn, CHAINS, NOW, rpc_factory=lambda c: fake, sleep=lambda s: None, budget=100)
    assert "回数制限" in out[f"4663:{PID}"]["skipped"]
    assert conn.execute("SELECT error FROM pool_liq_progress").fetchone()[0] is None


def test_unofficial_pool_manager_and_too_long_history_are_not_read(tmp_path):
    conn = _world(tmp_path, st_extra={"poolManager": "0x" + "99" * 20})
    out = ph.read(conn, CHAINS, NOW, rpc_factory=lambda c: FakeChain([]), sleep=lambda s: None)
    assert "公式の住所" in out[f"4663:{PID}"]["error"]
    # Base（1回 2,000 ブロックまで）: プールの始まりから読むと回数が多すぎるものは読まない
    conn2 = _world(tmp_path / "b")
    conn2.execute("UPDATE merkl_campaigns SET chain_id=8453")
    conn2.commit()
    fake = FakeChain([], head=40_000_000)
    out = ph.read(conn2, CHAINS, NOW, rpc_factory=lambda c: fake, sleep=lambda s: None)
    assert "読む量が多すぎる" in out[f"8453:{PID}"]["error"]
    assert [m for m, _ in fake.calls] == ["eth_getBlockByNumber"]


def test_interpolated_block_time_uses_anchor_blocks(tmp_path):
    conn = _world(tmp_path)
    r = ph.Reader(conn, 4663, FakeChain([], head=1_000_000), 100, lambda s: None)
    r.read_head()
    assert r.time_of(400_000) == 1_000_000 + 10 * 400_000      # 目印 360,000 と 720,000 の間を比例で
    assert r.block_at(1_000_000 + 10 * 777) == 778               # 時刻より後の最初のブロック


# --- A / B / 実際 -----------------------------------------------------------------------------------------

T0, T1 = "2026-10-04T00:00:00+00:00", "2026-10-04T02:00:00+00:00"


def _ab_world(tmp_path, st_extra=None, own_event_at=None, l_act=None):
    """プール: 自分（番号 7、幅の中）・ほか（幅の中、印の名前）・番号 8（幅の外。値段より上なので token0 だけ）。
    Merkl は B どおりに配った（幅の中だけが分母）ことにする。"""
    conn = store.connect(tmp_path / "ab.sqlite3")
    sq = math.sqrt(2500 * 1e6 / 1e18)
    tick = int(math.log(sq * sq) / math.log(1.0001))
    st = {"poolId": PID, "poolManager": MGR, "weightFees": 7000, "weightToken0": 1500, "weightToken1": 1500,
          "isOutOfRangeIncentivized": False, "blacklist": [], "whitelist": [], "symbolCurrency0": "ETH",
          "symbolCurrency1": "USDG", **(st_extra or {})}
    conn.execute("INSERT INTO merkl_campaigns(campaign_id, chain_id, type, settings_json, opportunity_id, first_seen, "
                 "last_seen) VALUES ('c1', 4663, 'UNISWAP_V4', ?, 'o1', 'x', 'x')", (json.dumps(st),))
    conn.execute("INSERT INTO merkl_opportunity_snaps(ts, opportunity_id, tvl) VALUES (?, 'o1', 100000)", (T1,))
    pos = {"own": (PM, tick - 600, tick + 600, 7, 10 ** 14), "x": (OTHER, tick - 1200, tick + 1200, 0, 9 * 10 ** 14),
           "out": (PM, tick + 1200, tick + 2400, 8, 5 * 10 ** 14)}
    keys = {}
    for name, (owner, lo, hi, salt, liq) in pos.items():
        s = "0x" + format(salt, "064x")
        k = ph.position_key(owner, lo, hi, s)
        keys[name] = k
        conn.execute("INSERT INTO pool_liq_base VALUES (4663, ?, ?, ?, ?, ?, ?, ?)", (PID, k, owner, lo, hi, s, str(liq)))
    t0 = merkl_ab._ts(T0)
    conn.execute("INSERT INTO pool_liq_progress(chain_id, pool_id, manager, keep_from_ts, keep_from_block, next_block, "
                 "done_ts) VALUES (4663, ?, ?, ?, 10, 999, ?)", (PID, MGR, t0 - 86400, t0 + 86400))
    if own_event_at is not None:
        owner, lo, hi, salt, _ = pos["own"]
        conn.execute("INSERT INTO pool_liq_events VALUES (4663, ?, 500, 0, ?, ?, ?, ?, ?, ?, '0')",
                     (PID, own_event_at, keys["own"], owner, lo, hi, "0x" + format(salt, "064x")))
    for h in ("01", "02"):
        conn.execute("INSERT INTO pool_state_snaps(chain_id, pool_id, checked_at, kind, sqrt_price_x96, tick, liquidity) "
                     "VALUES (4663, ?, ?, 'v4', ?, ?, ?)", (PID, f"2026-10-04T{h}:00:00+00:00", str(int(sq * 2 ** 96)),
                                                           tick, str(l_act or 10 ** 15)))
    # B どおりの取り分（手数料: 流動性の割合。token0・token1: 幅の中の2つのうちの割合）
    a_own = merkl_check.amounts(1e14, sq, tick - 600, tick + 600)
    a_x = merkl_check.amounts(9e14, sq, tick - 1200, tick + 1200)
    share_own = 0.7 * 0.1 + 0.15 * a_own[0] / (a_own[0] + a_x[0]) + 0.15 * a_own[1] / (a_own[1] + a_x[1])
    got_own = round(share_own * 1_000_000)
    rows = {f"UNISWAP_V4_{PID}_7": (0, got_own), f"UNISWAP_V4_{PID}_{keys['x']}": (0, 1_000_000 - got_own),
            f"UNISWAP_V4_{PID}_8": (0, 0)}
    for reason, (v0, v1) in rows.items():
        conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw, pending_raw) "
                     "VALUES (?, 'c1', 'r', ?, ?, '0')", (T0, reason, str(v0)))
        # 区切りの終わりは、確定した額と、まだ確定していない額に分かれている（足して数える）
        conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw, pending_raw) "
                     "VALUES (?, 'c1', 'r', ?, ?, ?)", (T1, reason, str(v1 // 2), str(v1 - v1 // 2)))
    conn.commit()
    return conn, keys, tick


def _check(conn):
    merkl_ab._CACHE.clear()
    return merkl_ab.check(conn, {4663: PM}, {"c1": 0.5})


def test_a_b_and_actual_are_lined_up_on_the_same_interval(tmp_path):
    conn, keys, _ = _ab_world(tmp_path)
    res = _check(conn)
    c = res["campaigns"][0]
    assert c["pair"] == "ETH/USDG" and c["out_label"] == "幅の中だけ" and c["pairs"] == 2 and c["positions"] == 3
    # Merkl は B どおりに配ったので、B の差は 0、A は控えめ（幅の外の預け方の token0 も分母に入れている）
    assert c["err_b"] == pytest.approx(0, abs=1e-5) and c["err_a"] < -0.01
    assert res["overall"]["b"]["within"] == 1.0 and res["overall"]["pairs"] == 2
    # B÷A（幅の中の額 ÷ 全部の額）は 1 より小さい。逆算した割合は「B どおりなら」と同じ
    assert 0 < c["ratio"] < 1 and c["share_backcalc"] == pytest.approx(c["share_chain"], rel=1e-4)
    assert c["share_backcalc_now"] == 0.5 and c["n_all"] == 3 and c["n_in"] == 2
    assert res["skipped"]["区切りのあいだ幅の外にいた"] == 1                # 番号 8
    # 手数料の分は A と B で同じ。5% 上限の準備: B の分母は小さいので、自分の割合は大きい
    assert "A と同じ" in res["parts"]["fee"]["b"]
    cap = res["cap"][0]
    assert cap["b"][1000] > cap["a"][1000] == pytest.approx(1000 / 101000)


def test_own_change_in_the_interval_or_near_the_edge_is_not_used(tmp_path):
    t0 = merkl_ab._ts(T0)
    conn, _, _ = _ab_world(tmp_path, own_event_at=t0 + 3600)
    res = _check(conn)
    assert res["skipped"]["区切りの間に預け方が変わった（足す・減らす・閉じる・作る）"] == 1
    conn2, _, _ = _ab_world(tmp_path / "e", own_event_at=t0 - 30)
    assert _check(conn2)["skipped"]["区切りの境目の近くで預け方が変わった（時刻を確かめきれない）"] == 1


def test_rebuilt_liquidity_must_match_the_chain(tmp_path):
    conn, _, _ = _ab_world(tmp_path, l_act=2 * 10 ** 15)
    res = _check(conn)
    assert res["overall"]["pairs"] == 0
    assert res["skipped"]["組み立て直した流動性がチェーンで読んだ値と合わない区切り"] == 1


@pytest.mark.parametrize("extra,why", [
    ({"isOutOfRangeIncentivized": True}, "幅の外にも配る設定なので B を当てはめない"),
    ({"isOutOfRangeIncentivized": None}, "幅の外の扱いが設定にない（判断できない）"),
    ({"blacklist": ["0x" + "44" * 20]}, "除外する人（または対象の人）の決まりがある（その人の預け方を分母から外せない）"),
])
def test_campaign_settings_that_b_cannot_be_applied_to(tmp_path, extra, why):
    conn, _, _ = _ab_world(tmp_path, st_extra=extra)
    res = _check(conn)
    assert res["overall"]["pairs"] == 0 and res["skipped"][why] == 1


def test_unknown_position_names_are_counted_not_guessed(tmp_path):
    conn, _, _ = _ab_world(tmp_path)
    for reason in (f"UNISWAP_V4_{PID}_123456", f"UNISWAP_V4_{PID}_0x" + "ab" * 32, "roundingError"):
        conn.execute("INSERT INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw) VALUES (?, 'c1', 'r', ?, '0')",
                     (T0, reason))
    conn.commit()
    res = _check(conn)
    assert res["unusable"] == {"チェーンの記録に見つからない（記録の始まりより前に閉じた預け方など）": 2, "名前の形が違う": 1}
    assert res["overall"]["pairs"] == 2


# --- 実際の数え方（確定した額 + まだ確定していない額。全部の行の合計） -----------------------------------------

def test_boundaries_use_all_rows_and_skip_the_top_100_only_period(tmp_path):
    conn, _, _ = _ab_world(tmp_path)
    b, why = merkl_check.boundaries(conn, "c1")
    assert why is None and [x[1] for x in b] == [0, 1_000_000]
    # 先頭100行しか読んでいなかった期間は、全体の増え方が数えられないので使わない
    b, why = merkl_check.boundaries(conn, "c1", page_rows=3)
    assert b == [] and "先頭100行" in why
    # 全部のページを読んだ記録（complete=1）があれば、そちらを使う
    conn.execute("INSERT INTO merkl_reward_sums VALUES ('2026-10-04T04:00:00+00:00', 'c1', '3000000', 500, 1)")
    conn.execute("INSERT INTO merkl_reward_sums VALUES ('2026-10-04T06:00:00+00:00', 'c1', '4000000', 500, 0)")
    b, why = merkl_check.boundaries(conn, "c1", page_rows=3)
    assert b == [("2026-10-04T04:00:00+00:00", 3_000_000)]


def test_rewards_read_all_pages_and_record_the_sum(tmp_path):
    conn = store.connect(tmp_path / "r.sqlite3")
    _campaign(conn, "0xa", chain=8453, dist=8453)
    page0 = [{"recipient": f"0x{i:040x}", "reason": f"R{i}", "amount": "10", "pending": "1"} for i in range(100)]
    page1 = [{"recipient": "0x" + "00" * 19 + "63", "reason": "R99", "amount": "10", "pending": "1"},   # 境目で同じ行
             {"recipient": "0x" + "ff" * 20, "reason": "R100", "amount": "5", "pending": "0"}]

    def routes(url, p):
        if url.endswith("/rewards/"):
            return page0 if p["page"] == "0" else page1
        return {"amount": "999"}
    f = Fake(routes)
    data, calls = trial.read_merkl_rewards(conn, f.text, CTX, NOW)
    assert calls == 3 and data["0xa"]["complete"] is True and len(data["0xa"]["rows"]) == 101
    trial.write_merkl_rewards(conn, "t1", data)
    s = conn.execute("SELECT sum_raw, rows, complete FROM merkl_reward_sums").fetchone()
    assert tuple(s) == (str(100 * 11 + 5), 101, 1)
    trial.write_merkl_rewards(conn, "t2", data)                    # 変わっていなければ書かない
    assert conn.execute("SELECT COUNT(*) FROM merkl_reward_sums").fetchone()[0] == 1


# --- パソコンへの反映（2026-10-04 指示書「PR40を今PCへ反映し、Merkl A/B用の記録を開始する」） ---------------

def _rows(n, amount="10"):
    return [{"recipient": f"0x{i:040x}", "reason": f"R{i}", "amount": amount, "pending": "1"} for i in range(n)]


def _src(sid):
    from farm_radar.feeds.sources import SOURCES
    return next(s for s in SOURCES if s.id == sid)


def _sums(conn):
    return [tuple(r) for r in conn.execute("SELECT ts, campaign_id, sum_raw, rows, complete FROM merkl_reward_sums "
                                           "ORDER BY campaign_id, ts")]


def test_rate_limit_in_the_middle_of_the_pages_writes_nothing(tmp_path):
    """429 のときは、その回の配った額を1行も書かない（途中までの合計を complete=1 にしない）。前の記録は残る。"""
    from datetime import timedelta

    from farm_radar.config import FeedSettings
    from farm_radar.external.http import ExternalError
    from farm_radar.feeds.run import every_due, run_source
    settings = FeedSettings(database_path=tmp_path / "feeds.sqlite3", raw_dir=tmp_path / "feeds")
    conn = store.connect(settings.database_path)
    _campaign(conn, "0xa", chain=8453, dist=8453, daily=200.0)        # 先に読む
    _campaign(conn, "0xb", chain=8453, dist=8453, daily=100.0)
    conn.commit()
    src = _src("merkl_rewards")

    def ok(url, p):
        if url.endswith("/rewards/"):
            return _rows(100) if p["page"] == "0" else _rows(3)
        return {"amount": "1"}
    assert run_source(conn, src, Fake(ok), settings, NOW, trial_ctx=CTX)["status"] == "ok"
    before = _sums(conn)
    assert [r[4] for r in before] == [1, 1] and before[0][3] == 100
    snaps = conn.execute("SELECT COUNT(*) FROM merkl_reward_snaps").fetchone()[0]

    later = NOW + timedelta(hours=2)

    def limited(url, p):
        if url.endswith("/rewards/") and p["campaignId"] == "0xb" and p["page"] == "1":
            return ExternalError("merkl: HTTP 429", 429)             # 2つ目のキャンペーンの2ページ目で回数制限
        if url.endswith("/rewards/"):
            return _rows(100, "20") if p["page"] == "0" else _rows(3, "20")
        return {"amount": "2"}
    out = run_source(conn, src, Fake(limited), settings, later, trial_ctx=CTX)
    assert out["status"] == "rate_limited"
    assert _sums(conn) == before                                     # 1つ目のキャンペーン（読めた分）も書かない
    assert conn.execute("SELECT COUNT(*) FROM merkl_reward_snaps").fetchone()[0] == snaps
    assert every_due(conn, src, later + timedelta(minutes=15))       # 次の回（15分後）にやり直す


def test_a_broken_page_skips_only_that_campaign_and_too_many_pages_is_not_complete(tmp_path, monkeypatch):
    from farm_radar.external.http import ExternalError
    conn = store.connect(tmp_path / "f.sqlite3")
    _campaign(conn, "0xa", chain=8453, dist=8453, daily=200.0)
    _campaign(conn, "0xb", chain=8453, dist=8453, daily=100.0)
    _campaign(conn, "0xc", chain=8453, dist=8453, daily=50.0)
    monkeypatch.setattr(trial, "MAX_REWARD_PAGES", 3)

    def routes(url, p):
        if url.endswith("/rewards/"):
            if p["campaignId"] == "0xa":
                return _rows(100) if p["page"] == "0" else ExternalError("merkl: HTTP 500", 500)
            if p["campaignId"] == "0xb":
                return _rows(100)                                    # いつも満ページ → 上限の3ページで打ち切り
            return _rows(5)
        return {"amount": "1"}
    data, calls = trial.read_merkl_rewards(conn, Fake(routes).text, CTX, NOW)
    assert "error" in data["0xa"] and data["0xb"]["complete"] is False and data["0xc"]["complete"] is True
    trial.write_merkl_rewards(conn, "t1", data)
    assert {r[1]: r[4] for r in _sums(conn)} == {"0xb": 0, "0xc": 1}   # 0xa は書かない。0xb は complete=0
    # 全部読めなかった記録は、区切りに使わない
    b, _why = merkl_check.boundaries(conn, "0xb")
    assert b == []


def test_new_full_page_record_starts_right_after_the_update(tmp_path):
    """前の版が少し前に配った額を読んでいても、合計の表が空なら、約2時間を待たずにすぐ読む。"""
    from datetime import timedelta

    from farm_radar.feeds.run import every_due
    conn = store.connect(tmp_path / "f.sqlite3")
    src = _src("merkl_rewards")
    rid = store.start_run(conn, "merkl_rewards", NOW - timedelta(minutes=10))
    store.finish_run(conn, rid, NOW - timedelta(minutes=9), "ok")
    assert every_due(conn, src, NOW)                                 # 合計の表が空 → すぐ
    conn.execute("INSERT INTO merkl_reward_sums(ts, campaign_id, sum_raw, rows, complete) VALUES ('t', 'c', '1', 1, 1)")
    assert not every_due(conn, src, NOW)                             # 記録が始まったら、いつもの約2時間ごと
    assert every_due(conn, src, NOW + timedelta(minutes=110))


def test_pool_history_records_each_round_and_reports_progress(tmp_path, monkeypatch):
    from farm_radar import trial_records
    monkeypatch.setattr(ph, "KEEP_MARGIN_S", 0)
    conn = _world(tmp_path, first_ts="1970-01-12T16:33:20+00:00")
    fake = FakeChain([_log(100, 0, PM, -60, 60, 500, 7), _log(1500, 0, PM, -60, 60, 50, 7)], head=2000, max_span=700)
    ph.read(conn, CHAINS, NOW, rpc_factory=lambda c: fake, sleep=lambda s: None, budget=16)
    st = ph.status(conn)
    assert st["targets"] == 1 and st["partial"] == 1 and st["resumable"] == 1 and st["done"] == 0
    assert st["last_run"]["calls"] == 16 and st["last_run"]["stopped"] == "この回に読む回数を使い切った"
    later = NOW.replace(minute=30)
    ph.read(conn, CHAINS, later, rpc_factory=lambda c: fake, sleep=lambda s: None, budget=50)
    st = ph.status(conn)
    assert st["done"] == 1 and st["partial"] == 0 and st["runs"] == 2 and st["last_run"]["stopped"] is None
    assert st["last_run"]["pools_read"] == 1 and st["last_run"]["events"] >= 1 and st["errors"] == []
    # パソコンの1行の更新のまとめが読む形（/api/trial/records の feeds）
    conn.close()
    f = trial_records.feeds(tmp_path / "f.sqlite3")
    assert f["pool_history"]["done"] == 1 and all(f["tables"].values())
    assert f["merkl_sums"]["records"] == 0
