"""N5 最終判断の準備（backtest の prep と final_prep。2026-10-04 18:20 JST オーナーの指示書）。

- 材料を数えるだけで、決めない・おすすめを付けない・設定を変えない。
- 状態は 判断できる / 記録待ち / 材料不足 の3つ。材料不足には「何が足りないか・今から記録が要るか・過去の公開記録で足りるか」。
"""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from farm_radar import backtest as bt
from farm_radar import final_prep as fp
from farm_radar.feeds import store

from .test_n5b import CFG, START, STEP, _pts, feeds, old_copy  # noqa: F401


# --- さかのぼりに足した数え方 ------------------------------------------------------------------------------

def test_replay_buffer_zero_is_the_old_rule_and_a_buffer_waits_longer():
    prices = [100.0, 102.5, 102.5, 102.5, 102.5, 102.5]
    a = bt.replay(_pts(prices), 0.02, 900)
    b = bt.replay(_pts(prices), 0.02, 900, buffer=0.0)
    assert [d.rebalances for d in a.values()] == [d.rebalances for d in b.values()] == [1]
    # 余裕 15%（幅 ±2% = 4 の 15% = 0.6）: 102.5 は 102 + 0.6 = 102.6 の内側なので置き直さない
    c = bt.replay(_pts(prices), 0.02, 900, buffer=0.15)
    assert sum(d.rebalances for d in c.values()) == 0
    # 幅の中にいた時間は、余裕を入れない本当の幅で数える（102.5 は幅の外）
    assert next(iter(c.values())).in_range_seconds < next(iter(c.values())).seconds


def test_replay_counts_back_and_forth_within_an_hour():
    # 置き直した直後に前の幅へ戻った = 行ったり来たり 1回
    d = next(iter(bt.replay(_pts([100.0, 103.0, 103.0, 101.0, 101.0]), 0.02, 900).values()))
    assert d.rebalances == 1 and d.flips == 1
    # 1時間より後に戻ったものは数えない
    late = [100.0, 103.0, 103.0] + [103.5] * 5 + [101.0]
    d = next(iter(bt.replay(_pts(late), 0.02, 900).values()))
    assert d.rebalances == 1 and d.flips == 0


def test_events_have_six_hours_after():
    pts = [(START + i * STEP, 100.0 if i < 4 else 80.0 if i < 30 else 90.0) for i in range(200)]
    evs = bt.events(bt.changes(pts, 1), lambda ch: ch * 100 <= -15, pts)
    assert len(evs) == 1
    assert evs[0]["after_6h_pct"] == pytest.approx(0.0) and evs[0]["after_24h_pct"] == pytest.approx(12.5)


def test_loss_series_day_week_and_since_start():
    rows = [{"pool_id": "a", "day": f"2026-09-{d:02d}", "now": v} for d, v in zip(range(1, 9), (-10, 5, -20, -5, 0, 1, 2, 3))]
    rows += [{"pool_id": "b", "day": "2026-09-01", "now": 4.0}]
    out = bt.loss_series(rows, 1000.0)
    assert len(out["day"]) == 9 and min(out["day"]) == -2.0
    # 7日続いた窓は a の2つ（1〜7日・2〜8日）。b は1日だけ
    assert out["week"] == [pytest.approx(-2.7), pytest.approx(-1.4)]
    # 始めてから: a は −10 → −5 → −25 → −30 がいちばん悪い（−3%）、b は 0
    assert sorted(out["since_start"]) == [-3.0, 0.0] and out["calendar_days"] == 8


def test_stage1_table_shares():
    def ev(small, rec, big, f24):
        return {"small": small, "recovered_6h": rec, "big_drop": big, "funds_after_24h_pct": f24}
    t = bt.stage1_table({30: [ev(False, True, False, 5.0), ev(False, False, True, -20.0), ev(True, True, False, None)],
                         50: []})
    r30 = t[0]
    assert r30["threshold_pct"] == 30 and r30["all"]["count"] == 3 and r30["big"]["count"] == 2
    assert r30["all"]["recovered_6h_share"] == pytest.approx(2 / 3)
    assert r30["all"]["worse_24h_share"] == 0.5 and r30["all"]["worse_24h_known"] == 2
    assert r30["all"]["false_alarm_share"] == pytest.approx(2 / 3)
    assert t[1]["all"]["count"] == 0 and t[1]["all"]["recovered_6h_share"] is None


def _sc(t, net):
    return (t, {"net_daily_pct": net})


