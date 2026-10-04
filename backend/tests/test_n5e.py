"""N5d「試す」の結果のまとめ（trial_view と /api/trial/summary。2026-10-04 17:27 JST オーナーの指示書）。"""

from __future__ import annotations

import json

from farm_radar import trial_view as tv

from .test_n5b import CFG, old_copy  # noqa: F401


def _range(r, pred_n, real_n, ok_n, days=60):
    return {"r_pct": r, "days": days,
            "rebalances": {"pred": pred_n, "real": real_n, "ok_share": ok_n, "pred_median": pred_n, "real_median": real_n},
            "in_range": {"pred": 0.9, "real": 0.92, "ok_share": 0.9},
            "cost": {"pred": 0.001, "real": 0.0012, "ok_share": ok_n, "slip_pred": 0.0005, "slip_real": 0.001}}


def _bt(days, ok=0.9, gamma_real=0.0105, baselines=None, funding=None):
    per = [{"pair": "SPY/USDG", "kind": "stock", "gamma_pred": 0.01, "gamma_real": gamma_real, "c_lp": 780.0,
            "sigma_pred": 0.02, "sigma_real": 0.021},
           {"pair": "WETH/MOO", "kind": "coin", "gamma_pred": 0.01, "gamma_real": gamma_real, "c_lp": 780.0,
            "sigma_pred": 0.193, "sigma_real": 0.395}]
    return {"present": True, "computed_at": "2026-10-04T08:00:00+00:00", "result": {
        "days": [f"2026-10-{d:02d}" for d in range(1, days + 1)], "pools": 2, "pool_days": 2 * days,
        "settings": {"withstand_rise_pct": 50.0, "stay_days": 14.0},
        "ranges": {"all": [_range(0.5, 36.0, 11.0, ok), _range(2.0, 2.3, 1.6, ok), _range(15.0, 0.0, 0.0, ok)]},
        "per_pool_best": per, "funding": funding or [],
        "baselines": baselines or {}, "margins_long": [{"symbol": "NBIS", "rise_pct": 85.2, "jump_up_pct": 9.0,
                                                         "span_days": 90}, {"symbol": "SPY", "rise_pct": 8.0}],
        "stage1": {"threshold_pct": 30, "events": [], "breakdown": {}, "counts_big_pools": {"30": 0}},
        "stage2_reward": {"threshold_pct": -15, "tokens": [{"symbol": "UP", "events": {"-15": [
            {"at": 1, "after_24h_pct": -10.0}, {"at": 2, "after_24h_pct": 4.0}]}}]},
        "stage2_dump": {"tokens": []}}}


def _base(now, wide, lend=3.9, days=231, cal=3):
    return {"days": days, "calendar_days": cal, "first_day": "2026-10-01", "last_day": "2026-10-03",
            "now_year_pct": now, "wide_year_pct": wide, "lend_year_pct": lend, "nothing_year_pct": 0.0,
            "beat_wide_share": 0.4, "beat_lend_share": 0.6, "beat_nothing_share": 0.6}


def _merkl(pairs_in_range, ratio, days_to="2026-10-08T00:00:00+00:00"):
    camp = {"pair": "WETH/USDG", "chain_id": 4663, "pairs": pairs_in_range + 5, "pairs_in_range": pairs_in_range,
            "predicted_median": 0.01, "actual_median": 0.01 * ratio, "ratio_median": ratio,
            "denominator_share_median": 0.8, "out_of_range_paid": False, "out_of_range_paid_share": 0.0}
    return {"feeds": {"merkl_rewards": {"from": "2026-10-04T00:00:00+00:00", "to": days_to, "campaigns": 1},
                      "merkl_check": {"campaigns": [camp], "pairs": pairs_in_range + 5, "pairs_in_range": pairs_in_range,
                                      "ratio_median": ratio, "denominator_share_median": 0.8, "errors": [], "skipped": {}}}}


def _all_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def test_short_records_are_shown_as_recording_and_never_pass_or_fail():
    """3日分では、置き直し・値動きの損・Merkl はどれも「記録中」。合格・不合格の言葉を出さない。"""
    out = tv.build(_bt(3), _merkl(5, 1.4, "2026-10-04T12:00:00+00:00"), {}, {"rows": []})
    s = out["sections"]
    assert s["rebalance"]["state"]["code"] == "rec" and "最終判定は7日後" in s["rebalance"]["state"]["why"]
    assert s["price_loss"]["state"]["code"] == "rec"
    assert s["bonus"]["state"]["code"] == "rec" and "まだ判定しない" in s["bonus"]["state"]["why"]
    assert all(i["state"]["code"] == "rec" for i in s["costs"]["items"])
    assert all(r["state"]["code"] == "rec" for r in s["early_exit"]["rows"])
    text = _all_text({k: v for k, v in out.items() if k != "notes"})   # notes は3つの判定の説明（「合格」を付けない、と書く）
    assert "合格" not in text.replace("合格の目安", "") and "不合格" not in text
    assert "Merkl の分母（全員か、幅の中だけか）" in out["held"] and "置き直しの式" in out["held"]
    # ±0.5%・±2%・±15% は目立たせる
    assert [x["focus"] for x in s["rebalance"]["kinds"]["all"]] == [True, True, True]


