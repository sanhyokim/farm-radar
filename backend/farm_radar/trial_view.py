"""N5d「試す」の結果をまとめて見る画面（docs/n5-plan-2026-10-03.md 4章 N5d。2026-10-04 17:27 JST オーナーの指示書）。

N5 で確かめている6つの項目（①ボーナスの取り分 ②幅の中にいた割合・置き直し ③値動きの損 ④費用 ⑤保険 ⑥早く出る決まり）と
比べる相手を、見込み・実際・差・判定・まだ記録中か、の形で1か所に並べる。

- 新しい記録は作らない。今ある計算（さかのぼり backtest.cached・Merkl の答え合わせ merkl_check・練習の記録）を並べ直すだけ。
- 判定は3つ: 確認できた（ok）・要注意（warn）・記録中（rec）。記録が足りないものに「合格」「不合格」を付けない。
  - さかのぼり（今の版の写し）の項目は、実際の期間が MIN_DAYS 日たつまで「記録中」。置き直しは「記録中・最終判定は7日後」
    （置き直しの式は7日分の記録まで変えない。2026-10-03 オーナー）。
  - Merkl の分母（全員か、幅の中だけか）は、ここでは決めない。比べた件数と期間が足りたら、A/B の案をオーナーに出す。
- 年に引きのばした値は「短期間の1日平均を365倍した参考値。1年間の予想ではありません」。とても大きい値は年の形で出さない。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import UTC, datetime
from typing import Any

MIN_DAYS = 7                    # さかのぼり・練習の記録で判定するのに要る実際の期間（日）。置き直しの「7日分」と同じ
MERKL_MIN_PAIRS = 30            # Merkl の答え合わせで判定するのに要る、比べた組（ずっと幅の中にいた区切り）の数（仮）
MERKL_MIN_DAYS = 3              # 同じく、記録の期間（日。仮。「数日分」）
REL_OK = 0.30                   # 合格の目安: 差が見込みの30%以内（さかのぼりと同じ PASS_REL）
OK_SHARE = 0.7                  # 「合っていた日の割合」がこれ以上なら「確認できた」、下なら「要注意」（仮）
FOCUS_RANGES = (0.5, 2.0, 15.0)  # 画面で目立たせる幅（オーナーの指示書）
JUMP_TIMES = 1.5                # 実際の値動きが見込みのこれ倍以上 = 「値動きが急に大きくなった」（探すの印と同じ 1.5 倍）
BIG_YEAR_PCT = 1000.0           # 年に引きのばした値がこれ以上（絶対値）なら、年の形では出さない
YEAR_NOTE = "短期間の1日平均を365倍した参考値。1年間の予想ではありません"
KIND_JA = {"stock": "株", "coin": "ふつうのコイン", "bonus": "ボーナスのコイン", "stable": "ステーブルどうし"}

OK, WARN, REC = "ok", "warn", "rec"
STATE_JA = {OK: "確認できた", WARN: "要注意", REC: "記録中"}


def state(code: str, why: str) -> dict[str, str]:
    return {"code": code, "label": STATE_JA[code], "why": why}


def _days_between(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return (_dt(b) - _dt(a)).total_seconds() / 86400
    except ValueError:
        return None


def _dt(v: Any) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _gap(pred: float | None, real: float | None) -> dict[str, float | None]:
    """差（実際 − 見込み）と、見込みに対する割合（%）。"""
    if pred is None or real is None:
        return {"diff": None, "diff_pct": None}
    return {"diff": real - pred, "diff_pct": (real / pred - 1) * 100 if pred else None}


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def year_value(day_frac: float | None) -> dict[str, Any]:
    """1日あたりの割合（建玉のお金あたり）から、1日の % と年に引きのばした参考値の %。"""
    if day_frac is None:
        return {"day_pct": None, "year_pct": None, "year_shown": False}
    year = day_frac * 365 * 100
    return {"day_pct": day_frac * 100, "year_pct": year, "year_shown": abs(year) < BIG_YEAR_PCT}


def _from_year(year_pct: float | None) -> dict[str, Any]:
    return year_value(year_pct / 100 / 365 if year_pct is not None else None)


# --- 記録の期間 ------------------------------------------------------------------------------------------

def periods(bt: dict[str, Any] | None, feeds: dict[str, Any], practice: dict[str, Any]) -> dict[str, Any]:
    days = (bt or {}).get("days") or []
    mr = feeds.get("merkl_rewards") or {}
    lh = feeds.get("lighter_history") or {}
    return {
        "backtest": {"first_day": days[0] if days else None, "last_day": days[-1] if days else None,
                     "calendar_days": len(days), "pools": (bt or {}).get("pools"),
                     "pool_days": (bt or {}).get("pool_days"),
                     "text": "今の版（ポート 18000）のデータの写し。15分ごとのプールの値段で、見込みと同じ幅を動かした結果"},
        "merkl": {"from": mr.get("from"), "to": mr.get("to"), "days": _days_between(mr.get("from"), mr.get("to")),
                  "campaigns": mr.get("campaigns")},
        "lighter": {"from": lh.get("from"), "to": lh.get("to"), "markets": lh.get("markets")},
        "practice": {"from": practice.get("since"), "days": practice.get("days"), "positions": practice.get("positions")},
    }


# --- ① ボーナスの取り分（Merkl） -------------------------------------------------------------------------

def bonus_share(mc: dict[str, Any] | None, merkl_days: float | None) -> dict[str, Any]:
    mc = mc or {}
    camps = mc.get("campaigns") or []
    n = int(mc.get("pairs_in_range") or 0)
    rows = [{"pair": c.get("pair"), "chain_id": c.get("chain_id"), "pairs": c.get("pairs"),
             "pairs_in_range": c.get("pairs_in_range"), "intervals": c.get("intervals"),
             "predicted": c.get("predicted_median"), "actual": c.get("actual_median"),
             "ratio": c.get("ratio_median"), "ratio_p25": c.get("ratio_p25"), "ratio_p75": c.get("ratio_p75"),
             "denominator_share": c.get("denominator_share_median"),
             "out_of_range_setting": c.get("out_of_range_paid"), "out_of_range_paid_share": c.get("out_of_range_paid_share"),
             "first": c.get("first"), "last": c.get("last")} for c in camps]
    out_paid = [r["out_of_range_paid_share"] for r in rows if r["out_of_range_paid_share"] is not None]
    enough = n >= MERKL_MIN_PAIRS and (merkl_days or 0) >= MERKL_MIN_DAYS
    ratio = mc.get("ratio_median")
    if not enough:
        st = state(REC, f"まだ判定しない（比べた組 {n} 件・記録 {merkl_days or 0:.1f} 日。"
                        f"判定は {MERKL_MIN_PAIRS} 件・{MERKL_MIN_DAYS} 日から）")
    elif ratio is not None and abs(ratio - 1) <= REL_OK:
        st = state(OK, "実際 ÷ 見込みが 1 に近い（±30% の中）")
    else:
        st = state(WARN, "実際 ÷ 見込みが 1 から離れている。分母の A/B の案を出す")
    return {
        "state": st,
        "summary": {"pairs": mc.get("pairs"), "pairs_in_range": n, "campaigns": len(rows), "ratio_median": ratio,
                    "denominator_share_median": mc.get("denominator_share_median"),
                    "out_of_range_paid_share": _mean(out_paid), "days": merkl_days,
                    "skipped": mc.get("skipped") or {}, "errors": mc.get("errors") or []},
        "campaigns": rows,
        "decision": "「全員を分母（A。今の式）」か「幅の中だけを分母（B）」かは、まだ決めません。比べた組がたまったら、"
                    "A と B それぞれの見込みと実際を並べて、どちらが近かったかを出してから A/B をお聞きします。",
        "notes": ["見込み = 今の探すの式で、1つの区切り（約2時間）に預け方1つがもらう割合。実際 = Merkl が実際に配った割合。",
                  "分母の割合: 100% に近い = 全員が分母（今の式のとおり）。小さい = もっと少ない額が分母（幅の中だけに近い）。",
                  "幅の外でも配られたか: ずっと幅の外にいた預け方が、なにかもらった割合。0% なら「幅の外には配らない」のとおり。"],
    }


# --- ② 幅の中にいた割合・置き直し --------------------------------------------------------------------------

def _range_row(x: dict[str, Any]) -> dict[str, Any]:
    rb, ir = x.get("rebalances") or {}, x.get("in_range") or {}
    return {"r_pct": x["r_pct"], "days": x.get("days"), "focus": float(x["r_pct"]) in FOCUS_RANGES,
            "rebalances": {"pred": rb.get("pred"), "real": rb.get("real"), **_gap(rb.get("pred"), rb.get("real")),
                           "pred_median": rb.get("pred_median"), "real_median": rb.get("real_median"),
                           "ok_share": rb.get("ok_share")},
            "in_range": {"pred": ir.get("pred"), "real": ir.get("real"), **_gap(ir.get("pred"), ir.get("real")),
                         "ok_share": ir.get("ok_share")}}


def rebalances(bt: dict[str, Any] | None, cal_days: int) -> dict[str, Any]:
    ranges = (bt or {}).get("ranges") or {}
    by_kind = {"all": [_range_row(x) for x in ranges.get("all") or []]}
    for k in KIND_JA:
        rows = ranges.get(f"kind_{k}") or []
        if rows:
            by_kind[k] = [_range_row(x) for x in rows]
    left = max(0, MIN_DAYS - cal_days)
    st = state(REC, f"記録中・最終判定は7日後（今 {cal_days} 日分。あと {left} 日）") if cal_days < MIN_DAYS else \
        state(REC, f"7日分たまった（{cal_days} 日分）。今の式・実際・差・直す案を並べてオーナーに判断をお願いする段階"
                   "（式はオーナーの判断まで変えない）")
    return {"state": st, "kinds": by_kind, "kind_ja": {"all": "ぜんぶ", **KIND_JA},
            "notes": ["見込みの置き直し回数は今の式（σ²/r²）。実際は、記録の値段で「幅の外に15分いたら置き直す」を動かした回数。",
                      "合っていた割合: 置き直しは差が見込みの30%以内（または1日0.5回以内）、幅の中の割合は差が5%以内だった日の割合。",
                      "置き直しの式は、7日分の記録がたまるまで変えません。"]}


# --- ③ 値動きの損 ---------------------------------------------------------------------------------------

def price_loss(bt: dict[str, Any] | None, cal_days: int, amount_usd: float) -> dict[str, Any]:
    per = (bt or {}).get("per_pool_best") or []
    cap = 0.001 * amount_usd               # さかのぼりの目安と同じ: 差が見込みの30%以内、または資金の0.1%（1日）以内
    kinds = []
    for k in ("stock", "coin", "bonus", "stable"):
        xs = [x for x in per if x.get("kind") == k and x.get("gamma_pred") is not None and x.get("gamma_real") is not None]
        if not xs:
            continue
        pred = _mean([x["gamma_pred"] for x in xs])
        real = _mean([x["gamma_real"] for x in xs])
        ok = [abs(x["gamma_real"] - x["gamma_pred"]) * (x.get("c_lp") or 0) <= max(REL_OK * x["gamma_pred"] * (x.get("c_lp") or 0), cap)
              for x in xs]
        g = _gap(pred, real)
        kinds.append({"kind": k, "kind_ja": KIND_JA[k], "pools": len(xs), "pred": pred, "real": real, **g,
                      "pred_usd_day": _mean([x["gamma_pred"] * (x.get("c_lp") or 0) for x in xs]),
                      "real_usd_day": _mean([x["gamma_real"] * (x.get("c_lp") or 0) for x in xs]),
                      "ok_share": sum(ok) / len(ok),
                      # 合格の目安（見込みの±30%）との差: 差が目安の何倍か（1 以下なら目安の中）
                      "vs_line": (abs(g["diff"]) / (REL_OK * pred)) if g["diff"] is not None and pred else None})
    jumps = [{"pair": x["pair"], "kind_ja": KIND_JA.get(x.get("kind"), x.get("kind")), "sigma_pred": x.get("sigma_pred"),
              "sigma_real": x.get("sigma_real"), "times": x["sigma_real"] / x["sigma_pred"],
              "gamma_pred": x.get("gamma_pred"), "gamma_real": x.get("gamma_real")}
             for x in per if x.get("sigma_pred") and x.get("sigma_real") and x["sigma_real"] >= JUMP_TIMES * x["sigma_pred"]]
    jumps.sort(key=lambda j: -j["times"])
    if cal_days < MIN_DAYS:
        st = state(REC, f"記録中（今 {cal_days} 日分。判定は {MIN_DAYS} 日分から）")
    elif kinds and min(k["ok_share"] for k in kinds) >= OK_SHARE:
        st = state(OK, "どの種類も、見込みと実際が目安の中だったプールが多い")
    else:
        st = state(WARN, "見込みと実際の差が目安の外のプールが多い種類がある")
    return {"state": st, "kinds": kinds, "jumps": jumps,
            "notes": ["値動きの損は、建玉のお金に対する1日の割合（今のやり方で選んだ幅）。",
                      "見込みは過去7日の値動きから出すので、値動きが急に大きくなると遅れる（例: MOO は σ 19.3%→39.5%）。",
                      f"「値動きが急に大きくなった」= 実際の値動きが見込みの {JUMP_TIMES:g} 倍以上のプール。"]}


# --- ④ 費用 --------------------------------------------------------------------------------------------

def costs(bt: dict[str, Any] | None, cal_days: int, practice: dict[str, Any]) -> dict[str, Any]:
    ranges = ((bt or {}).get("ranges") or {}).get("all") or []
    rows = []
    for x in ranges:
        c = x.get("cost") or {}
        rows.append({"r_pct": x["r_pct"], "focus": float(x["r_pct"]) in FOCUS_RANGES, "days": x.get("days"),
                     "pred": c.get("pred"), "real": c.get("real"), **_gap(c.get("pred"), c.get("real")),
                     "slip_pred": c.get("slip_pred"), "slip_real": c.get("slip_real"), "ok_share": c.get("ok_share")})
    fund = (bt or {}).get("funding") or []
    f_pred = _mean([f["pred"] for f in fund if f.get("pred") is not None])
    f_real = _mean([f["real"] for f in fund if f.get("real") is not None])
    pos = practice.get("rows") or []
    opens = [p["open_cost_pct"] for p in pos if p.get("open_cost_pct") is not None]
    closes = [p["close_cost_pct"] for p in pos if p.get("close_cost_pct") is not None]
    rec = cal_days < MIN_DAYS
    items = [
        {"key": "rebalance", "label": "置き直しの費用", "rows": rows,
         "state": state(REC, f"記録中・最終判定は7日後（今 {cal_days} 日分）") if rec else
         (state(OK, "合っていた日が多い") if rows and min(r["ok_share"] or 0 for r in rows if r["focus"]) >= OK_SHARE
          else state(WARN, "見込みと実際の差が目安の外の日が多い幅がある"))},
        {"key": "enter_exit", "label": "入る／出る費用",
         "open_pct": _mean(opens), "close_pct": _mean(closes), "open_n": len(opens), "close_n": len(closes),
         "state": state(REC, "記録中（練習の費用は見込みと同じ式で数えるので、実際との比べは本物のお金を動かしてから）")},
        {"key": "hedge", "label": "保険の費用（資金調達料）", "pred": f_pred, "real": f_real, **_gap(f_pred, f_real),
         "markets": len(fund),
         "ok_share": _mean([f["ok_share"] for f in fund]) if fund else None,
         "state": state(REC, f"記録中（今 {cal_days} 日分）") if rec or not fund else
         (state(OK, "合っていた日が多い") if (_mean([f["ok_share"] for f in fund]) or 0) >= OK_SHARE
          else state(WARN, "見込みと実際の差が目安の外の日が多い"))},
    ]
    return {"items": items,
            "notes": ["置き直しの費用: 建玉のお金に対する1日の割合（両替の手数料・両替のずれ・ガス代）。両替のずれは % で別に出す。",
                      "入る／出る費用: 練習の記録（建玉のお金に対する %）。本物のお金ではまだ確かめていない。",
                      "保険の費用: Lighter 本体の資金調達料（売り1ドルあたり1日）。RH版は ⑤ に分けて出す。"]}


# --- ⑤ 保険 --------------------------------------------------------------------------------------------

def hedge(bt: dict[str, Any] | None, feeds: dict[str, Any], guard: dict[str, Any], practice: dict[str, Any],
          cal_days: int) -> dict[str, Any]:
    fund = (bt or {}).get("funding") or []
    main_rows = [{"venue": "main", "perp": f.get("perp"), "days": f.get("days"), "pred": f.get("pred"),
                  "real": f.get("real"), **_gap(f.get("pred"), f.get("real")), "ok_share": f.get("ok_share")} for f in fund]
    rh = feeds.get("lighter_rh") or {}
    rh_rows = [{"venue": "rh", "symbol": r.get("symbol"), "funding_daily_rh": r.get("funding_daily_rh"),
                "funding_daily_main": r.get("funding_daily_main"), "mmf_rh_pct": r.get("mmf_rh_pct"),
                "mmf_main_pct": r.get("mmf_main_pct"), "funding_points": r.get("funding_points"),
                "price_points": r.get("price_points")} for r in rh.get("rows") or []]
    s = (bt or {}).get("settings") or {}
    line = float(s.get("withstand_rise_pct") or 50.0)
    margins = [{"symbol": m.get("symbol"), "rise_pct": m.get("rise_pct"), "jump_up_pct": m.get("jump_up_pct"),
                "span_days": m.get("span_days"), "over": (m.get("rise_pct") or 0) >= line}
               for m in (bt or {}).get("margins_long") or []]
    margins.sort(key=lambda m: -(m["rise_pct"] or 0))
    topups = guard.get("topups") or []
    margin_log = guard.get("margin_log") or []
    venues = practice.get("hedge_venues") or {}
    rec = cal_days < MIN_DAYS
    return {
        "funding_main": {"rows": main_rows,
                         "state": state(REC, f"記録中（今 {cal_days} 日分。判定は {MIN_DAYS} 日分から）") if rec or not main_rows else
                         (state(OK, "合っていた日が多い") if (_mean([r["ok_share"] for r in main_rows]) or 0) >= OK_SHARE
                          else state(WARN, "見込みと実際の差が目安の外の日が多い"))},
        "funding_rh": {"rows": rh_rows, "markets": rh.get("markets"), "hedge_markets": rh.get("hedge_markets"),
                       "updated_at": rh.get("updated_at"),
                       "state": state(REC, "記録中（RH版の見込みと実際の答え合わせは、RH版の記録がたまってから）")},
        "margins": {"rows": margins, "line_pct": line, "stay_days": s.get("stay_days"),
                    "over": [m["symbol"] for m in margins if m["over"]],
                    "state": state(OK, f"Lighter 本体の値段の過去（最大90日）で確かめた。{line:g}% を超えた市場は、"
                                       "その市場の過去の大きな上げを耐える幅にする（決定 ①A）") if margins else
                    state(REC, "記録中（Lighter の値段の過去がまだない）")},
        "topups": {"rows": topups, "count": sum(int(t.get("count") or 0) for t in topups),
                   "total_usd": sum(float(t.get("total_usd") or 0) for t in topups),
                   "cost_usd": sum(float(t.get("cost_usd") or 0) for t in topups), "line_frac": guard.get("topup_line_frac"),
                   "state": state(REC, "記録中（練習の記録。足すかどうかの線は仮。N5・N6 の記録で見直す）")},
        "margin_log": {"rows": margin_log, "state": state(REC, "記録中（練習の保険の預け金の減り方）")},
        "practice_venues": venues,
        "notes": ["Lighter 本体（USDC）と、Robinhood Chain 版の Lighter（USDG）は別の取引所です。資金調達料も預け金の決まりも別に出します。",
                  "Robinhood Chain のプールの保険は、RH版にある18市場は RH版、ほかは本体を使います（決定 ②A）。",
                  "「足したとしたら」: 預け金の余裕がはじめの半分を切ったときに、本物のお金なら足す額（練習では足さず、記録だけ）。"]}


# --- ⑥ 早く出る決まり ------------------------------------------------------------------------------------

def _event_stats(evs: list[dict[str, Any]], key: str = "after_24h_pct") -> dict[str, Any]:
    after = [float(e[key]) for e in evs if e.get(key) is not None]
    return {"count": len(evs), "with_after": len(after), "after_median_pct": _median(after),
            # 出たあと下がった分 = 出ていれば避けられた可能性のある損。上がった分 = 早く出て逃した可能性のある利益
            "avoided_mean_pct": _mean([max(0.0, -a) for a in after]) if after else None,
            "missed_mean_pct": _mean([max(0.0, a) for a in after]) if after else None,
            "down_after": sum(1 for a in after if a < 0), "up_after": sum(1 for a in after if a > 0)}


def early_exit(bt: dict[str, Any] | None, guard: dict[str, Any], cal_days: int) -> dict[str, Any]:
    bt = bt or {}
    s1 = bt.get("stage1") or {}
    evs1 = s1.get("events") or []
    rw = (bt.get("stage2_reward") or {})
    line_rw = str(int(rw.get("threshold_pct") or -15))
    reward_evs, up_evs = [], []
    for t in rw.get("tokens") or []:
        evs = (t.get("events") or {}).get(line_rw) or []
        reward_evs += evs
        if str(t.get("symbol") or "").upper() == "UP":
            up_evs += evs
    dumps = (bt.get("stage2_dump") or {}).get("tokens") or []
    dump_evs = [e for d in dumps for e in (d.get("events_1h") or []) + (d.get("events_24h") or [])]
    rec = state(REC, f"記録中（今 {cal_days} 日分。合図の数が少ないうちは判定しない）")
    watch = guard.get("stage1_watch") or []
    diffs = {h: [float(w[f"diff_{h}h_usd"]) for w in watch if w.get(f"diff_{h}h_usd") is not None] for h in (1, 6, 24)}
    rows = [
        {"key": "pool_funds", "label": f"プールのお金の急減（1時間で −{s1.get('threshold_pct') or 30:g}%）",
         **_event_stats(evs1, "price_after_24h_pct"), "breakdown": s1.get("breakdown"),
         "big_pools": (s1.get("counts_big_pools") or {}).get(str(int(s1.get("threshold_pct") or 30))), "state": rec},
        {"key": "reward_drop", "label": f"ボーナスのコインの急落（24時間で {line_rw}%）", **_event_stats(reward_evs), "state": rec},
        {"key": "dump", "label": "投げ売り（1時間で −15% か 24時間で −30%）", **_event_stats(dump_evs), "state": rec},
        {"key": "up", "label": "UP の早出（ボーナスのコイン UP の急落）", **_event_stats(up_evs), "state": rec,
         "case": "2026-10-01 21:45 JST、UP が24時間で −22.8% になり、今の版の練習の2つが自動で出た（このときの記録は事例のメモに残してある）。"},
    ]
    return {"rows": rows,
            "practice_watch": {"count": len(watch),
                               "diff_sum_usd": {str(h): sum(v) if v else None for h, v in diffs.items()},
                               "state": state(REC, "記録中（練習で段階1の合図が出たあとの「出ていたら／残っていたら」）")},
            "notes": ["出たあとの値動き = 合図が出てから24時間後のコインの値段の変化（まん中）。",
                      "避けられた可能性のある損 = 出たあと下がった分の平均。逃した可能性のある利益 = 出たあと上がった分の平均。"
                      "どちらも「その場で出ていたら」の目安で、費用は入れていない。",
                      "合図の数がたまるまで、線（−30% など）は変えない。"]}


# --- 比べる相手 -----------------------------------------------------------------------------------------

def baselines(bt: dict[str, Any] | None, cal_days: int) -> dict[str, Any]:
    b = (bt or {}).get("baselines") or {}
    kinds = []
    for k in ("all", "stock", "coin", "bonus", "stable"):
        x = b.get(k if k == "all" else f"kind_{k}") or {}
        if not x.get("days"):
            continue
        row = {"kind": k, "kind_ja": "ぜんぶ" if k == "all" else KIND_JA[k], "pairs": x.get("days"),
               "calendar_days": x.get("calendar_days"), "first_day": x.get("first_day"), "last_day": x.get("last_day"),
               "now": _from_year(x.get("now_year_pct")), "wide": _from_year(x.get("wide_year_pct")),
               "lend": _from_year(x.get("lend_year_pct")), "nothing": _from_year(x.get("nothing_year_pct")),
               "beat_wide_share": x.get("beat_wide_share"), "beat_lend_share": x.get("beat_lend_share"),
               "beat_nothing_share": x.get("beat_nothing_share")}
        now_y, wide_y = x.get("now_year_pct"), x.get("wide_year_pct")
        row["finding"] = None
        if now_y is not None and wide_y is not None and wide_y > now_y:
            who = "全体では" if k == "all" else f"{row['kind_ja']}は"
            w = f"±{b.get('wide_r_pct') or 15:g}%"
            row["finding"] = (f"この {x.get('calendar_days') or cal_days} 日では、{who} {w} で置きっぱなしのほうが良かった。"
                              f"現在の発見として記録するだけで、「{w} に変えるべき」とはまだ判定しません。")
        kinds.append(row)
    return {"kinds": kinds, "lending": b.get("lending"), "wide_r_pct": b.get("wide_r_pct"), "year_note": YEAR_NOTE,
            "state": state(REC, f"記録中（今 {(b.get('all') or {}).get('calendar_days') or cal_days} 日分。"
                                "幅の最終案は7日分たまってから）"),
            "notes": ["今のやり方: その日のいちばん良い幅で、外に出たら置き直す。",
                      "広く置きっぱなし: いちばん広い幅で記録の最初に1回置き、置き直さない。",
                      "貸し出し: 同じ額を Aave v3 の USDC（Base）に置く。何もしない: 0%。",
                      f"「年」は{YEAR_NOTE}。年の値がとても大きい（±{BIG_YEAR_PCT:g}% 以上）ときは、年の形では出さず1日の値だけ出します。"]}


# --- 練習の記録 -----------------------------------------------------------------------------------------

def practice_rows(conn: sqlite3.Connection | None, now: datetime, risk: Any = None, limit: int = 50) -> dict[str, Any]:
    """練習の建玉ごとの、見込みと実際（1日あたり）と、保険の取引所（本体・RH版）。"""
    if conn is None:
        return {"rows": [], "positions": 0, "since": None, "days": None, "hedge_venues": {}}
    from .execution import views as paper_views
    try:
        pos = conn.execute("SELECT * FROM positions WHERE is_paper=1 ORDER BY opened_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        pos = []
    rows, venues = [], {"lighter": 0, "lighter_rh": 0}
    for p in pos:
        try:
            d = paper_views.detail(conn, p, now, risk)
        except Exception:  # noqa: BLE001  1つの建玉の不良で、まとめの画面を止めない
            continue
        cap = float(d.get("capital") or 0.0)
        comp = {r["key"]: r for r in ((d.get("compare") or {}).get("rows") or [])}
        legs = []
        for h in d.get("hedges") or []:
            hid = h.get("hedge_id") or "lighter"
            venues[hid] = venues.get(hid, 0) + 1
            legs.append({"perp": h.get("perp"), "venue": "rh" if hid == "lighter_rh" else "main"})
        rows.append({"id": d["id"], "pair": d.get("pair"), "status": d.get("status"), "days": d.get("days"),
                     "opened_at": d.get("opened_at"), "rebalances": d.get("rebalances"), "hedges": legs,
                     "compare_enabled": d.get("compare_enabled"),
                     "gamma": {"pred": (comp.get("gamma") or {}).get("predicted"), "real": (comp.get("gamma") or {}).get("actual")},
                     "hedge": {"pred": (comp.get("hedge") or {}).get("predicted"), "real": (comp.get("hedge") or {}).get("actual")},
                     "rebalance_cost": {"pred": (comp.get("other") or {}).get("predicted"),
                                        "real": (comp.get("other") or {}).get("actual")},
                     "open_cost_pct": float(d.get("open_cost_usd") or 0) / cap * 100 if cap else None,
                     "close_cost_pct": (float(d["close_cost_usd"]) / cap * 100) if cap and d.get("close_cost_usd") else None})
    since = min((r["opened_at"] for r in rows), default=None)
    return {"rows": rows, "positions": len(rows), "since": since,
            "days": (now - _dt(since)).total_seconds() / 86400 if since else None, "hedge_venues": venues}


# --- まとめ ---------------------------------------------------------------------------------------------

def build(bt_cached: dict[str, Any] | None, trial: dict[str, Any], guard: dict[str, Any],
          practice: dict[str, Any], amount_usd: float = 1000.0) -> dict[str, Any]:
    """画面の中身。bt_cached = backtest.cached の結果、trial = trial_records.summary、guard = 守るの記録の一部。"""
    bt = (bt_cached or {}).get("result") if (bt_cached or {}).get("present") else None
    feeds = (trial or {}).get("feeds") or {}
    cal_days = len((bt or {}).get("days") or [])
    per = periods(bt, feeds, practice)
    sections = {
        "bonus": bonus_share(feeds.get("merkl_check"), per["merkl"]["days"]),
        "rebalance": rebalances(bt, cal_days),
        "price_loss": price_loss(bt, cal_days, amount_usd),
        "costs": costs(bt, cal_days, practice),
        "hedge": hedge(bt, feeds, guard, practice, cal_days),
        "early_exit": early_exit(bt, guard, cal_days),
    }
    held = ["Merkl の分母（全員か、幅の中だけか）", "置き直しの式", "幅の最終案"]
    return {
        "periods": per, "sections": sections, "baselines": baselines(bt, cal_days),
        "practice": {"rows": practice.get("rows") or [], "positions": practice.get("positions"),
                     "since": practice.get("since")},
        "backtest_present": bt is not None,
        "backtest_text": None if bt is not None else (bt_cached or {}).get("text"),
        "computed_at": (bt_cached or {}).get("computed_at"),
        "held": held, "min_days": MIN_DAYS, "states": STATE_JA, "year_note": YEAR_NOTE,
        "notes": ["ここは今ある記録を並べ直すだけで、新しい記録は作りません。",
                  "判定は「確認できた」「要注意」「記録中」の3つです。記録が足りないうちは「合格」「不合格」を付けません。",
                  "数字はこの画面を開いているパソコンの記録です（作業場所の試験用の値とは別）。"],
    }


def guard_bits(conn: sqlite3.Connection | None, config: Any) -> dict[str, Any]:
    """守るの記録のうち、ここで使うもの（預け金の減り方・足したとしたら・段階1のあと）。"""
    if conn is None:
        return {}
    from .execution import risk_job
    out: dict[str, Any] = {"topup_line_frac": config.guard.hedge_topup_buffer_frac}
    for k, fn in (("topups", risk_job.topup_rows), ("margin_log", risk_job.margin_log_rows),
                  ("stage1_watch", risk_job.stage1_watch_rows)):
        try:
            out[k] = fn(conn)
        except sqlite3.OperationalError:
            out[k] = []
    return out


__all__ = ["build", "guard_bits", "practice_rows", "year_value", "state", "MIN_DAYS"]