def test_below_target_counts_only_after_being_above():
    hi, lo = 0.1, 0.05                 # 年 36.5% と 18.25%（狙い 30%）
    seq = [_sc(START + i * STEP, v) for i, v in enumerate([lo, lo, lo, hi, lo, lo, lo, lo, hi])]
    evs = bt.below_target_events(seq, 30.0, 3)
    assert len(evs) == 1                # 最初の3回は、前に狙い以上でなかったので数えない
    assert evs[0]["at"] == START + 6 * STEP and evs[0]["back_6h"] is True


def test_move_sim_moves_only_when_the_gain_beats_the_multiple():
    info = {"a": {d: {"net": 0.05, "side_cost": 1.0} for d in ("2026-10-01", "2026-10-02", "2026-10-03")},
            "b": {d: {"net": 0.10, "side_cost": 1.0} for d in ("2026-10-02", "2026-10-03")}}
    now = {("a", "2026-10-01"): 1.0, ("a", "2026-10-02"): 1.0, ("a", "2026-10-03"): 1.0,
           ("b", "2026-10-02"): 3.0, ("b", "2026-10-03"): 3.0}
    flip = {"b": {"2026-10-02": 5.0, "2026-10-03": 4.0}}
    s = bt.BacktestSettings(target_apr_pct=10.0)
    # 差 0.05% × $1,000 × 5日 = $2.5、移る費用 $2 → 1倍なら移る、1.5倍（$3）なら移らない
    one = bt.move_sim(info, now, flip, s, 1.0)
    assert one["moves"] == 1 and one["move_cost_usd"] == 2.0 and one["pnl_usd"] == pytest.approx(1 + 3 + 3 - 2)
    assert bt.move_sim(info, now, flip, s, 1.5)["moves"] == 0
    stay = bt.move_sim(info, now, flip, s, None)
    assert stay["moves"] == 0 and stay["pnl_usd"] == 3.0


def test_backtest_run_has_the_prep(old_copy, feeds):  # noqa: F811
    ranges = [{"r_pct": r, "income": 10.0 / r * 0.8, "in_range_ratio": 0.8} for r in CFG.scoring.ranges_pct]
    det = json.loads(old_copy.execute("SELECT details_json FROM scores LIMIT 1").fetchone()[0])
    old_copy.execute("UPDATE scores SET details_json=?, net_daily_pct=0.1", (json.dumps({**det, "ranges": ranges}),))
    old_copy.commit()
    res = bt.run(old_copy, CFG, feeds)
    pr = res["prep"]
    assert set(pr) == {"loss", "stage1", "below_target", "moves", "grid", "gas"}
    assert pr["loss"]["calendar_days"] == 3 and len(pr["loss"]["day"]) == 3
    assert [r["threshold_pct"] for r in pr["stage1"]] == [20, 30, 40, 50] and pr["stage1"][1]["all"]["count"] == 1
    assert len(pr["grid"]["rows"]) == 12 and all(r["pool_days"] == 3 for r in pr["grid"]["rows"])
    assert pr["gas"]["median"] == pytest.approx(0.15) and pr["gas"]["points"] > 0
    assert [r["multiple"] for r in pr["moves"]["runs"]] == [None, 1.0, 1.5, 2.0, 3.0]
    # 置き直しの今の決まり（余裕 0・15分）は、今のやり方と同じ数え方
    cur = next(r for r in pr["grid"]["rows"] if r["buffer"] == 0 and r["wait_min"] == 15)
    assert cur["net_usd_day"] == pytest.approx(sum(x / 3 for x in pr["loss"]["day"]) * 10, abs=1e-3)  # 損の分布は小数4桁で丸める


# --- final_prep -------------------------------------------------------------------------------------------