def test_seven_days_still_keep_the_rebalance_formula_for_the_owner():
    """7日分たまっても、置き直しは判定を出さず、オーナーに並べて聞く段階と書く（式は変えない）。"""
    s = tv.build(_bt(7), {}, {}, {"rows": []})["sections"]
    assert s["rebalance"]["state"]["code"] == "rec" and "オーナーに判断" in s["rebalance"]["state"]["why"]
    assert s["price_loss"]["state"]["code"] == "ok"
    bad = tv.build(_bt(7, ok=0.2, gamma_real=0.02), {}, {}, {"rows": []})["sections"]
    assert bad["price_loss"]["state"]["code"] == "warn"


def test_moo_like_jumps_are_listed():
    s = tv.build(_bt(3), {}, {}, {"rows": []})["sections"]["price_loss"]
    assert [j["pair"] for j in s["jumps"]] == ["WETH/MOO"] and s["jumps"][0]["times"] > 2


def test_merkl_needs_enough_pairs_and_days_before_a_state():
    ok = tv.build(None, _merkl(40, 1.1), {}, {"rows": []})["sections"]["bonus"]
    warn = tv.build(None, _merkl(40, 1.6), {}, {"rows": []})["sections"]["bonus"]
    few_days = tv.build(None, _merkl(40, 1.1, "2026-10-05T00:00:00+00:00"), {}, {"rows": []})["sections"]["bonus"]
    assert ok["state"]["code"] == "ok" and warn["state"]["code"] == "warn" and few_days["state"]["code"] == "rec"
    assert "A/B" in ok["decision"]                    # 分母は決めない。A/B はあとで聞く
    assert abs(ok["campaigns"][0]["actual"] - 0.011) < 1e-12


def test_stock_finding_is_recorded_without_a_verdict_and_huge_years_are_hidden():
    bt = _bt(3, baselines={"all": _base(-1719.6, -300.0), "kind_stock": _base(28.6, 34.8, days=60),
                           "kind_coin": _base(-2878.1, -500.0), "wide_r_pct": 15.0})
    b = tv.build(bt, {}, {}, {"rows": []})["baselines"]
    stock = next(k for k in b["kinds"] if k["kind"] == "stock")
    assert "置きっぱなしのほうが良かった" in stock["finding"] and "まだ判定しません" in stock["finding"]
    assert stock["now"]["year_shown"] and round(stock["now"]["year_pct"], 1) == 28.6
    assert round(stock["now"]["day_pct"], 4) == round(28.6 / 365, 4)
    whole = next(k for k in b["kinds"] if k["kind"] == "all")
    assert whole["now"]["year_shown"] is False          # −1719.6% は年の形で出さない
    assert "1年間の予想ではありません" in b["year_note"]
    assert b["state"]["code"] == "rec"


def test_early_exit_counts_avoided_loss_and_missed_gain():
    rows = {r["key"]: r for r in tv.build(_bt(3), {}, {}, {"rows": []})["sections"]["early_exit"]["rows"]}
    up = rows["up"]
    assert up["count"] == 2 and up["down_after"] == 1 and up["up_after"] == 1
    assert up["avoided_mean_pct"] == 5.0 and up["missed_mean_pct"] == 2.0
    assert rows["reward_drop"]["count"] == 2


def test_hedge_keeps_rh_and_main_apart():
    trial = {"feeds": {"lighter_rh": {"markets": 58, "hedge_markets": 1, "rows": [
        {"symbol": "NVDA", "funding_daily_rh": 0.0001, "funding_daily_main": 0.0002, "mmf_rh_pct": 3.0, "mmf_main_pct": 2.0}]}}}
    bt = _bt(3, funding=[{"perp": "ETH", "days": 3, "pred": 0.0001, "real": 0.00012, "ok_share": 1.0}])
    practice = {"rows": [], "hedge_venues": {"lighter": 2, "lighter_rh": 1}}
    h = tv.build(bt, trial, {"topups": [{"pair": "USDG/NVDA", "count": 2, "total_usd": 30.0, "cost_usd": 0.1}]},
                 practice)["sections"]["hedge"]
    assert [r["venue"] for r in h["funding_main"]["rows"]] == ["main"]
    assert [r["venue"] for r in h["funding_rh"]["rows"]] == ["rh"]
    assert h["margins"]["over"] == ["NBIS"] and h["practice_venues"] == {"lighter": 2, "lighter_rh": 1}
    assert h["topups"]["count"] == 2 and h["topups"]["total_usd"] == 30.0


def test_no_backtest_copy_still_answers():
    out = tv.build({"present": False, "text": "今の版のデータの写しがない"}, {}, {}, {"rows": []})
    assert out["backtest_present"] is False and out["backtest_text"]
    assert out["sections"]["rebalance"]["kinds"]["all"] == []


def test_api_trial_summary(tmp_path, old_copy, monkeypatch):  # noqa: F811
    import dataclasses

    from fastapi.testclient import TestClient

    from farm_radar import api

    data = tmp_path / "data"
    (data / "import").mkdir(parents=True)
    old_copy.close()
    (tmp_path / "old.sqlite3").replace(data / "import" / "old-18000.sqlite3")
    cfg = dataclasses.replace(CFG, database_path=data / "farm_radar.sqlite3",
                              feeds=dataclasses.replace(CFG.feeds, database_path=data / "feeds.sqlite3"))
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    r = TestClient(api.app).get("/api/trial/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["backtest_present"] is True
    assert set(body["sections"]) == {"bonus", "rebalance", "price_loss", "costs", "hedge", "early_exit"}
    assert body["periods"]["backtest"]["pools"] == 2
