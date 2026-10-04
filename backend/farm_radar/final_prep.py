"""N5 最終判断の準備（2026-10-04 18:20 JST オーナーの指示書「N5最終判断の準備を進める」）。

N5 の終わりにオーナーへ出す「仮の数字の見直し案」の材料を、記録がそろえばすぐ出せる形に並べる。
- 10項目それぞれに: 今の値・状態・根拠・記録量・変更案の準備状況。
- 状態は3つ: 判断できる（ready。A/B/C の案を出せる）・記録待ち（wait。数え方はできていて、日数か件数が足りない）・
  材料不足（lack。今の記録では決められない。何が足りないか、新しく記録が要るか、過去の公開記録で足りるかを書く）。
- ここでは決めない。おすすめも付けない（案とおすすめは指示役が出す）。設定は変えない。数字を推測で埋めない。
- 新しい記録は作らない。さかのぼり（backtest.cached の prep）・feeds の保存（読み取りだけ）・Merkl の答え合わせを数えるだけ。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

READY, WAIT, LACK = "ready", "wait", "lack"
STATE_JA = {READY: "判断できる", WAIT: "記録待ち", LACK: "材料不足"}
ORDER = {READY: 0, WAIT: 1, LACK: 2}
MIN_DAYS = 7                    # さかのぼりの項目で案を出すのに要る暦の日数（置き直しの「7日分」と同じ）
WEEK_MIN_DAYS = 14              # 1週間・始めてからの損の線に要る暦の日数（7日の窓が何回か取れるまで）
FEW_EVENTS = 10                 # 合図の数がこれより少なければ「線を変える根拠には少ない」と書く（目安）
MERKL_MIN_PAIRS = 30            # Merkl の答え合わせ（N5d と同じ）
MERKL_MIN_DAYS = 3
BONUS_MIN_COINS = 20            # ボーナスのコインの値下がり: 30日の記録があるコインがこれ以上で「判断できる」
RECENT_HACK_DAYS = 365          # riskscore と同じ
PCTS = (50, 75, 90, 95, 99)


def st(code: str, why: str) -> dict[str, str]:
    return {"code": code, "label": STATE_JA[code], "why": why}


def worst(*codes: str) -> str:
    return max(codes, key=lambda c: ORDER[c])


def q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = p * (len(ys) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)


def pct(v: float | None, nd: int = 1, sign: bool = True) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{nd}f}%" if sign else f"{v:.{nd}f}%"
    return s.replace("+0.0%", "0.0%").replace("-0.0%", "0.0%")


def share(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def usd(v: float | None, nd: int = 2) -> str:
    if v is None:
        return "—"
    return f"−${abs(v):,.{nd}f}" if v < 0 else f"${v:,.{nd}f}"


def table(title: str, head: list[str], rows: list[list[str]], note: str | None = None,
          mark: list[int] | None = None) -> dict[str, Any]:
    """画面にそのまま出す表（文字だけ）。mark は今の設定の行（目立たせる）。"""
    return {"title": title, "head": head, "rows": rows, "note": note, "mark": mark or []}


def item(no: int, key: str, title: str, now: str, state: dict[str, str], basis: list[str], amount: str,
         prep: str, tables: list[dict[str, Any]] | None = None, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"no": no, "key": key, "title": title, "now": now, "state": state, "basis": basis, "amount": amount,
            "prep": prep, "tables": tables or [], "missing": missing}


def lacking(what: str, new_recording: bool, past_public: bool | None, how: str) -> dict[str, Any]:
    """材料不足の中身: 何が足りないか・今から新しく記録が要るか・過去の公開記録で足りるか。"""
    return {"what": what, "new_recording": new_recording, "past_public": past_public, "how": how}


def _wait_why(days: int, need: int = MIN_DAYS) -> str:
    return f"暦の日数が {days} 日分です。{need} 日分たまったら案を出せます（あと {max(0, need - days)} 日）。"


# --- 1. 損の線 -----------------------------------------------------------------------------------------

LEVEL_JA = {"caution": "注意", "no_new": "新しく入らない", "stop": "すべて止める"}
PERIOD_JA = {"day": "1日", "week": "1週間", "since_start": "始めてから"}


def loss_lines(prep: dict[str, Any] | None, lines: dict[str, dict[str, float]]) -> dict[str, Any]:
    loss = (prep or {}).get("loss") or {}
    cal = int(loss.get("calendar_days") or 0)
    now = " / ".join(f"{PERIOD_JA[p]} {'・'.join(f'{v:g}%' for v in lines[p].values())}" for p in PERIOD_JA)
    tables, codes = [], []
    need = {"day": MIN_DAYS, "week": WEEK_MIN_DAYS, "since_start": WEEK_MIN_DAYS}
    for p, vals in ((k, loss.get(k) or []) for k in PERIOD_JA):
        code = READY if cal >= need[p] and vals else WAIT
        codes.append(code)
        dist = [[f"{x}%", pct(q(vals, 1 - x / 100), 2)] for x in PCTS]
        where = []
        for lv, line in lines[p].items():
            hit = sum(1 for v in vals if v <= line) / len(vals) if vals else None
            where.append([f"{LEVEL_JA[lv]} {line:g}%", share(hit)])
        unit = {"day": "プールと日の組", "week": "7日続いた窓", "since_start": "プール"}[p]
        n4 = {"day": "4日のうち3日", "week": "7日の窓4つのうち3つ", "since_start": "プール4つのうち3つ"}[p]
        tables.append(table(f"{PERIOD_JA[p]}（{STATE_JA[code]}・{unit} {len(vals)} 件）", ["よくある範囲", "損益（総資産あたり）"],
                            dist, f"「75%」= {n4}は、損がこれより小さかった。"))
        tables.append(table(f"{PERIOD_JA[p]}の今の線の位置", ["今の線", "この線より悪かった割合"], where))
    code = worst(*codes)
    why = (_wait_why(cal) if codes[0] == WAIT else
           "1日の線は判断できます。1週間・始めてからは、暦の日数が " f"{WEEK_MIN_DAYS} 日分たまるまで記録待ちです。"
           if code == WAIT else "3つの期間とも、ふだんの揺れの分布が出ています。")
    return item(1, "loss_lines", "損の線", now, st(code, why),
                ["今の版の15分ごとの記録で、今のやり方（その日のいちばん良い幅で置き直す）でプールを1つ持ったときの損益。",
                 "収入はスコアの見込み × 実際に幅の中にいた割合。値動きの損と置き直しの費用は実際の値段で計算。",
                 "保険の資金調達料とボーナスのコインの値下がりは入っていません（別の項目で見ます）。"],
                f"暦の日数 {cal} 日、プール {loss.get('pools') or 0} 個（今の版の写し）",
                "分布（50/75/90/95/99%）と、今の線がどこにあるかを出す計算はできています。線の案は指示役が出します。",
                tables)


# --- 2. 段階1 ----------------------------------------------------------------------------------------

def stage1(bt: dict[str, Any] | None, now_pct: float, own_pct: float) -> dict[str, Any]:
    rows = ((bt or {}).get("prep") or {}).get("stage1") or []
    cal = len((bt or {}).get("days") or [])
    body = []
    mark = []
    for i, r in enumerate(rows):
        a, b = r["all"], r["big"]
        body.append([f"−{r['threshold_pct']}%", str(a["count"]), str(b["count"]), share(a["recovered_6h_share"]),
                     share(a["worse_24h_share"]), share(a["false_alarm_share"])])
        if abs(r["threshold_pct"] - now_pct) < 1e-9:
            mark.append(i)
    total = sum(r["all"]["count"] for r in rows)
    code = WAIT if cal < MIN_DAYS or not rows else READY
    why = _wait_why(cal) if code == WAIT else (
        f"線ごとの回数とそのあとが出ています。合図は全部で {total} 回"
        + ("と少ないので、数字は目安です。" if total < FEW_EVENTS else "です。"))
    return item(2, "stage1", "段階1（プールのお金が急に減った）",
                f"プールのお金 1時間 −{now_pct:g}% → 強い注意と24時間新しく入らない。自分の持ち分 1時間 −{own_pct:g}% → 自動で出る",
                st(code, why),
                ["今の版の15分ごとの記録で、プールのお金（コインの量 × 今の値段）を1時間前と比べた。",
                 "「すぐ戻った」= 6時間のうちに減る前の90%まで戻った。「空振りらしい」= すぐ戻り、コインも24時間で20%以上は下がらなかった。",
                 "「24時間でさらに悪化」= 24時間後のプールのお金が、合図のときより少なかった。"],
                f"暦の日数 {cal} 日、合図 {total} 回（4つの線の合計）",
                "−20/−30/−40/−50% の線ごとに並べる計算はできています。自分の持ち分の −10% は練習の建玉の記録が要るので N6 で見ます。",
                [table("線ごとの回数とそのあと", ["線", "回数", "大きいプール", "6時間で戻った", "24時間でさらに悪化",
                                          "空振りらしい"], body, "大きいプール = 合図の前のプールのお金が $50,000 以上。", mark)])


# --- 3. 段階2 ----------------------------------------------------------------------------------------

def _ev_row(name: str, evs: list[dict[str, Any]]) -> list[str]:
    a6 = [e["after_6h_pct"] for e in evs if e.get("after_6h_pct") is not None]
    a24 = [e["after_24h_pct"] for e in evs if e.get("after_24h_pct") is not None]
    down = [-x for x in a24 if x < 0]
    up = [x for x in a24 if x >= 0]
    n = len(evs)
    enough = "少ない" if n < FEW_EVENTS else "数はある"
    return [name, str(n), pct(statistics.fmean(a6)) if a6 else "—", pct(statistics.fmean(a24)) if a24 else "—",
            share(len(down) / len(a24)) if a24 else "—", share(len(up) / len(a24)) if a24 else "—",
            pct(statistics.fmean(down), sign=False) if down else "—", pct(statistics.fmean(up), sign=False) if up else "—",
            enough]


def stage2(bt: dict[str, Any] | None, risk: Any, guard: Any) -> dict[str, Any]:
    b = bt or {}
    cal = len(b.get("days") or [])
    th = str(int(risk.exit_reward_token_24h_pct)) if float(risk.exit_reward_token_24h_pct).is_integer() \
        else str(risk.exit_reward_token_24h_pct)
    reward = [e for t in (b.get("stage2_reward") or {}).get("tokens") or [] for e in (t.get("events") or {}).get(th) or []]
    dumps = (b.get("stage2_dump") or {}).get("tokens") or []
    d1 = [e for t in dumps for e in t.get("events_1h") or []]
    d24 = [e for t in dumps for e in t.get("events_24h") or []]
    rows = [_ev_row(f"ボーナスのコイン 24時間 {risk.exit_reward_token_24h_pct:g}%", reward),
            _ev_row(f"投げ売り 1時間 {risk.exit_dump_1h_pct:g}%", d1),
            _ev_row(f"投げ売り 24時間 {risk.exit_dump_24h_pct:g}%", d24)]
    bt_prep = (b.get("prep") or {}).get("below_target") or {}
    evs = bt_prep.get("events") or []
    nd = bt_prep.get("next_day") or {}
    k24 = [e for e in evs if e.get("back_24h") is not None]
    below = [[f"狙い利回り（年{bt_prep.get('target_apr_pct', 30):g}%）以下が {guard.below_target_times} 回続いた", str(len(evs)),
              share(sum(1 for e in evs if e["back_6h"]) / len(evs)) if evs else "—",
              share(sum(1 for e in k24 if e["back_24h"]) / len(k24)) if k24 else "—",
              f"{nd.get('avoided_n', 0)} 回・平均 {usd(nd.get('avoided_mean_usd'))}",
              f"{nd.get('missed_n', 0)} 回・平均 {usd(nd.get('missed_mean_usd'))}",
              "少ない" if len(evs) < FEW_EVENTS else "数はある"]]
    total = sum(int(r[1]) for r in rows) + len(evs)
    code = WAIT if cal < MIN_DAYS else READY
    why = _wait_why(cal) if code == WAIT else "合図ごとの回数とそのあとが出ています。数が少ない合図は「少ない」と書いています。"
    return item(3, "stage2", "段階2（コインの急な値下がり・狙い以下）",
                f"ボーナスのコイン 24時間 {risk.exit_reward_token_24h_pct:g}%、投げ売り 1時間 {risk.exit_dump_1h_pct:g}%・"
                f"24時間 {risk.exit_dump_24h_pct:g}%、狙い以下 {guard.below_target_times} 回",
                st(code, why),
                ["値段の合図は、今の版の15分ごとのプールの値段（ドル）で、合図のあと6時間・24時間の値段の動き。",
                 "「さらに下がった」= 24時間後の値段が合図のときより低い（出て避けた損）。「戻った」= 高い（出て逃した分）。",
                 "狙い以下は、今の版のスコアの残る利回りで数え、「次の日」はそのプールの今のやり方の実際の損益（ドル。$1,000 あたり）。",
                 f"「少ない」= {FEW_EVENTS} 回より少ない。線を変える根拠には足りない目安です。"],
                f"暦の日数 {cal} 日、合図 {total} 回",
                "合図ごとの回数・6時間後・24時間後・避けた損・逃した分を出す計算はできています。",
                [table("値段の合図", ["合図", "回数", "6時間後", "24時間後", "さらに下がった", "戻った", "避けた損（平均）",
                                  "逃した分（平均）", "根拠"], rows),
                 table("狙い以下", ["合図", "回数", "6時間で狙い以上に戻った", "24時間で戻った", "次の日に損（避けた）",
                                "次の日に得（逃した）", "根拠"], below)])


# --- 4. 段階4 ----------------------------------------------------------------------------------------

def stage4(bt: dict[str, Any] | None, now_mult: float) -> dict[str, Any]:
    mv = ((bt or {}).get("prep") or {}).get("moves") or {}
    runs = mv.get("runs") or []
    cal = len((bt or {}).get("days") or [])
    stay = next((r for r in runs if r["multiple"] is None), None)
    body, mark = [], []
    for r in runs:
        if r["multiple"] is None:
            continue
        diff = r["pnl_usd"] - stay["pnl_usd"] if stay else None
        body.append([f"{r['multiple']:g}倍", str(r["moves"]), usd(r["move_cost_usd"]), usd(r["pnl_usd"]), usd(diff)])
        if abs(r["multiple"] - now_mult) < 1e-9:
            mark.append(len(body) - 1)
    if stay:
        body.append(["移らない", "0", usd(0), usd(stay["pnl_usd"]), usd(0)])
    if not runs or not mv.get("pools"):
        code, why = LACK, "今の版の写しに、比べられるプールの日ごとの記録がありません。"
    elif cal < MIN_DAYS:
        code, why = WAIT, _wait_why(cal)
    else:
        code, why = READY, "倍率ごとに、移った場合と移らなかった場合の差が出ています（up. の中のプールどうしだけ）。"
    return item(4, "stage4", "段階4（もっと良い場所へ移る）", f"（利回りの差 × 次の切り替えまでの日数）が移る費用の {now_mult:g} 倍より大きければ移る",
                st(code, why),
                ["1日ごとに、今のプールと、狙い利回り以上でいちばん良いプールを、今の版のスコアの利回りで比べた（アプリの段階4と同じ式）。",
                 "移る費用 = 閉じる費用 + 始める費用（両替する額 ×（手数料 + ずれ）+ ガス代2回ずつ）。",
                 "損益は、そのプールのその日の今のやり方の実際（$1,000 あたり。比べる相手と同じ作り）。",
                 "今の版が読んでいるのは up. だけなので、会場をまたぐ移動は比べられません（N6 の練習で見ます）。"],
                f"暦の日数 {cal} 日、プール {mv.get('pools') or 0} 個",
                "1/1.5/2/3倍と移らない場合を並べる計算はできています。",
                [table("倍率ごとの結果（$1,000 あたり）", ["倍率", "移った回数", "移る費用", "損益（費用のあと）", "移らない場合との差"],
                       body, None, mark)])


# --- 5. 幅の境目の余裕と待ち時間 ----------------------------------------------------------------------------

def edge_grid(bt: dict[str, Any] | None, guard: Any, wait_now: float) -> dict[str, Any]:
    g = ((bt or {}).get("prep") or {}).get("grid") or {}
    rows = g.get("rows") or []
    cal = max((r.get("calendar_days") or 0 for r in rows), default=0)
    body, mark = [], []
    for r in rows:
        tag = ""
        if r["buffer"] == 0 and abs(r["wait_min"] - wait_now) < 1e-9:
            tag = "（今の置き直し）"
        if abs(r["buffer"] - guard.hedge_edge_buffer_frac) < 1e-9 and abs(r["wait_min"] - guard.hedge_edge_wait_minutes) < 1e-9:
            tag = "（今の保険の直し）"
        if tag:
            mark.append(len(body))
        body.append([f"{r['buffer'] * 100:.0f}%{tag}", f"{r['wait_min']:g}分",
                     f"{r['rebalances_day']:.2f}" if r["rebalances_day"] is not None else "—",
                     f"{r['flips_day']:.2f}" if r["flips_day"] is not None else "—",
                     usd(r["cost_usd_day"]), share(r["out_share"]), usd(r["net_usd_day"])])
    code = LACK if not rows else WAIT if cal < MIN_DAYS else READY
    why = ("今の版の写しに、比べられるプールの日ごとの記録がありません。" if code == LACK else _wait_why(cal) if code == WAIT
           else "余裕 × 待ち時間の12の組み合わせが出ています。置き直しの式は変えていません。")
    return item(5, "edge", "幅の境目の余裕と待ち時間",
                f"保険の直し: 境目から幅の {guard.hedge_edge_buffer_frac * 100:g}% 外に {guard.hedge_edge_wait_minutes:g}分、"
                f"直したあと {guard.hedge_cooldown_minutes:g}分は直さない。置き直し: 外に {wait_now:g}分",
                st(code, why),
                ["今の版の15分ごとの値段で、その日のいちばん良い幅を置き、境目から「余裕」だけ外に出たまま「待ち時間」たったら置き直した。",
                 f"行ったり来たり = 置き直してから{int(g.get('flip_s', 3600) / 60)}分のうちに、値段が前の幅に戻った回数。",
                 "損益 = 収入の見込み × 実際に幅の中にいた割合 − 値動きの損 − 置き直しの費用（$1,000 あたり・1日）。",
                 "置き直しの見込みの式は、7日分の記録まで変えません（2026-10-03 オーナー）。ここは決まりの線を比べるだけです。"],
                f"暦の日数 {cal} 日、プールと日の組 {max((r['pool_days'] for r in rows), default=0)} 件",
                "余裕 0/5/10/15% × 待ち 15/30/60分の比べ方はできています。直したあとの待ち（1時間）は比べていません。",
                [table("余裕 × 待ち時間（1日あたり、プールと日の組の平均）",
                       ["余裕", "待ち", "置き直し", "行ったり来たり", "費用", "幅の外の時間", "損益"], body, None, mark)])


# --- 6. 保険で耐える上げ幅 ---------------------------------------------------------------------------------

def _rise_table(conn: sqlite3.Connection | None, table_name: str, window_s: float) -> dict[int, dict[str, Any]]:
    from .backtest import max_rise_hl
    if conn is None:
        return {}
    try:
        rows = conn.execute(f"SELECT market_id, ts, high, low, close, symbol FROM {table_name} WHERE high IS NOT NULL "
                            "AND low IS NOT NULL AND close IS NOT NULL ORDER BY market_id, ts").fetchall()
    except sqlite3.Error:
        return {}
    by: dict[int, list[tuple[int, float, float, float]]] = {}
    sym: dict[int, str] = {}
    for mid, t, h, lo, c, s in rows:
        by.setdefault(int(mid), []).append((int(t), float(h), float(lo), float(c)))
        sym[int(mid)] = s
    out = {}
    for mid, cs in by.items():
        mr = max_rise_hl(cs, window_s)
        if mr:
            out[mid] = {**mr, "symbol": sym.get(mid)}
    return out


def hedge_rise(fconn: sqlite3.Connection | None, config: Any) -> dict[str, Any]:
    from .backtest import hedge_markets
    from .execution.hedge_guard import rh_markets
    from .tokens import load_tokens
    op = config.opportunities
    floor = op.hedge_withstand_rise_pct
    window = op.stay_days * 86400
    try:
        markets = hedge_markets(load_tokens(root=config.root), [])
    except Exception:  # noqa: BLE001  登録が読めなくても画面は出す
        markets = {}
    main = _rise_table(fconn, "lighter_price_history", window)
    rh = _rise_table(fconn, "lighter_rh_price_history", window)
    rh_map = rh_markets(fconn) if fconn is not None else {}
    rows = []
    for mid, sym in sorted(markets.items(), key=lambda x: x[1] or ""):
        m = main.get(mid)
        r = rh.get(rh_map[mid][0]) if mid in rh_map else None
        rises = [x["rise_pct"] for x in (m, r) if x]
        if not rises:
            continue
        top = max(rises)
        rows.append({"symbol": sym, "main": m["rise_pct"] if m else None, "rh": r["rise_pct"] if r else None,
                     "span": max(x["span_days"] for x in (m, r) if x), "survive": max(floor, top), "floor_binds": top < floor})
    rows.sort(key=lambda x: -max(x["main"] or 0, x["rh"] or 0))
    over = [x for x in rows if not x["floor_binds"]]
    span = min((x["span"] for x in rows), default=0)
    if not rows:
        code, why = LACK, "Lighter の値段の過去がまだ保存されていません。"
        miss = lacking("Lighter の値段の過去（1時間の足）", False, True,
                       "毎日1回、公開の過去の記録を読んでいます（新しく集め始めるものはありません）。読めたら出ます。")
    else:
        code, why, miss = READY, (f"市場 {len(rows)} 個の、{op.stay_days:g}日のうちのいちばんの上げが出ています。"
                                  "決め直しではなく、今のやり方の確認です。"), None
    body = [[x["symbol"], pct(x["main"]) if x["main"] is not None else "—", pct(x["rh"]) if x["rh"] is not None else "—",
             pct(x["survive"], sign=False), "床（50%）" if x["floor_binds"] else "市場の上げ"] for x in rows]
    return item(6, "hedge_rise", "保険で耐える上げ幅",
                f"市場ごとに max({floor:g}%, {op.stay_days:g}日のうちのいちばんの上げ)（2026-10-04 決定 A）",
                st(code, why),
                ["Lighter（本体と Robinhood Chain 版）の値段の過去（1時間の足。最大90日）で、"
                 f"{op.stay_days:g}日のうちに安値からいちばん上がった幅。",
                 f"耐える上げ幅 = その市場の上げと {floor:g}% の大きい方（両方の版にある市場は大きい方）。",
                 f"市場の上げが {floor:g}% を超える市場: {len(over)} 個（{', '.join(x['symbol'] for x in over[:8]) or 'なし'}）。"
                 f"それ以外の {len(rows) - len(over)} 個は床の {floor:g}% で決まっています。"],
                f"市場 {len(rows)} 個、いちばん短い記録 {span:.0f} 日",
                "確認だけです（決定済み）。今のやり方で足りているか、床が要るかを並べています。",
                [table("市場ごと（上げの大きい順）", ["市場", "本体の上げ", "RH版の上げ", "耐える上げ幅", "決めているもの"], body)],
                miss)


# --- 7. 危なさの点数と推奨金額 ----------------------------------------------------------------------------

def _items(conn: sqlite3.Connection | None, source: str) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        return [{"key": k, **json.loads(v or "{}")} for k, v in
                conn.execute("SELECT key, info_json FROM feed_items WHERE source=?", (source,))]
    except sqlite3.Error:
        return []


def risk_score(fconn: sqlite3.Connection | None, config: Any) -> dict[str, Any]:
    from . import riskscore
    hacks = _items(fconn, "llama_hacks")
    protos = _items(fconn, "llama_protocols")
    by_id = {p["id"]: p for p in protos if p.get("id")}
    linked = []
    for h in hacks:
        p = by_id.get(h.get("defillama_id") or "")
        if p is None or not h.get("date"):
            continue
        prior = [x for x in hacks if x is not h and x.get("date") and x["date"] < h["date"]
                 and ((x.get("defillama_id") and x["defillama_id"] == h.get("defillama_id"))
                      or (x.get("parent_id") and x["parent_id"] == h.get("parent_id")))]
        age = (h["date"] - p["listed_at"]) / 86400 if p.get("listed_at") and p["listed_at"] <= h["date"] else None
        linked.append({"age": age, "prior_recent": any(h["date"] - x["date"] <= RECENT_HACK_DAYS * 86400 for x in prior),
                       "prior": bool(prior)})
    ages = [x["age"] for x in linked if x["age"] is not None]
    # 使う会場（会場の登録）の事件
    ours = []
    try:
        from .venue_match import load_known
        known = load_known(config.root)
    except Exception:  # noqa: BLE001
        known = {}
    slug = {p["key"]: p for p in protos}
    for vid, v in sorted(known.items()):
        rows = [slug[s] for s in ((v.get("defillama") or {}).get("slugs") or []) if s in slug]
        ids = {r.get("id") for r in rows if r.get("id")}
        parents = {r.get("parent") for r in rows if r.get("parent")}
        hs = [h for h in hacks if (h.get("defillama_id") and h["defillama_id"] in ids)
              or (h.get("parent_id") and h["parent_id"] in parents)]
        ours.append([v.get("name") or vid, str(len(hs)),
                     datetime.fromtimestamp(max(h["date"] for h in hs), UTC).strftime("%Y-%m-%d") if hs else "—"])
    body = [["事件の一覧（DefiLlama）", f"{len(hacks)} 件"],
            ["会場の記録と結びつけられた事件", f"{len(linked)} 件"],
            ["そのうち、会場が載った日が分かる（事件のときの年齢が分かる）", f"{len(ages)} 件"],
            ["事件のとき、載ってから30日より短い（年齢の点 20）", share(sum(1 for a in ages if a < 30) / len(ages)) if ages else "—"],
            ["事件のとき、30〜90日（年齢の点 10）", share(sum(1 for a in ages if 30 <= a < 90) / len(ages)) if ages else "—"],
            ["前の1年に別の事件があった（事件の点 25）", share(sum(1 for x in linked if x["prior_recent"]) / len(linked))
             if linked else "—"]]
    levels = " / ".join(f"{riskscore.LABEL[k]} {v:g}%" for k, v in riskscore.SHARE_PCT.items())
    return item(7, "risk", "危なさの点数と推奨金額", f"区切り: 20点・40点・60点。推奨金額: {levels}",
                st(LACK, "事件の前の点数を出す材料（そのときの預かり額の増え減り・運営の鍵・会場の見分け）が保存されていません。"
                         "分かる材料だけを目安として並べています。"),
                ["DefiLlama の事件の一覧と会場の一覧（毎日1回保存）を、会場の番号で結びつけた。",
                 "今の計算の材料のうち、事件の前に分かったはずのもの（会場の年齢・前の事件）だけを数えています。",
                 "監査の数は今の値しかなく、事件のあとに増えたものも入るので使っていません。",
                 "事件の前の点数そのものは出せないので、ここは目安にとどめます。新しい区切りは作りません。"],
                f"事件 {len(hacks)} 件、結びつけられた事件 {len(linked)} 件",
                "「低いのに事件」「高いと出て当たった」の数は、事件の前の点数が出せないのでまだ出せません。",
                [table("分かる材料だけの目安", ["中身", "数"], body),
                 table("使う会場（会場の登録）の事件", ["会場", "事件", "いちばん新しい事件"], ours)],
                lacking("事件の前の、会場の預かり額の増え減り・会場の年齢・運営の鍵（今の計算の材料）", False, True,
                        "DefiLlama の会場ごとの過去の預かり額の記録（公開）で、預かり額の増え減りは埋められます。"
                        "運営の鍵と会場の見分けは、そのときのチェーンの記録を読み直す必要があり、手間が大きいです。"
                        "どれも過去の記録なので、今から新しく集め始める必要はありません。"))


# --- 8. 1プールでの自分の割合 ----------------------------------------------------------------------------------

def pool_share(mc: dict[str, Any] | None, merkl_days: float | None, cap: float) -> dict[str, Any]:
    camps = (mc or {}).get("campaigns") or []
    pairs = int((mc or {}).get("pairs_in_range") or 0)
    a = cap / (1 + cap)

    def row(name: str, d: float | None) -> list[str]:
        if d is None or d <= 0:
            return [name, "—", share(a), "—", "—"]
        b = cap / (d + cap)
        return [name, f"{d:.2f}", f"{a * 100:.2f}%", f"{b * 100:.2f}%", pct((b / a - 1) * 100)]
    body = [row("（例）分母の割合 1.0 = 全員", 1.0), row("（例）0.8", 0.8), row("（例）0.5", 0.5)]
    for c in camps[:8]:
        body.append(row(f"{c.get('pair')}（比べた組 {c.get('pairs_in_range', 0)}）", c.get("denominator_share_median")))
    days = merkl_days or 0.0
    return item(8, "pool_share", "1プールでの自分の割合", f"自分が預かり額の {cap * 100:g}% を超えない",
                st(WAIT, "Merkl の分母（全員か、幅の中だけか）を決めてから見直します。計算の形はできています。"),
                [f"自分が預かり額の {cap * 100:g}% を預けたとき、A（全員が分母）なら取り分は {a * 100:.2f}%。",
                 "B（幅の中だけが分母）なら、分母は預かり額 × 分母の割合（d）になり、取り分は 5% ÷（d + 5%）。",
                 "d は Merkl の答え合わせで、実際の配り方から逆算した値（キャンペーンごとのまん中）。",
                 "A と B の取り分の差が、5% の上限が見込みのずれにどう効くかの目安です。"],
                f"比べた組 {pairs} 件、記録 {days:.1f} 日",
                "A・B それぞれの取り分と差を出す計算はできています。Merkl の A/B が決まったら、上限の案を出せます。",
                [table("5% のときの取り分", ["分母の割合 d", "d", "A の取り分", "B の取り分", "B と A の差"], body)])


# --- 9. 記録がないボーナスのコインの値下がり --------------------------------------------------------------------

def _change(pts: list[tuple[int, float]], days: float) -> float | None:
    """最後の値と、days 日前（±12時間）の値の変化（%）。"""
    if len(pts) < 2:
        return None
    t1, p1 = pts[-1]
    target = t1 - days * 86400
    best = min(pts, key=lambda x: abs(x[0] - target))
    if abs(best[0] - target) > 12 * 3600 or best[1] <= 0:
        return None
    return (p1 / best[1] - 1) * 100


def bonus_drop(fconn: sqlite3.Connection | None, config: Any, coin_keys: dict[int, str]) -> dict[str, Any]:
    monthly = config.opportunities.unknown_reward_drop_monthly_pct
    coins: set[str] = set()
    if fconn is not None:
        try:
            for cid, addr in fconn.execute("SELECT DISTINCT distribution_chain_id, reward_address FROM merkl_campaigns "
                                           "WHERE reward_address IS NOT NULL"):
                if coin_keys.get(cid):
                    coins.add(f"{coin_keys[cid]}:{str(addr).lower()}")
        except sqlite3.Error:
            coins = set()
    d7, d30, w7 = [], [], []
    for coin in sorted(coins):
        pts = [(int(t), float(p)) for t, p in fconn.execute(
            "SELECT ts, price FROM token_prices WHERE coin=? ORDER BY ts", (coin,))] if fconn is not None else []
        if len(pts) < 2 or all(0.97 <= p <= 1.03 for _, p in pts):
            continue                            # 値段がない・ドルのコイン（値下がりの比べ方に入れない）
        c7, c30 = _change(pts, 7), _change(pts, 30)
        if c7 is not None:
            d7.append(c7)
        if c30 is not None:
            d30.append(c30)
        # 記録の中の7日ごとの窓のいちばん悪い値（1日ずつずらす）
        worst7 = None
        for t, p in pts:
            later = [pp for tt, pp in pts if t < tt <= t + 7 * 86400]
            if later and pts[-1][0] >= t + 7 * 86400 - 12 * 3600 and p > 0:
                v = (later[-1] / p - 1) * 100
                worst7 = v if worst7 is None else min(worst7, v)
        if worst7 is not None:
            w7.append(worst7)

    def stats_row(name: str, xs: list[float]) -> list[str]:
        return [name, str(len(xs)), pct(q(xs, 0.5)), pct(q(xs, 0.10)), pct(q(xs, 0.05)), pct(min(xs) if xs else None)]
    body = [stats_row("直近7日", d7), stats_row("記録の中のいちばん悪い7日", w7), stats_row("直近30日", d30)]
    hit = sum(1 for x in d30 if x <= -monthly) / len(d30) if d30 else None
    if len(d30) >= BONUS_MIN_COINS:
        code, why, miss = READY, f"30日の記録があるボーナスのコインが {len(d30)} 個あります。", None
    else:
        code = LACK
        why = f"30日の記録があるボーナスのコインが {len(d30)} 個で、{BONUS_MIN_COINS} 個に足りません。"
        miss = lacking("値段の記録があるボーナスのコインの、30日の値段の動き", False, True,
                       "コインの値段は毎日1回、公開の過去30日分（4時間ごと）を読んでいます。DefiLlama の公開の過去の値段で"
                       "もっと長い期間も読めるので、今から新しく集め始める必要はありません。")
    return item(9, "bonus_drop", "記録がないボーナスのコインの値下がり", f"月 −{monthly:g}% とみなす",
                st(code, why),
                ["値段の記録があるボーナスのコイン（Merkl のキャンペーンで配るコイン。ドルのコインは除く）の値段の動きで、"
                 "記録がないコインの見込みの目安にする。",
                 "今配っているコインだけなので、消えたコインは入っていません（実際より甘く出るおそれ）。",
                 f"直近30日で −{monthly:g}% 以下だったコインの割合: {share(hit)}。"],
                f"コイン 7日 {len(d7)} 個・30日 {len(d30)} 個",
                "まん中・悪い方から10%・5%・いちばん悪い値を出す計算はできています。",
                [table("値段の変化（コインごと）", ["期間", "コイン", "まん中", "悪い方から10%", "悪い方から5%", "いちばん悪い"], body)],
                miss)


# --- 10. 予備のガス代 -----------------------------------------------------------------------------------------

def reserve_gas(bt: dict[str, Any] | None, config: Any) -> dict[str, Any]:
    g = ((bt or {}).get("prep") or {}).get("gas") or {}
    res = config.opportunities.reserve_usd
    rh = res.get("robinhood")
    first, last = g.get("first"), g.get("last")
    span = ((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() / 86400) if first and last else 0.0
    body = []
    for name, key in (("ふだん（まん中）", "median"), ("高いとき（上から10%）", "p90"), ("とても高いとき（上から1%）", "p99"),
                      ("いちばん高い", "max")):
        v = g.get(key)
        body.append([name, usd(v, 4), f"{rh / v:,.0f} 回" if v and rh else "—"])
    rh_code = READY if g.get("points") and span >= MIN_DAYS else WAIT if g.get("points") else LACK
    code = worst(rh_code, LACK)
    return item(10, "reserve", "予備のガス代", " / ".join(f"{k} ${v:g}" for k, v in res.items()),
                st(code, f"Robinhood Chain は{STATE_JA[rh_code]}（{span:.1f} 日分）。Base はガス代の記録が保存されていないので材料不足です。"),
                ["Robinhood Chain: 今の版が15分ごとにチェーンから読んだガスの値段 × 1回の取引の量（設定 "
                 f"{config.scoring.gas_units_per_tx:,}）× ETH の値段。",
                 "回数 = 予備の額で払える取引の回数（1回の出る・置き直しは数回の取引です）。",
                 "Base: 新しい版はチェーンを読まない設定なので、ガス代は決めた値（$" +
                 f"{config.opportunities.gas_usd_per_tx.get('base', 0):g}）のままで、記録がありません。"],
                f"Robinhood Chain {g.get('points') or 0} 回（{span:.1f} 日）、Base 0 回",
                "Robinhood Chain のふだん・高いとき・いちばん高いときと、予備で払える回数を出す計算はできています。今は変えません。",
                [table(f"Robinhood Chain（予備 ${rh:g}）", ["ガス代", "1回の取引", "予備で払える回数"], body)],
                lacking("Base のガス代の過去", False, True,
                        "Base のチェーンの公開の記録（ブロックごとの手数料）はあとから読めるので、今から集め始める必要はありません。"))


# --- 置き直し（7日で自動）と Merkl ------------------------------------------------------------------------------

def rebalance_auto(bt: dict[str, Any] | None) -> dict[str, Any]:
    """置き直しの回数の式（見込み = σ²/r²）を、7日分そろったら 今の式 / 実際 / 差 / 修正候補 の形で自動で出す。決めない。"""
    rows = ((bt or {}).get("ranges") or {}).get("all") or []
    cal = len((bt or {}).get("days") or [])
    ready = cal >= MIN_DAYS
    body = []
    for r in rows:
        rb = r.get("rebalances") or {}
        p, a = rb.get("pred_median"), rb.get("real_median")
        k = a / p if p and a is not None else None
        body.append([f"±{r['r_pct']:g}%", f"{p:.2f}" if p is not None else "—", f"{a:.2f}" if a is not None else "—",
                     f"{a - p:+.2f}" if p is not None and a is not None else "—",
                     (f"今の式 × {k:.2f}" if k is not None else "—") if ready else "7日後に出ます"])
    return {"state": st(READY if ready else WAIT,
                        "7日分そろいました。修正候補は、実際 ÷ 今の式 のまん中をかけた形です（決めるのはオーナー）。" if ready
                        else _wait_why(cal) + "修正候補はそのときに自動で出ます。"),
            "table": table("置き直しの回数（1日あたりのまん中）", ["幅", "今の式", "実際", "差", "修正候補"], body,
                           "今の式 = 値動き² ÷ 幅²。7日分の記録までは式を変えません（2026-10-03 オーナー）。"),
            "days": cal}


def merkl_ab(mc: dict[str, Any] | None, merkl_days: float | None) -> dict[str, Any]:
    pairs = int((mc or {}).get("pairs_in_range") or 0)
    days = merkl_days or 0.0
    ratio = (mc or {}).get("ratio_median")
    d = (mc or {}).get("denominator_share_median")
    ready = pairs >= MERKL_MIN_PAIRS and days >= MERKL_MIN_DAYS
    body = [["A（全員が分母。今の式）", f"{ratio:.2f}" if ratio is not None else "—",
             pct((ratio - 1) * 100) if ratio is not None else "—"],
            ["B（幅の中だけが分母）", "—", "材料不足"]]
    why = (f"比べた組 {pairs} 件・{days:.1f} 日分。{MERKL_MIN_PAIRS} 件・{MERKL_MIN_DAYS} 日たまるまで記録待ちです。" if not ready
           else f"A の差は出せます（比べた組 {pairs} 件・{days:.1f} 日分）。B の見込みは、幅の中にある預け方の合計が保存されていないので出せません。")
    return {"state": st(WAIT if not ready else LACK, why),
            "table": table("見込みと実際の差（実際 ÷ 見込みのまん中）", ["数え方", "実際 ÷ 見込み", "差"], body,
                           f"実際の配り方から逆算した分母の割合（まん中）: {d:.2f}（1 に近い = 全員、小さい = 幅の中だけに近い）。"
                           if d is not None else None),
            "pairs": pairs, "days": days,
            "missing": lacking("キャンペーンごとの、幅の中にある預け方の合計（B の見込みの分母）", False, True,
                               "チェーンには、預け方を足した・減らした記録がずっと残ります。あとから読み直して、その時の"
                               "幅の中の合計を作れる見込みです（2026-10-04 に、Robinhood Chain と Base の公開の読み取り口で、"
                               "この記録が読めることだけ確かめました。読み取りの仕組みはまだ作っていません）。")}


# --- まとめ ---------------------------------------------------------------------------------------------

def build(bt_cached: dict[str, Any] | None, fconn: sqlite3.Connection | None, trial: dict[str, Any] | None,
          config: Any, coin_keys: dict[int, str], now: datetime | None = None) -> dict[str, Any]:
    bt = (bt_cached or {}).get("result") if (bt_cached or {}).get("present") else None
    feeds = (trial or {}).get("feeds") or {}
    mc = feeds.get("merkl_check")
    mr = feeds.get("merkl_rewards") or {}
    merkl_days = None
    if mr.get("from") and mr.get("to"):
        try:
            merkl_days = (datetime.fromisoformat(mr["to"]) - datetime.fromisoformat(mr["from"])).total_seconds() / 86400
        except ValueError:
            merkl_days = None
    rk, gd = config.risk, config.guard
    items = []
    for fn in (lambda: loss_lines(bt and bt.get("prep"), gd.loss_lines),
               lambda: stage1(bt, rk.emergency_pool_funds_drop_1h_pct, rk.emergency_own_value_drop_1h_pct),
               lambda: stage2(bt, rk, gd),
               lambda: stage4(bt, gd.better_cost_multiple),
               lambda: edge_grid(bt, gd, rk.rebalance_after_minutes),
               lambda: hedge_rise(fconn, config),
               lambda: risk_score(fconn, config),
               lambda: pool_share(mc, merkl_days, config.opportunities.max_pool_share),
               lambda: bonus_drop(fconn, config, coin_keys),
               lambda: reserve_gas(bt, config)):
        try:
            items.append(fn())
        except Exception as exc:  # noqa: BLE001  1つの項目が落ちても、ほかの項目は出す
            items.append({"no": len(items) + 1, "key": f"error{len(items) + 1}", "title": "（計算できませんでした）",
                          "now": "", "state": st(LACK, f"計算で問題が起きました（{type(exc).__name__}）。"), "basis": [],
                          "amount": "", "prep": "", "tables": [], "missing": None})
    days = len((bt or {}).get("days") or [])
    first = ((bt or {}).get("days") or [None])[0]
    ready_on = (datetime.fromisoformat(first) + timedelta(days=MIN_DAYS)).strftime("%m/%d") if first else None
    counts = {k: sum(1 for x in items if x["state"]["code"] == k) for k in STATE_JA}
    return {
        "items": items, "counts": counts, "states": STATE_JA,
        "rebalance": rebalance_auto(bt), "merkl": merkl_ab(mc, merkl_days),
        "n6": [{"title": "控えめのほかの人のお金 1.5倍", "why": "練習の建玉で、ほかの人のお金がどれだけ動いたかを見てから決めます。"},
               {"title": "中身がステーブルの預かり証 月−3%", "why": "預かり証の値段の記録が、練習の間にたまってから決めます。"}],
        "backtest_days": days, "ready_on": ready_on,
        "backtest_present": bt is not None,
        "computed_at": (bt_cached or {}).get("computed_at"),
        "notes": ["ここでは決めません。設定も変えません。案とおすすめは指示役が出します。",
                  "状態は3つ: 判断できる（案を出せる）・記録待ち（数え方はできていて、日数か件数が足りない）・材料不足（今の記録では決められない）。",
                  "さかのぼりの数字は、今の版のデータの写しを取った時点までの分です。写しを取り直すと増えます。"],
        "now": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }


__all__ = ["build", "STATE_JA", "READY", "WAIT", "LACK"]