def _bt(days):
    ds = [f"2026-10-{d:02d}" for d in range(1, days + 1)]
    grid = [{"buffer": b, "wait_min": w, "pool_days": 10, "rebalances_day": 1.0, "flips_day": 0.2, "flip_share": 0.2,
             "cost_usd_day": 1.0, "out_share": 0.05, "net_usd_day": -1.0, "net_usd_total": -10.0, "calendar_days": days}
            for b in bt.GRID_BUFFERS for w in bt.GRID_WAITS_MIN]
    runs = [{"multiple": k, "moves": 0 if k is None else 2, "move_cost_usd": 0.0 if k is None else 3.0,
             "pnl_before_cost_usd": 10.0, "pnl_usd": 10.0 if k is None else 7.0, "days": days, "missing_days": 0}
            for k in (None, *bt.MOVE_MULTIPLES)]
    return {"present": True, "computed_at": "2026-10-04T09:00:00+00:00", "result": {
        "days": ds,
        "ranges": {"all": [{"r_pct": 0.5, "rebalances": {"pred_median": 561.0, "real_median": 14.0}},
                           {"r_pct": 2.0, "rebalances": {"pred_median": 2.0, "real_median": 1.0}}]},
        "stage2_reward": {"tokens": [{"events": {"-15": [{"after_6h_pct": -2.0, "after_24h_pct": -10.0},
                                                         {"after_6h_pct": 1.0, "after_24h_pct": 4.0}]}}]},
        "stage2_dump": {"tokens": [{"events_1h": [{"after_6h_pct": 0.0, "after_24h_pct": -5.0}], "events_24h": []}]},
        "prep": {"loss": {"day": [-6.0, -4.5, -3.5, -1.0, 0.5, 1.0, 2.0, 3.0] * 5, "week": [-8.0, -2.0],
                          "since_start": [-12.0, -1.0], "pools": 5, "calendar_days": days},
                 "stage1": bt.stage1_table({20: [], 30: [{"small": False, "recovered_6h": True, "big_drop": False,
                                                          "funds_after_24h_pct": 1.0}], 40: [], 50: []}),
                 "below_target": {"target_apr_pct": 30.0, "times": 3, "events": [{"back_6h": True, "back_24h": True}],
                                  "next_day": {"known": 1, "avoided_n": 0, "missed_n": 1, "avoided_mean_usd": None,
                                               "missed_mean_usd": 2.0}},
                 "moves": {"runs": runs, "pools": 3}, "grid": {"rows": grid, "flip_s": 3600},
                 "gas": {"points": 700, "first": "2026-09-27T00:00:00+00:00",
                         "last": f"2026-10-{days:02d}T00:00:00+00:00", "median": 0.1, "p90": 0.2, "p99": 0.5, "max": 1.0}}}}


def _text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def test_short_records_wait_and_nothing_is_decided():
    out = fp.build(_bt(3), None, {}, CFG, {})
    items = {x["key"]: x for x in out["items"]}
    assert [x["no"] for x in out["items"]] == list(range(1, 11))
    for k in ("loss_lines", "stage1", "stage2", "stage4", "edge"):
        assert items[k]["state"]["code"] == "wait" and "あと 4 日" in items[k]["state"]["why"]
    assert items["pool_share"]["state"]["code"] == "wait"
    assert items["risk"]["state"]["code"] == "lack" and items["risk"]["missing"]["new_recording"] is False
    assert items["reserve"]["state"]["code"] == "lack" and "Base" in items["reserve"]["state"]["why"]
    assert out["rebalance"]["state"]["code"] == "wait"
    assert all(r[-1] == "7日後に出ます" for r in out["rebalance"]["table"]["rows"])
    assert "おすすめ" not in _text({k: v for k, v in out.items() if k != "notes"})   # おすすめは指示役が出す
    assert {n["title"] for n in out["n6"]} == {"控えめのほかの人のお金 1.5倍", "中身がステーブルの預かり証 月−3%"}
    assert out["ready_on"] == "10/08"
    # 材料不足のものには「足りないもの・新しい記録が要るか・過去の公開記録で足りるか」
    for x in out["items"]:
        if x["state"]["code"] == "lack":
            assert x["missing"] and {"what", "new_recording", "past_public", "how"} <= set(x["missing"])


def test_seven_days_make_the_loss_and_stage_items_ready_and_the_rebalance_candidates_appear():
    out = fp.build(_bt(7), None, {}, CFG, {})
    items = {x["key"]: x for x in out["items"]}
    for k in ("stage1", "stage2", "stage4", "edge"):
        assert items[k]["state"]["code"] == "ready"
    # 1日は判断できる、1週間・始めてからは14日まで待つので、項目全体は記録待ち
    assert items["loss_lines"]["state"]["code"] == "wait" and "1日の線は判断できます" in items["loss_lines"]["state"]["why"]
    day = items["loss_lines"]["tables"][1]["rows"]
    assert day[0][0] == "注意 -3%" and day[0][1] == "38%"          # 40 件のうち −3% 以下は 15 件
    assert out["rebalance"]["state"]["code"] == "ready"
    assert out["rebalance"]["table"]["rows"][0][-1] == "今の式 × 0.02"   # ±0.5%: 実際 14 ÷ 見込み 561
    s4 = items["stage4"]["tables"][0]
    assert s4["mark"] == [2] and s4["rows"][2][0] == "2倍" and s4["rows"][-1][0] == "移らない"
    g = items["edge"]["tables"][0]
    marked = [g["rows"][i][0] for i in g["mark"]]
    assert marked == ["0%（今の置き直し）", "10%（今の保険の直し）"]
    assert "おすすめ" not in _text({k: v for k, v in out.items() if k != "notes"})


