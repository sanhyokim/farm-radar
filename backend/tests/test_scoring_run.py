import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from farm_radar.config import ScoringSettings
from farm_radar.db import database as db
from farm_radar.external.geckoterminal import PoolMarket, TokenMarket
from farm_radar.scoring.judge import NO_HEDGE_TEXT
from farm_radar.scoring.prices import Q96
from farm_radar.scoring.run import EXTERNAL_SOURCE, ScoreContext, score_venue
from farm_radar.tokens import PerpRef, TokenBook

USDG, WETH, UP, NVDA = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20
T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)

VENUE = {
    "id": "up-robinhood",
    "contracts": {"reward_token": {"address": UP, "unverified": False}},
    "warnings": [{"code": "C4", "level": "minor", "title_ja": "報酬の上限の仕組みが非公開"}],
}
TOKENS = TokenBook(
    stablecoins=frozenset({USDG}), stock_tokens={NVDA: "NVDA"},
    perps={WETH: PerpRef("lighter", "ETH", 0), NVDA: PerpRef("lighter", "NVDA", 110)},
    wrapped_native=WETH,
)
# (プール, token0, token1, 桁0, 桁1, 基準の価格 token1/token0, 1時間ごとの揺れ)
POOLS = [
    ("p-weth", WETH, USDG, 18, 6, 2500.0, 0.004),
    ("p-up", UP, WETH, 18, 18, 0.0001, 0.01),
    ("p-nvda", USDG, NVDA, 6, 18, 1 / 200, 0.002),
]


class FakeLighter:
    def short_funding_hourly(self, market_id, start, end):
        return [(t, 0.00001) for t in range(start, end, 3600)]


class FakeRpc:
    def gas_price(self):
        return 10 ** 8   # 0.1 gwei


class FakeGT:
    def __init__(self):
        self.ohlcv_calls = 0

    def pools(self, addresses):
        return {a: PoolMarket(a, 500_000.0, 200_000.0, None, None) for a in addresses}

    def tokens(self, addresses):
        return {a: TokenMarket(a, None, 1_000_000.0, "0xtop" + a[-4:]) for a in addresses}

    def hourly_usd(self, pool, token, limit=169):
        self.ohlcv_calls += 1
        end = int(T0.timestamp()) + 8 * 86400
        base = {WETH: 2500.0, UP: 0.25, NVDA: 200.0}[token]
        return [(end - i * 3600, base * math.exp(0.005 * (-1) ** i)) for i in range(limit)][::-1]


