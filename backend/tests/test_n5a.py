"""N5a（試すための記録を集める）のテスト。外のサイトには行かず、偽の応答で確かめる。

- Merkl の配った額（預け方ごと）・DefiLlama の毎日の記録・Lighter の資金調達率の過去・Aero の公式の住所・影の記録
- 今の版のデータの写しを読む（読むだけ）と、写しの確かめ（scripts/pc/copy-18000.ps1 の中の Python）
"""

import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from farm_radar import trial_records
from farm_radar.config import FeedSettings
from farm_radar.db import database as db
from farm_radar.external.http import ExternalError
from farm_radar.feeds import shadow, store, trial
from farm_radar.feeds import status as feeds_status
from farm_radar.feeds.run import FeedRunner, run_source
from farm_radar.feeds.sources import BY_ID

NOW = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
NOW_S = int(NOW.timestamp())
REPO = Path(__file__).resolve().parents[2]


class Fake:
    """URL（と一部の引数）ごとに本文か例外を返す偽の読み手。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def text(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        r = self.routes(url, params) if callable(self.routes) else self.routes[url]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


@pytest.fixture
def settings(tmp_path):
    return FeedSettings(database_path=tmp_path / "feeds.sqlite3", raw_dir=tmp_path / "feeds")


def _campaign(conn, cid, chain=8453, dist=1, weights=True, start=NOW_S - 86400, end=NOW_S + 86400, daily=100.0):
    settings_json = json.dumps({"weightFees": 7000, "weightToken0": 1500, "weightToken1": 1500}) if weights else None
    conn.execute("INSERT INTO merkl_campaigns(campaign_id, opportunity_id, chain_id, distribution_chain_id, start_ts, "
                 "end_ts, daily_rewards, settings_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cid, "o1", chain, dist, start, end, daily, settings_json, "x", "x"))


CTX = trial.TrialContext(chain_ids=(8453, 4663), llama_chains=("Base", "Robinhood Chain"),
                         perp_alias={"WETH": "ETH", "cbBTC": "BTC"})


# --- Merkl の配った額 ----------------------------------------------------------------------------------

def test_reward_campaigns_are_registered_range_campaigns_that_started(settings):
    conn = store.connect(settings.database_path)
    _campaign(conn, "0xa")                                       # Base・幅に配る → 読む
    _campaign(conn, "0xb", chain=4663, dist=4663)                # Robinhood → 読む
    _campaign(conn, "0xc", chain=1)                              # 登録していないチェーン
    _campaign(conn, "0xd", weights=False)                        # 幅に配らない（貸し出しなど）
    _campaign(conn, "0xe", start=NOW_S + 3600)                   # まだ始まっていない
    _campaign(conn, "0xf", end=NOW_S - 2 * 86400)                # 終わって2日（最後の配りはもう読んだ）
    _campaign(conn, "0x10", end=NOW_S - 3600)                    # 終わって1時間 → 最後の配りを読む
    got = {r["campaign_id"] for r in trial.reward_campaigns(conn, CTX.chain_ids, NOW)}
    assert got == {"0xa", "0xb", "0x10"}


def test_rewards_are_read_with_the_distribution_chain_and_only_changes_are_written(settings):
    conn = store.connect(settings.database_path)
    _campaign(conn, "0xa", chain=8453, dist=1)
    rows = [{"recipient": "0xAAA", "reason": "UNISWAP_V3_0xpool_5869532", "amount": "100", "claimed": "0",
             "pending": "5", "rewardTokenAddress": "0xTOK"}]
    f = Fake(lambda url, p: rows if url.endswith("/rewards/") else {"campaignId": "0xa", "amount": "1000"})
    data, calls = trial.read_merkl_rewards(conn, f.text, CTX, NOW)
    assert calls == 2 and f.calls[0][1]["chainId"] == "1"          # 配る側のチェーン（預ける側 8453 だと 404）
    assert trial.write_merkl_rewards(conn, "t1", data) == 1
    assert trial.write_merkl_rewards(conn, "t2", data) == 0          # 同じ額は書かない
    rows[0]["amount"] = "130"                                         # 次の配る木で増えた
    data, _ = trial.read_merkl_rewards(conn, f.text, CTX, NOW)
    assert trial.write_merkl_rewards(conn, "t3", data) == 1
    snaps = conn.execute("SELECT ts, amount_raw, recipient FROM merkl_reward_snaps ORDER BY ts").fetchall()
    assert [tuple(r) for r in snaps] == [("t1", "100", "0xaaa"), ("t3", "130", "0xaaa")]   # 差 = 30（この間に配った額）
    assert conn.execute("SELECT COUNT(*) FROM merkl_reward_totals").fetchone()[0] == 1       # 合計は変わったときだけ


def test_a_missing_campaign_is_kept_as_an_error_and_429_gives_up_the_round(settings):
    conn = store.connect(settings.database_path)
    _campaign(conn, "0xa")
    _campaign(conn, "0xb", daily=50.0)

    def routes(url, p):
        if p.get("campaignId") == "0xa":
            return ExternalError("HTTP 404", 404)
        return [] if url.endswith("/rewards/") else {"amount": "0"}
    data, _ = trial.read_merkl_rewards(conn, Fake(routes).text, CTX, NOW)
    assert data["0xa"]["error"] and data["0xb"]["rows"] == []
    with pytest.raises(ExternalError):
        trial.read_merkl_rewards(conn, Fake(lambda u, p: ExternalError("HTTP 429", 429)).text, CTX, NOW)


# --- DefiLlama の毎日の記録 -------------------------------------------------------------------------------

def _yield_item(conn, key, chain, tvl, reward):
    conn.execute("INSERT INTO feed_items(source, key, first_seen, last_seen, chain, info_json) VALUES "
                 "('llama_yields', ?, 'x', 'x', ?, ?)", (key, chain, json.dumps({"tvl": tvl, "apy_reward": reward})))


def test_llama_pools_take_big_pools_and_bonus_pools_on_registered_chains(settings, monkeypatch):
    conn = store.connect(settings.database_path)
    monkeypatch.setattr(trial, "LLAMA_TOP_TVL", 1)
    monkeypatch.setattr(trial, "LLAMA_TOP_REWARD", 2)
    _yield_item(conn, "big-lend", "Base", 5e8, 0)                # 預かり額がいちばん多い（比べる相手の貸し出し）
    _yield_item(conn, "lend2", "Base", 4e8, 0)                   # 2番目（上限1なので入らない）
    _yield_item(conn, "bonus1", "Robinhood Chain", 2e6, 12.0)
    _yield_item(conn, "bonus2", "Base", 1e6, 30.0)
    _yield_item(conn, "bonus3", "Base", 9e5, 30.0)               # ボーナスのある3番目（上限2なので入らない）
    _yield_item(conn, "tiny", "Base", 1e4, 30.0)                 # 小さすぎる
    _yield_item(conn, "other", "Ethereum", 9e9, 5.0)             # 登録していないチェーン
    assert trial.llama_pools(conn, CTX.llama_chains) == ["big-lend", "bonus1", "bonus2"]


def test_llama_history_is_written_per_day_and_old_days_are_dropped(settings):
    conn = store.connect(settings.database_path)
    _yield_item(conn, "p1", "Base", 1e6, 5.0)
    pts = [{"timestamp": "2025-01-01T00:00:00.000Z", "tvlUsd": 1, "apy": 1},
           {"timestamp": "2026-10-01T00:00:00.000Z", "tvlUsd": 2e6, "apy": 12.5, "apyBase": 2.5, "apyReward": 10.0},
           {"timestamp": "2026-10-02T00:00:00.000Z", "tvlUsd": 2.1e6, "apy": 11.0}]
    data, calls = trial.read_llama_history(conn, Fake({f"{trial.LLAMA_CHART}/p1": {"status": "success", "data": pts}}).text,
                                           CTX, NOW)
    assert calls == 1 and trial.write_llama_history(conn, NOW, data) == 3
    days = [r[0] for r in conn.execute("SELECT day FROM llama_yield_history ORDER BY day")]
    assert days == ["2026-10-01", "2026-10-02"]                  # 400日より前は消す
    assert conn.execute("SELECT apy_reward FROM llama_yield_history WHERE day='2026-10-01'").fetchone()[0] == 10.0


# --- Lighter の資金調達率の過去 -----------------------------------------------------------------------------

def _market(conn, mid, sym, vol):
    conn.execute("INSERT INTO lighter_markets(market_id, symbol, status, daily_quote_volume, updated_at) "
                 "VALUES (?,?, 'active', ?, 'x')", (mid, sym, vol))


def test_lighter_targets_use_coins_of_registered_opportunities_and_the_busiest_markets(settings, monkeypatch):
    conn = store.connect(settings.database_path)
    monkeypatch.setattr(trial, "LIGHTER_TOP_VOLUME", 1)
    for mid, sym, vol in ((0, "ETH", 1e9), (1, "BTC", 5e8), (2, "NVDA", 1e6), (3, "DOGE", 1e7)):
        _market(conn, mid, sym, vol)
    for i, (chain, toks) in enumerate(((8453, ["WETH", "USDC"]), (4663, ["NVDA", "USDG"]), (1, ["DOGE"]))):
        conn.execute("INSERT INTO feed_items(source, key, first_seen, last_seen, info_json) VALUES "
                     "('merkl_opportunities', ?, 'x', 'x', ?)", (f"o{i}", json.dumps({"chain_id": chain, "tokens": toks})))
    # WETH → ETH（読み替え）、NVDA。DOGE は登録していないチェーンだけなので入らない。取引の多い1つ（ETH）も入る
    assert trial.lighter_targets(conn, CTX) == [(0, "ETH"), (2, "NVDA")]


def test_lighter_history_walks_back_750_points_at_a_time(settings, monkeypatch):
    conn = store.connect(settings.database_path)
    _market(conn, 0, "ETH", 1e9)
    monkeypatch.setattr(trial, "LIGHTER_PAGE", 3)
    start = NOW_S - trial.LIGHTER_DAYS * 86400
    hours = list(range(start + 3600, NOW_S + 1, 3600 * 24 * 10))        # 10日おきの点（テストを小さくするため）

    def routes(url, p):
        lo, hi = int(p["start_timestamp"]), int(p["end_timestamp"])
        pts = [h for h in hours if lo <= h <= hi][-3:]                    # 新しい方から3点だけ返す（本物は750点）
        return {"code": 200, "fundings": [{"timestamp": h, "rate": "0.0012", "value": "0.05", "direction": "long"}
                                          for h in pts]}
    data, calls = trial.read_lighter_history(conn, Fake(routes).text, CTX, NOW)
    assert [p["timestamp"] for p in data["0"]["points"]] == hours and calls >= 3
    assert trial.write_lighter_history(conn, data) == len(hours)
    # 次の日は、前に保存した最後の時刻のあとだけを読む
    f = Fake(routes)
    trial.read_lighter_history(conn, f.text, CTX, NOW + timedelta(days=1))
    assert int(f.calls[0][1]["start_timestamp"]) == hours[-1] + 1


# --- Aero の公式の住所 --------------------------------------------------------------------------------------

LIST_URL = f"https://api.github.com/repos/{trial.AERO_REPO}/contents/{trial.AERO_DIR}"


def test_aero_addresses_from_the_listing_and_new_or_changed_files_are_marked(settings):
    conn = store.connect(settings.database_path)
    src = BY_ID["aero_addresses"]
    arc = {"type": "file", "name": "arc.json", "sha": "s1", "size": 10, "html_url": "u"}
    run_source(conn, src, Fake({LIST_URL: [arc]}), settings, NOW)                       # 最初の回（もとからある）
    base = {"type": "file", "name": "base.json", "sha": "s2", "size": 20, "html_url": "u2"}
    run_source(conn, src, Fake({LIST_URL: [arc, base]}), settings, NOW + timedelta(hours=1))   # Base の住所が出た
    run_source(conn, src, Fake({LIST_URL: [{**arc, "sha": "s9"}, base]}), settings, NOW + timedelta(hours=2))  # 中身が変わった
    conn.close()
    d = feeds_status.status(settings, NOW + timedelta(hours=2, minutes=5))
    by = {a["name"]: a for a in d["aero"]["addresses"]}
    assert by["base.json"]["new"] is True                     # 新しく出た → ホームで知らせる
    assert by["arc.json"]["new"] is True and by["arc.json"]["changed_at"]   # 中身が変わった → 知らせる
    assert any(n["source"] == "aero_addresses" and n["key"] == "base.json" for n in d["new"])


def test_aero_addresses_fall_back_to_checking_known_names(settings):
    def routes(url, p):
        if url == LIST_URL:
            return ExternalError("HTTP 403", 403)                 # 鍵なしの回数制限など
        return '{"factoryRegistry": "0x1"}' if url.endswith("/arc.json") else ExternalError("HTTP 404", 404)
    files, calls = trial.read_aero_addresses(Fake(routes).text)
    assert list(files) == ["arc.json"] and files["arc.json"]["via"] == "guess" and calls == 1 + len(trial.AERO_GUESS)


# --- 影の記録 -----------------------------------------------------------------------------------------------

@dataclass
class _V:
    apr_pct: float
    hedge: bool = False
    net_after_move: float = 1.0

    def to_dict(self):
        return {"apr_pct": self.apr_pct, "hedge": self.hedge}


@dataclass
class _Base:
    key: str
    chain: str = "base"
    name: str = "X/USDC"
    venue: str = "uniswap"
    tvl_usd: float = 1e6
    bonus_usd_per_day: float = 100.0
    bonus_token: str = "UNI"
    days_left: float = 5.0


@dataclass
class _Op:
    base: _Base
    apr: float
    computable: bool = True
    excluded: bool = False
    kind: str = "pool_range"
    cap_usd: float | None = None
    campaigns: list = field(default_factory=list)
    flags: list = field(default_factory=list)

    def best(self, amount, case="cautious"):
        return _V(self.apr if case == "cautious" else self.apr * 1.5)

    def recommended(self, amount, target):
        return not self.excluded and self.apr >= target


def test_shadow_keeps_rows_above_target_and_the_top_rows():
    ops = [_Op(_Base(f"k{i}"), apr) for i, apr in enumerate([90, 50, 31, 20, 10, 5])]
    ops += [_Op(_Base("ex"), 99, excluded=True), _Op(_Base("nc"), 99, computable=False)]
    picked = shadow.select(ops, 30.0, top_n=4)
    assert [o.base.key for _, o in picked] == ["k0", "k1", "k2", "k3"]      # 外したもの・計算できないものは入れない
    assert [o.base.key for _, o in shadow.select(ops, 15.0, top_n=1)] == ["k0", "k1", "k2", "k3"]   # 狙い以上は全部
    rows = shadow.rows(ops, 30.0, "t")
    assert len(rows) == 2 * 6                                    # 上位30行に6つとも入る × 金額2つ
    r = rows[0]
    assert r[1] == "k0" and r[2] == 1000.0 and r[7] == 90 and r[8] == 135 and r[12] == 1
    assert json.loads(r[13])["cautious"]["apr_pct"] == 90


def test_shadow_source_writes_rows_and_a_failure_is_recorded_without_stopping(settings):
    conn = store.connect(settings.database_path)
    ctx = trial.TrialContext(shadow=lambda c, now: {"rows": shadow.rows([_Op(_Base("k0"), 40)], 30.0, "t1")})
    r = run_source(conn, BY_ID["shadow_predictions"], Fake({}), settings, NOW, trial_ctx=ctx)
    assert r["status"] == "ok" and conn.execute("SELECT COUNT(*) FROM shadow_predictions").fetchone()[0] == 2

    def boom(c, now):
        raise KeyError("x")
    r = run_source(conn, BY_ID["shadow_predictions"], Fake({}), settings, NOW, trial_ctx=trial.TrialContext(shadow=boom))
    assert r["status"] == "error" and "影の記録" in r["error"]


def test_hourly_runner_reads_aero_addresses_and_shadow(settings):
    texts = {"https://aero.xyz/articles/index.md": "", BY_ID["lighter_funding"].url: "{}", LIST_URL: "[]"}
    ctx = trial.TrialContext(shadow=lambda c, now: {"rows": []})
    out = FeedRunner(settings, Fake(texts), trial_ctx=ctx).run("hourly", NOW)
    assert [o["source"] for o in out][-2:] == ["aero_addresses", "shadow_predictions"]
    assert all(o["status"] == "ok" for o in out)


# --- 今の版のデータの写し -------------------------------------------------------------------------------------

def _old_db(path: Path, runs=3):
    conn = db.connect(path)
    for i in range(runs):
        conn.execute("INSERT INTO collection_runs(venue_id, slot, started_at, finished_at, status) VALUES "
                     "('up-robinhood', ?, ?, ?, 'ok')", (f"s{i}", f"2026-10-03T00:0{i}:00+00:00",
                                                        f"2026-10-03T00:0{i}:30+00:00"))
    conn.commit()
    return conn


def test_records_summary_reads_the_copy_without_writing(tmp_path, settings):
    data = tmp_path / "data"
    assert trial_records.old_copy(data)["present"] is False
    (data / "import").mkdir(parents=True)
    copy = data / trial_records.OLD_COPY
    _old_db(copy).close()
    sqlite3.connect(copy).execute("PRAGMA journal_mode=DELETE").fetchone()
    (data / trial_records.OLD_MANIFEST).write_text(json.dumps({"integrity": "ok", "health_match": [{"same": True}],
                                                               "copied_at": "2026-10-03T00:10:00+00:00"}))
    before = copy.stat().st_mtime_ns
    d = trial_records.summary(data, settings.database_path)
    assert d["old_copy"]["checked"] is True and d["old_copy"]["tables"]["collection_runs"]["rows"] == 3
    assert copy.stat().st_mtime_ns == before                      # 読むだけ
    assert not (data / "import" / "old-18000.sqlite3-wal").exists()
    store.connect(settings.database_path).close()
    assert trial_records.feeds(settings.database_path)["shadow"]["rows"] == 0


def _check_py() -> str:
    text = (REPO / "scripts/pc/copy-18000.ps1").read_text(encoding="utf-8-sig")
    return re.search(r"\$Check = @'\n(.*?)\n'@", text, re.S).group(1)


def _health(path: Path, last):
    path.write_text(json.dumps({"venues": [{"venue_id": "up-robinhood", "last_ok_at": last}]}))
    return str(path)


def _run_check(tmp_path, a, b, hb, ha):
    script = tmp_path / "check.py"
    script.write_text(_check_py(), encoding="utf-8")
    out = tmp_path / "m.json"
    p = subprocess.run([sys.executable, str(script), str(a), str(b), hb, ha, str(out), str(tmp_path / "none.json")],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, out


def _copies(tmp_path, runs=3):
    src = tmp_path / "live" / "farm_radar.sqlite3"
    src.parent.mkdir()
    conn = _old_db(src, runs)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    dirs = []
    for k in ("a", "b"):
        d = tmp_path / k
        d.mkdir()
        for suf in ("", "-wal"):
            p = Path(str(src) + suf)
            if p.exists():
                (d / ("old.sqlite3" + suf)).write_bytes(p.read_bytes())
        dirs.append(d)
    conn.close()
    return dirs


def test_copy_check_accepts_two_equal_copies_that_match_18000(tmp_path):
    a, b = _copies(tmp_path)
    code, out, manifest = _run_check(tmp_path, a, b, _health(tmp_path / "h1", "2026-10-03T00:02:30+00:00"),
                                     _health(tmp_path / "h2", "2026-10-03T00:02:30+00:00"))
    assert code == 0, out
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["integrity"] == "ok" and m["journal_mode"] == "delete" and m["tables"]["collection_runs"]["rows"] == 3
    assert not (a / "old.sqlite3-wal").exists()                  # 1つのファイルにまとめた


def test_copy_check_retries_when_the_copies_differ_or_18000_moved_on(tmp_path):
    a, b = _copies(tmp_path)
    (b / "old.sqlite3").write_bytes((b / "old.sqlite3").read_bytes() + b"x")      # 2回目の写しの途中に書き込みがあった
    h = _health(tmp_path / "h", "2026-10-03T00:02:30+00:00")
    assert _run_check(tmp_path, a, b, h, h)[0] == 3


def test_copy_check_retries_when_18000_answer_differs_from_the_copy(tmp_path):
    a, b = _copies(tmp_path)
    h1 = _health(tmp_path / "h1", "2026-10-03T00:02:30+00:00")
    assert _run_check(tmp_path, a, b, h1, _health(tmp_path / "h2", "2026-10-03T00:05:30+00:00"))[0] == 3   # 写す間に1回終わった
    a2, b2 = (tmp_path / "x"), (tmp_path / "y")
    for src, dst in ((a, a2), (b, b2)):
        dst.mkdir()
        for p in src.iterdir():
            (dst / p.name).write_bytes(p.read_bytes())
    late = _health(tmp_path / "h3", "2026-10-03T00:09:30+00:00")                  # 写しより新しい回を今の版が答えた
    assert _run_check(tmp_path, a2, b2, late, late)[0] == 3


def test_copy_check_stops_on_a_broken_copy(tmp_path):
    a, b = _copies(tmp_path)
    for d in (a, b):
        raw = bytearray((d / "old.sqlite3").read_bytes())
        raw[100:4096] = b"\xff" * (4096 - 100)                    # 中身を壊す
        (d / "old.sqlite3").write_bytes(bytes(raw))
        w = d / "old.sqlite3-wal"
        if w.exists():
            w.unlink()
    h = _health(tmp_path / "h", "2026-10-03T00:02:30+00:00")
    code, out, _ = _run_check(tmp_path, a, b, h, h)
    assert code in (1, 4), out                                    # 壊れていれば置かない（0 にはならない）