def _ab(pairs: int, days: float) -> dict:
    st = {"median": -0.1, "p25": -0.2, "p75": 0.05, "within": 0.9}
    camp = {"campaign_id": "c1", "pair": "WETH/USDG", "chain_id": 4663, "out_class": "in", "out_label": "幅の中だけ",
            "weights": {"fee": 0.7, "token0": 0.15, "token1": 0.15}, "main_weight": "手数料が中心", "intervals": 3,
            "intervals_l_ok": 3, "pairs": pairs, "positions": 10, "unusable_positions": 1, "n_all": 12.0, "n_in": 9.0,
            "a_den_v": 1.0, "b_den_v": 0.8, "ratio": 0.8, "share_backcalc": 0.82, "share_chain": 0.8,
            "share_backcalc_now": 0.5, "err_a": -0.1, "err_b": 0.01, "tvl": 1000.0, "skipped": {}, "unusable": {}}
    return {"present": True, "overall": {"pairs": pairs, "campaigns": 1, "days": days, "a": st, "b": st},
            "by_weight": {"手数料が中心": {"pairs": pairs, "a": st, "b": st}}, "campaigns": [camp],
            "parts": {"fee": {"label": "手数料", "b": "A と同じ", "share_of_pred_a": 0.9}},
            "skipped": {"区切りの間に預け方が変わった（足す・減らす・閉じる・作る）": 2}, "unusable": {"名前の形が違う": 1},
            "out_classes": {"幅の中だけ": 1},
            "cap": [{"pair": "WETH/USDG", "tvl": 1000.0, "ratio": 0.8, "b_den": 800.0,
                     "a": {100: 100 / 1100, 500: 500 / 1500}, "b": {100: 100 / 900, 500: 500 / 1300}}],
            "own_usd": [100, 500]}


def test_merkl_ab_waits_then_lines_up_a_b_and_actual():
    """2026-10-04 指示書: A（全員）と B（幅の中だけ）と実際を、同じ区切り・同じ預け方で並べる。決めない。"""
    early = fp.merkl_ab(None, _ab(10, 1.0), {"pools": 1})
    assert early["state"]["code"] == "wait" and early["missing"] is None
    m = fp.merkl_ab(None, _ab(40, 4.0), {"pools": 23, "caught_up": 23, "events": 316000})
    assert m["state"]["code"] == "ready"
    assert m["table"]["rows"][0][:2] == ["A（全員が分母。今の式）", "-10.0%"] and m["table"]["rows"][1][4] == "90%"
    titles = [t["title"] for t in m["tables"]]
    assert "5% 上限の準備: 自分の額が分母の何%か" in titles and "使わなかった区切り・預け方と理由" in titles
    cap = next(t for t in m["tables"] if t["title"].startswith("5% 上限"))
    assert cap["head"][:4] == ["組み合わせ", "B÷A", "$100 A", "$100 B"] and cap["rows"][0][2:4] == ["9.09%", "11.11%"]
    camp = next(t for t in m["tables"] if t["title"] == "キャンペーンごと")["rows"][0]
    assert camp[0] == "WETH/USDG" and camp[7] == "0.80" and camp[8] == "0.50"      # B÷A と、今の答え合わせの逆算
    # チェーンの記録をまだ読んでいない
    assert fp.merkl_ab(None, {"present": False}, None)["state"]["code"] == "wait"


def test_pool_share_uses_the_chain_counted_ratio():
    rows = {r[0]: r for r in fp.pool_share(_ab(40, 4.0), 4.0, 0.05)["tables"][0]["rows"]}
    r = rows["WETH/USDG（チェーンで数えた）"]
    assert r[1] == "0.80" and r[2] == "4.76%" and r[3] == "5.88%"           # A は 5/105、d = 0.8 なら B は 5/85


def _feeds(tmp_path):
    return store.connect(tmp_path / "feeds.sqlite3")