def _fill(conn, hours):
    conn.execute("INSERT INTO venues(id, name, chain) VALUES ('up-robinhood', 'up.', 'robinhood')")
    for pid, t0, t1, d0, d1, _p, _w in POOLS:
        conn.execute(
            """INSERT INTO pools(id, venue_id, address, token0, token1, discovered_at, token0_symbol, token1_symbol,
                 token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"up-robinhood:{pid}", "up-robinhood", pid, t0, t1, T0.isoformat(),
             {USDG: "USDG", WETH: "WETH", UP: "UP", NVDA: "NVDA"}[t0],
             {USDG: "USDG", WETH: "WETH", UP: "UP", NVDA: "NVDA"}[t1], d0, d1),
        )
    start = T0 + timedelta(days=8) - timedelta(hours=hours)
    for h in range(hours + 1):
        ts = start + timedelta(hours=h)
        run_id = db.start_run(conn, "up-robinhood", ts, ts)
        for pid, t0, t1, d0, d1, p, wig in POOLS:
            price = p * math.exp(wig * (-1) ** h)
            sp = math.sqrt(price * 10 ** (d1 - d0))
            db.insert_snapshot(conn, {
                "pool_id": f"up-robinhood:{pid}", "ts": ts.isoformat(timespec="seconds"), "block_number": 1000 + h,
                "run_id": run_id, "price": price, "tick": 0, "sqrt_price_x96": str(int(sp * Q96)), "fee": 3000,
                "liquidity_total": str(10 ** 18), "liquidity_staked_inrange": str(5 * 10 ** 17),
                "reward_rate_raw": str(10 ** 18), "reward_rate_effective_raw": str(10 ** 18),
                "reward_token": UP, "gauge_alive": 1, "unstaked_fee": 100000, "epoch_just_flipped": 0,
                "block_time": ts.isoformat(timespec="seconds"), "source": "test",
            })
        db.finish_run(conn, run_id, now=ts, status="ok", block_number=1000 + h, pools_ok=3, pools_failed=0)
    conn.commit()


def _ctx(gt=None, **kw):
    return ScoreContext(venue=VENUE, tokens=TOKENS, settings=ScoringSettings(**kw), stale_after_minutes=45,
                        rpc=FakeRpc(), gt=gt, lighter=FakeLighter())


NOW = T0 + timedelta(days=8, minutes=10)


def test_scores_every_pool_with_own_data():
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    rows = {r["pool_id"].split(":")[1]: r for r in score_venue(conn, _ctx(), now=NOW)}
    assert set(rows) == {"p-weth", "p-up", "p-nvda"}
    for r in rows.values():
        assert r["signal"] in ("green", "yellow", "red")
        assert r["reason_ja"] and len(r["reason_ja"].splitlines()) <= 3
        assert r["vol_source"] == "own"
        assert r["net_daily_pct"] is not None
    # 揺れの大きさから σ が出る（1時間 ±0.4% → 1日 約 0.4% × √24 × 2 の往復）
    assert rows["p-weth"]["sigma_pair"] == pytest.approx(0.008 * math.sqrt(24), rel=0.02)
    # ステーブルの σ は0
    assert rows["p-weth"]["sigma_token1"] == 0.0
    # UP はヘッジ先がない → 値動きの損を引き、判定は最高でも🟡、理由に必ず書く
    assert rows["p-up"]["has_perp"] == 0
    assert rows["p-up"]["direction_risk"] > 0 and rows["p-up"]["signal"] != "green"
    assert NO_HEDGE_TEXT in rows["p-up"]["reason_ja"]
    # WETH/USDG はヘッジできる → 値動きの損は0、ヘッジ費用を引く
    assert rows["p-weth"]["has_perp"] == 1 and rows["p-weth"]["direction_risk"] == 0
    assert rows["p-weth"]["hedge"] > 0
    # 軽微な警告（C4）があるので、どのプールも🟢にはならない
    assert all(r["signal"] != "green" for r in rows.values())
    # 詳細にはレンジ幅ごとの結果と、参考値（置きっぱなし）が入る
    det = json.loads(rows["p-weth"]["details_json"])
    assert len(det["ranges"]) == 7
    assert all("in_range_ratio_hold" in x for x in det["ranges"])
    # 両替のずれはプールの流動性から計算し、レンジごとにプールの流動性と自分の取り分を残す
    assert det["inputs"]["slippage_source"] == "pool" and det["inputs"]["slippage"] > 0
    assert det["ranges"][0]["pool_inrange_usd"] > det["ranges"][0]["staked_inrange_usd"] > 0
    assert 0 < det["ranges"][0]["share_staked"] < 1
    # 株トークンのペアは、市場時間中と時間外の σ も記録する
    assert "NVDA" in json.loads(rows["p-nvda"]["details_json"])["inputs"]["sigma_stock_split"]
    assert conn.execute("SELECT is_stock_pair FROM pools WHERE id='up-robinhood:p-nvda'").fetchone()[0] == 1
    assert len(db.latest_scores(conn)) == 3


def test_short_history_uses_external_data_and_says_so():
    conn = db.connect(":memory:")
    _fill(conn, 24)            # まだ1日分しかない
    gt = FakeGT()
    rows = score_venue(conn, _ctx(gt), now=NOW)
    assert all(r["vol_source"] == "external" for r in rows)
    assert all("外部データ" in r["reason_ja"] for r in rows)
    assert gt.ohlcv_calls == 3   # WETH, UP, NVDA
    assert conn.execute("SELECT COUNT(*) FROM token_prices WHERE source=?", (EXTERNAL_SOURCE,)).fetchone()[0] > 0
    # 取り直す間隔の中なら、ためた足を使って外部サイトには聞かない
    score_venue(conn, _ctx(gt), now=NOW + timedelta(minutes=30))
    assert gt.ohlcv_calls == 3


def test_stale_data_is_red():
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    rows = score_venue(conn, _ctx(), now=NOW + timedelta(hours=3))
    assert all(r["signal"] == "red" and "古い" in r["reason_ja"] for r in rows)


def test_reward_token_crash_is_major():
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    # UP が7日で半分になった形にする（UP/WETH の価格を途中から下げる）
    conn.execute("""UPDATE pool_snapshots SET price = price * 0.5
                    WHERE pool_id='up-robinhood:p-up' AND ts >= ?""", ((T0 + timedelta(days=4)).isoformat(),))
    rows = {r["pool_id"].split(":")[1]: r for r in score_venue(conn, _ctx(), now=NOW)}
    assert rows["p-weth"]["signal"] == "red"
    assert "報酬トークンが7日で" in rows["p-weth"]["reason_ja"]


def test_merge_prefers_own_data_and_fills_older_hours_from_external():
    from farm_radar.scoring.run import merge_series
    ext = [(1, 10.0), (2, 11.0), (3, 12.0), (4, 13.0)]
    own = [(3, 99.0), (4, 98.0)]
    assert merge_series(ext, own) == [(1, 10.0), (2, 11.0), (3, 99.0), (4, 98.0)]
    assert merge_series(ext, []) == ext


def test_external_data_fetched_once_then_own_data_takes_over():
    conn = db.connect(":memory:")
    _fill(conn, 24)
    gt = FakeGT()
    score_venue(conn, _ctx(gt), now=NOW)
    assert gt.ohlcv_calls == 3
    # 次の回では、ためた足と自分の記録で7日分がそろっているので、外部サイトには聞き直さない
    score_venue(conn, _ctx(gt), now=NOW + timedelta(minutes=10))
    assert gt.ohlcv_calls == 3


def test_slippage_falls_back_when_it_cannot_be_computed(monkeypatch):
    from farm_radar.scoring import run
    monkeypatch.setattr(run, "swap_price_impact", lambda *a: None)
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    rows = {r["pool_id"].split(":")[1]: json.loads(r["details_json"])["inputs"]
            for r in score_venue(conn, _ctx(), now=NOW)}
    # ステーブルと株トークンだけのプールは 0.1%、それ以外は 1%
    assert rows["p-nvda"]["slippage_source"] == "fallback" and rows["p-nvda"]["slippage"] == pytest.approx(0.001)
    assert rows["p-weth"]["slippage"] == pytest.approx(0.01)


def _latest_run(conn):
    return conn.execute("SELECT MAX(run_id) FROM pool_snapshots").fetchone()[0]


def test_others_liquidity_is_larger_of_24h_median_and_latest():
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    last = _latest_run(conn)
    # 最新の回だけ、ステーク分がたまたま少ない（大口がレンジの外にいた瞬間）→ 24時間の中央値を使う
    conn.execute("UPDATE pool_snapshots SET liquidity_staked_inrange=? WHERE run_id=? AND pool_id='up-robinhood:p-weth'",
                 (str(10 ** 15), last))
    # 最新の回だけ、ステーク分が多い → 最新の値を使う
    conn.execute("UPDATE pool_snapshots SET liquidity_staked_inrange=? WHERE run_id=? AND pool_id='up-robinhood:p-up'",
                 (str(9 * 10 ** 17), last))
    rows = {r["pool_id"].split(":")[1]: json.loads(r["details_json"])["inputs"]
            for r in score_venue(conn, _ctx(), now=NOW)}
    assert rows["p-weth"]["liquidity_staked_inrange"] == str(5 * 10 ** 17)
    assert rows["p-weth"]["liquidity_latest"]["staked"] == str(10 ** 15)
    assert rows["p-weth"]["liquidity_median_24h"]["staked"] == str(5 * 10 ** 17)
    assert rows["p-up"]["liquidity_staked_inrange"] == str(9 * 10 ** 17)


class WashGT(FakeGT):
    def pools(self, addresses):
        return {a: PoolMarket(a, 10_000.0, 250_000.0, None, None) for a in addresses}   # 取引量がTVLの25倍


def test_unnatural_volume_counts_no_fee_income_and_is_warned():
    # 2026-09-29 オーナー決定: 見せかけの取引の警告が出たら、手数料の収入は0（TVLの10倍までに抑えるルールを置き換え）
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    rows = score_venue(conn, _ctx(WashGT()), now=NOW)
    for r in rows:
        det = json.loads(r["details_json"])
        inp = det["inputs"]
        assert inp["volume_24h_usd"] == 250_000.0 and inp["volume_used_usd"] == 0.0
        assert inp["fees_usd_day"] == 0.0
        assert all(not x["income_unstaked"] for x in det["ranges"])
        assert r["mode"] == "staked"
        assert any(w["code"] == "VOL" for w in json.loads(r["warnings_json"]))
        assert "手数料の収入は0" in r["reason_ja"] and len(r["reason_ja"].splitlines()) <= 3
    # しきい値（倍率）は設定で変えられる。超えていなければ取引量そのままで計算する
    rows = score_venue(conn, _ctx(WashGT(), volume_suspicious_tvl_multiple=30), now=NOW)
    assert all(json.loads(r["details_json"])["inputs"]["volume_used_usd"] == 250_000.0 for r in rows)


def test_manual_bonus_is_shown_apart_and_not_judged():
    # 2026-09-30 オーナー条件4: 運営が手で足したボーナスは、いつものボーナスと分けて見せ、判定には入れない
    conn = db.connect(":memory:")
    _fill(conn, 24 * 8)
    base = {r["pool_id"]: r for r in score_venue(conn, _ctx(), now=NOW)}
    last = conn.execute("SELECT MAX(run_id) FROM pool_snapshots").fetchone()[0]
    # WETH/USDG に、今週の合計 700,000 のうち 150,000 が手で足された（いつもの分のレートは変えない）
    conn.execute("UPDATE pool_snapshots SET reward_epoch_total_raw=?, reward_manual_raw=? WHERE run_id=? AND pool_id=?",
                 (str(700_000 * 10 ** 18), str(150_000 * 10 ** 18), last, "up-robinhood:p-weth"))
    conn.commit()
    rows = {r["pool_id"]: r for r in score_venue(conn, _ctx(), now=NOW + timedelta(minutes=1))}
    weth = rows["up-robinhood:p-weth"]
    # 判定の数字は変わらない
    assert weth["net_daily_pct"] == pytest.approx(base["up-robinhood:p-weth"]["net_daily_pct"])
    assert weth["signal"] == base["up-robinhood:p-weth"]["signal"]
    m = json.loads(weth["details_json"])["inputs"]["manual_bonus"]
    assert m["amount"] == pytest.approx(150_000) and m["regular"] == pytest.approx(550_000)
    token_usd = json.loads(weth["details_json"])["inputs"]["reward_token_usd"]
    assert m["usd"] == pytest.approx(150_000 * token_usd)
    assert m["usd_day_spread"] == pytest.approx(m["usd"] / 7)
    assert m["your_extra_usd_day"] > 0
    assert "手で足した" in " ".join(json.loads(weth["details_json"])["inputs"]["notes"])
    # 手で足した分がないプールは None
    assert json.loads(rows["up-robinhood:p-nvda"]["details_json"])["inputs"]["manual_bonus"] is None