def test_hedge_rise_lists_floor_and_market_values(tmp_path):
    conn = _feeds(tmp_path)
    t0 = START
    for h in range(24 * 40):              # ETH（market 0）は +10%、NBIS（189）は +80%
        for mid, sym, k in ((0, "ETH", 0.10), (189, "NBIS", 0.80)):
            p = 100.0 * (1 + k * min(1.0, h / 100))
            conn.execute("INSERT INTO lighter_price_history(market_id, ts, symbol, open, high, low, close) VALUES "
                         "(?,?,?,?,?,?,?)", (mid, t0 + h * 3600, sym, p, p, p, p))
    conn.commit()
    x = fp.hedge_rise(conn, CFG)
    assert x["state"]["code"] == "ready"
    rows = {r[0]: r for r in x["tables"][0]["rows"]}
    assert rows["NBIS"][3] == "80.0%" and rows["NBIS"][4] == "市場の上げ"
    assert rows["ETH"][3] == "50.0%" and rows["ETH"][4] == "床（50%）"
    empty = fp.hedge_rise(_feeds(tmp_path / "e"), CFG) if (tmp_path / "e").mkdir() is None else None
    assert empty["state"]["code"] == "lack" and empty["missing"]["new_recording"] is False


def test_bonus_drop_needs_twenty_coins_with_thirty_days(tmp_path):
    conn = _feeds(tmp_path)
    now = int(datetime(2026, 10, 4, tzinfo=UTC).timestamp())
    for i in range(25):
        addr = f"0x{i:040x}"
        conn.execute("INSERT INTO merkl_campaigns(campaign_id, chain_id, distribution_chain_id, reward_address, first_seen, "
                     "last_seen) VALUES (?, 8453, 8453, ?, '2026-10-01', '2026-10-01')", (f"c{i}", addr))
        drop = -0.01 * i                   # 0% 〜 −24%（30日）
        for h in range(0, 30 * 24 + 1, 4):
            t = now - 30 * 86400 + h * 3600
            conn.execute("INSERT INTO token_prices(coin, ts, price) VALUES (?,?,?)",
                         (f"base:{addr}", t, 2.0 * (1 + drop * h / (30 * 24))))
    conn.commit()
    x = fp.bonus_drop(conn, CFG, {8453: "base"})
    assert x["state"]["code"] == "ready"
    row30 = next(r for r in x["tables"][0]["rows"] if r[0] == "直近30日")
    assert row30[1] == "25" and row30[5] == "-24.0%"
    few = fp.bonus_drop(conn, CFG, {})
    assert few["state"]["code"] == "lack" and few["missing"]["past_public"] is True


def test_risk_counts_only_what_was_known_before_the_incident(tmp_path):
    conn = _feeds(tmp_path)
    day = 86400
    t = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    for key, info in (("x-protocol", {"id": "1", "listed_at": t - 10 * day}), ("y-protocol", {"id": "2", "listed_at": t - 400 * day})):
        conn.execute("INSERT INTO feed_items(source, key, name, info_json, first_seen, last_seen) VALUES "
                     "('llama_protocols', ?, ?, ?, '2026-10-01', '2026-10-01')", (key, key, json.dumps(info)))
    for i, (pid, date) in enumerate((("1", t), ("2", t), ("2", t - 100 * day), ("9", t))):
        conn.execute("INSERT INTO feed_items(source, key, name, info_json, first_seen, last_seen) VALUES "
                     "('llama_hacks', ?, 'h', ?, '2026-10-01', '2026-10-01')",
                     (f"h{i}", json.dumps({"date": date, "defillama_id": pid})))
    conn.commit()
    x = fp.risk_score(conn, CFG)
    assert x["state"]["code"] == "lack"
    rows = {r[0]: r[1] for r in x["tables"][0]["rows"]}
    assert rows["事件の一覧（DefiLlama）"] == "4 件" and rows["会場の記録と結びつけられた事件"] == "3 件"
    assert rows["事件のとき、載ってから30日より短い（年齢の点 20）"] == "33%"
    assert rows["前の1年に別の事件があった（事件の点 25）"] == "33%"


def test_api_trial_final(tmp_path, old_copy, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from farm_radar import api

    data = tmp_path / "data"
    (data / "import").mkdir(parents=True)
    old_copy.close()
    (tmp_path / "old.sqlite3").replace(data / "import" / "old-18000.sqlite3")
    cfg = dataclasses.replace(CFG, database_path=data / "farm_radar.sqlite3",
                              feeds=dataclasses.replace(CFG.feeds, database_path=data / "feeds.sqlite3"))
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    r = TestClient(api.app).get("/api/trial/final")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 10 and body["backtest_present"] is True
    assert sum(body["counts"].values()) == 10
    assert not any(x["title"] == "（計算できませんでした）" for x in body["items"])
