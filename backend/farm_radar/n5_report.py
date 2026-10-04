"""N5 最終判定レポート（2026-10-05 指示書「10/8のN5最終判定を一発で出せる状態にする」）。

10/8 ごろに今の版（18000）の写しを取り直したあと、1回の実行で N5 の判断材料をまとめて出す。
- 新しい版（18001）の API（/api/trial/final・/api/trial/backtest・/api/trial/records）を読んで並べ直すだけ。
  計算はパソコンで動いている版（#41）のまま。新しい記録は作らない。データベースは開かない。設定も変えない。
- 決めない。おすすめも付けない（案は指示役が出す）。記録が足りない項目は「記録待ち」「件数不足」と書き、案を出さない。
- パソコンの Python（標準の道具だけ）でそのまま動く: python n5_report.py --api http://localhost:18001 --out <ファイル>
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Any

MIN_DAYS = 7                 # さかのぼりの項目で案を出すのに要る暦の日数（final_prep と同じ）
WEEK_MIN_DAYS = 14           # 損の線の 1週間・始めてから（final_prep と同じ。ここでは変えない）
MERKL_MIN_PAIRS = 30         # Merkl A/B（final_prep と同じ）
MERKL_MIN_DAYS = 3
TYPE_MIN_PAIRS = 30          # 重みの種類ごとに「判断できる」とする目安: 比べた組 30 件以上・キャンペーン 3 件以上
TYPE_MIN_CAMPAIGNS = 3
FEW_EVENTS = 10              # final_prep の「少ない」と同じ目安 → ここでは「件数不足」と書く
MAIN_RANGES = (0.5, 2.0, 15.0)
CAP_PCT = 5.0                # 1プールでの自分の割合の上限（変えない。ここでは比べるだけ）
CAP_NEAR_PCT = CAP_PCT / 2   # 上限の半分を超えたら「境目に近い」
UNREAD_REASONS = ("読み終えていない", "読めていない")   # merkl_ab が「まだ読んでいない区切り」を外したときの理由
STATE = {"ready": "判断できる", "wait": "記録待ち", "lack": "材料不足"}


# --- 文字の数字を読む（/api/trial/final の表は文字で返る） ---------------------------------------------------

def num(s: Any) -> float | None:
    """"+1.2%" "−$3.40" "83%" "0.97" "—" → 数（% や $ は外す）。読めなければ None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("−", "-").replace("$", "").replace(",", "").replace("%", "").strip()
    m = re.match(r"^[+-]?\d+(\.\d+)?", t)
    return float(m.group(0)) if m else None


def fmt(v: float | None, nd: int = 2, unit: str = "", sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    return s + unit


def days_between(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
    except ValueError:
        return None


def tbl(title: str, head: list[str], rows: list[list[str]], note: str | None = None) -> dict[str, Any]:
    return {"title": title, "head": head, "rows": rows, "note": note}


def section(no: str, title: str, state: str, why: str, tables: list[dict[str, Any]] | None = None,
            lines: list[str] | None = None) -> dict[str, Any]:
    return {"no": no, "title": title, "state": state, "state_ja": STATE.get(state, state), "why": why,
            "tables": tables or [], "lines": lines or []}


def _item(final: dict[str, Any], key: str) -> dict[str, Any]:
    return next((x for x in final.get("items") or [] if x.get("key") == key), {})


def _table(x: dict[str, Any], title_start: str) -> dict[str, Any] | None:
    return next((t for t in x.get("tables") or [] if str(t.get("title", "")).startswith(title_start)), None)


def _marked(t: dict[str, Any] | None) -> list[list[str]]:
    """final の表に「★今の設定」の印を付けて返す。"""
    if not t:
        return []
    return [(["★" + r[0]] + r[1:]) if i in (t.get("mark") or []) else list(r) for i, r in enumerate(t.get("rows") or [])]


# --- ① 置き直し ------------------------------------------------------------------------------------------

def rebalance(bt: dict[str, Any] | None) -> dict[str, Any]:
    rows = ((bt or {}).get("ranges") or {}).get("all") or []
    cal = len((bt or {}).get("days") or [])
    ready = cal >= MIN_DAYS and bool(rows)
    body, ks = [], []
    for r in rows:
        rb = r.get("rebalances") or {}
        p, a = rb.get("pred_median"), rb.get("real_median")
        k = a / p if p and a is not None else None
        if k is not None:
            ks.append(k)
        main = any(abs(float(r["r_pct"]) - m) < 1e-9 for m in MAIN_RANGES)
        body.append([f"{'★' if main else ''}±{r['r_pct']:g}%", fmt(p), fmt(a), fmt(a - p if p is not None and a is not None
                                                                                   else None, sign=True),
                     fmt(k), "（1日の値動き ÷ 幅）²", (f"今の式 × {k:.2f}" if k is not None else "—") if ready else "（7日たまってから）"])
    lines = [f"暦の日数 {cal} 日（今の版の写し）。★ = ±0.5%・±2%・±15%。数は1日あたりのまん中（プールと日の組）。",
             "修正候補 = 今の式 × （実際 ÷ 見込み）。設定は変えていません（決めるのはオーナー）。"]
    if ready and ks:
        lines.append(f"全部の幅をまとめた 実際 ÷ 見込み のまん中: {statistics.median(ks):.2f}")
    why = (f"7日分そろいました（{cal} 日）。今の式 / 実際 / 差 / 修正候補 を出しています。" if ready
           else f"暦の日数が {cal} 日分です。{MIN_DAYS} 日分たまるまで修正候補は出しません。")
    return section("①", "置き直しの回数", "ready" if ready else "wait", why,
                   [tbl("幅ごと", ["幅", "今の見込み", "実際", "差", "実際÷見込み", "今の式", "修正候補"], body)], lines)


# --- ② 幅の境目の余裕 × 待ち時間 --------------------------------------------------------------------------

def edge(bt: dict[str, Any] | None, final: dict[str, Any]) -> dict[str, Any]:
    rows = (((bt or {}).get("prep") or {}).get("grid") or {}).get("rows") or []
    cal = max((r.get("calendar_days") or 0 for r in rows), default=0)
    ft = _table(_item(final, "edge"), "余裕 × 待ち時間")
    marks = set((ft or {}).get("mark") or [])
    frows = (ft or {}).get("rows") or []
    body = []
    for i, r in enumerate(rows):
        name = frows[i][0] if i in marks and i < len(frows) else f"{r['buffer'] * 100:.0f}%"   # 「（今の置き直し）」などの印も写す
        body.append([f"{'★' if i in marks else ''}{name}", f"{r['wait_min']:g}分", fmt(r.get("rebalances_day")),
                     fmt(r.get("flips_day")), fmt(r.get("cost_usd_day"), unit=" $"),
                     fmt(None if r.get("out_share") is None else r["out_share"] * 100, 1, "%"),
                     fmt(r.get("net_usd_day"), unit=" $", sign=True)])
    ranked = sorted((r for r in rows if r.get("net_usd_day") is not None),
                    key=lambda r: (-r["net_usd_day"], r.get("flips_day") or 0, r.get("cost_usd_day") or 0))[:3]
    top = [[f"{n}", f"余裕 {r['buffer'] * 100:.0f}%・待ち {r['wait_min']:g}分", fmt(r["net_usd_day"], unit=" $", sign=True),
            fmt(r.get("rebalances_day")), fmt(r.get("flips_day")), fmt(r.get("cost_usd_day"), unit=" $")]
           for n, r in enumerate(ranked, 1)]
    state = "lack" if not rows else "ready" if cal >= MIN_DAYS else "wait"
    why = ("比べられるプールの日ごとの記録がありません。" if state == "lack" else
           f"暦の日数 {cal} 日。上位3つは「最終損益（1日・$1,000 あたり）」の大きい順です。おすすめではありません。" if state == "ready"
           else f"暦の日数 {cal} 日。数は出ていますが、{MIN_DAYS} 日分たまるまでは目安です。")
    return section("②", "幅の境目の余裕 × 待ち時間", state, why,
                   [tbl("12の組み合わせ（★ = 今の設定）", ["余裕", "待ち", "置き直し/日", "行ったり来たり/日", "費用/日", "幅の外の時間",
                                                    "最終損益/日"], body),
                    tbl("成績の良い候補（上位3つ。決めない）", ["順", "組み合わせ", "最終損益/日", "置き直し/日", "行ったり来たり/日",
                                                 "費用/日"], top)])


# --- ③ 段階1 ・④ 段階2・⑤ 段階4・損の線（final の表をそのまま使う） ----------------------------------------

def stage1(bt: dict[str, Any] | None, final: dict[str, Any]) -> dict[str, Any]:
    x = _item(final, "stage1")
    rows = ((bt or {}).get("prep") or {}).get("stage1") or []
    total = sum(int((r.get("all") or {}).get("count") or 0) for r in rows)
    lines = [f"合図は4つの線の合計で {total} 回。" + ("少ないので、線を変える根拠には足りない目安です（件数不足）。" if total < FEW_EVENTS else ""),
             "★ = 今の線（−30%）。比べ方: A −30% のまま / B 別の線 / 必要なら C。"]
    head = [{"大きいプール": "5万ドル以上のプール"}.get(h, h) for h in (_table(x, "線ごと") or {}).get("head") or []]
    return section("③", "段階1（プールのお金が1時間で減った）", (x.get("state") or {}).get("code", "lack"),
                   (x.get("state") or {}).get("why", ""), [tbl("線ごと", head, _marked(_table(x, "線ごと")))], lines)


def _few(rows: list[list[str]]) -> list[list[str]]:
    return [[{"少ない": "件数不足", "数はある": "足りる"}.get(c, c) for c in r] for r in rows]


def stage2(final: dict[str, Any]) -> dict[str, Any]:
    x = _item(final, "stage2")
    tables = [tbl(t["title"], t["head"], _few(t["rows"]), t.get("note")) for t in x.get("tables") or []]
    return section("④", "段階2（コインの急な値下がり・狙い以下）", (x.get("state") or {}).get("code", "lack"),
                   (x.get("state") or {}).get("why", ""), tables,
                   [f"「件数不足」= 合図が {FEW_EVENTS} 回より少ない（線を変える根拠には足りない）。",
                    "避けた損 = 24時間後にさらに下がった分（出て避けた）。逃した利益 = 24時間後に戻った分（出て逃した）。"])


def stage4(final: dict[str, Any]) -> dict[str, Any]:
    x = _item(final, "stage4")
    t = (x.get("tables") or [None])[0]
    return section("⑤", "段階4（もっと良い場所へ移る）", (x.get("state") or {}).get("code", "lack"),
                   (x.get("state") or {}).get("why", ""),
                   [tbl("倍率ごと（★ = 今の 2倍・$1,000 あたり）", (t or {}).get("head") or [], _marked(t))] if t else [])


def week_ready_from(first_day: str | None) -> str | None:
    """1週間・始めてからの線に要る 14 日分がそろう最初の日（この日まで記録が入った写しが要る）。"""
    if not first_day:
        return None
    try:
        return (datetime.fromisoformat(first_day) + timedelta(days=WEEK_MIN_DAYS - 1)).date().isoformat()
    except ValueError:
        return None


def loss_lines(final: dict[str, Any], first_day: str | None = None) -> dict[str, Any]:
    x = _item(final, "loss_lines")
    lines = [f"1日の線は {MIN_DAYS} 日、1週間・始めてからの線は {WEEK_MIN_DAYS} 日分で「判断できる」にする決まりのままです。"]
    wr = week_ready_from(first_day)
    if wr:
        lines.append(f"記録は {first_day} から。{WEEK_MIN_DAYS} 日分がそろうのは、{wr} の終わりまで入った写しから"
                     "（記録の抜けた日があれば、もっと後）。")
    return section("損", "損の線（参考。ここでは条件を変えない）", (x.get("state") or {}).get("code", "lack"),
                   (x.get("state") or {}).get("why", ""), x.get("tables") or [], lines)


# --- Merkl A/B ・ 預け方の歴史 ・ 5% 上限 -------------------------------------------------------------------

def pool_history(records: dict[str, Any]) -> dict[str, Any]:
    ph = ((records or {}).get("feeds") or {}).get("pool_history") or {}
    errs = ph.get("errors") or []
    decided = [e for e in errs if str(e.get("error") or "").startswith(("読む量が多すぎる", "PoolManager が公式"))]
    real = [e for e in errs if e not in decided]
    lr = ph.get("last_run") or {}
    rows = [["対象のプール", str(ph.get("targets") if ph.get("targets") is not None else ph.get("pools", 0))],
            ["読み終わり", str(ph.get("done", 0))], ["途中（続きから読める）", f"{ph.get('partial', 0)}（{ph.get('resumable', 0)}）"],
            ["まだ読んでいない", str(ph.get("not_started", 0))], ["読まないと決めたもの", str(len(decided))],
            ["エラー", str(len(real))], ["読んだ記録（件）", f"{ph.get('events', 0):,}"],
            ["最後の回", f"{lr.get('ts', '—')}・{lr.get('calls', 0)} 回・{lr.get('stopped') or '最後まで'}"]]
    lines = [f"エラーの例: {real[0].get('error')}"] if real else []
    state = "lack" if not ph else "ready" if not ph.get("partial") and not ph.get("not_started") else "wait"
    why = ("預け方の歴史の進み具合を読めませんでした。" if state == "lack" else
           "対象のプールは全部読み終わっています。" if state == "ready" else
           "読み終わっていないプールがあります。全部の完了は条件にしません（下の Merkl A/B は、読めている区切りだけで数えます）。")
    return section("歴", "預け方の歴史（Merkl B の材料）の進み具合", state, why, [tbl("プール", ["項目", "値"], rows)], lines)


def _weight_types(final_merkl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """キャンペーンごとの表から、重みの種類ごとのキャンペーンの数と比べた組を数える（merkl_ab.main_weight と同じ分け方）。"""
    t = next((t for t in final_merkl.get("tables") or [] if t.get("title") == "キャンペーンごと"), None)
    out: dict[str, dict[str, Any]] = {}
    for r in (t or {}).get("rows") or []:
        w = [num(x) or 0.0 for x in str(r[2]).split("/")] if len(r) > 3 else []
        if len(w) != 3:
            continue
        name = "手数料が中心" if w[0] >= w[1] + w[2] else "コインが中心"
        pairs = int(num(r[3]) or 0)
        d = out.setdefault(name, {"campaigns": 0, "campaigns_compared": 0, "pairs": 0})
        d["campaigns"] += 1
        d["campaigns_compared"] += pairs > 0
        d["pairs"] += pairs
    return out


def merkl_ab(final: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
    m = final.get("merkl") or {}
    sums = ((records or {}).get("feeds") or {}).get("merkl_sums") or {}
    full_days = days_between(sums.get("from"), sums.get("to")) or 0.0
    pairs, days = int(m.get("pairs") or 0), float(m.get("days") or 0.0)
    skipped = next((t for t in m.get("tables") or [] if str(t.get("title", "")).startswith("使わなかった")), None)
    unread = sum(int(num(r[1]) or 0) for r in (skipped or {}).get("rows") or [] if str(r[0]).endswith(UNREAD_REASONS))
    conds = [("全部のページの記録が3日以上", full_days >= MERKL_MIN_DAYS, f"{full_days:.1f} 日（complete=1 {sums.get('complete', 0)} 件）"),
             ("比べた組が30以上", pairs >= MERKL_MIN_PAIRS, f"{pairs} 組"),
             ("比べた記録が3日以上", days >= MERKL_MIN_DAYS, f"{days:.1f} 日"),
             ("預け方の歴史が読めている区切りだけを使う", True, f"読めていない区切りは外した（{unread} 件）")]
    ok = all(c[1] for c in conds)
    tables = [tbl("判定の条件", ["条件", "満たした", "今"], [[c[0], "はい" if c[1] else "いいえ", c[2]] for c in conds])]
    lines = []
    if ok:
        t_all = m.get("table") or {}
        rows = [[r[0], r[1], r[2], r[3], r[4], str(pairs), str(_campaigns(m)), f"{days:.1f} 日"] for r in t_all.get("rows") or []]
        tables.append(tbl("A（全員を分母）と B（幅の中だけを分母）: 見込み ÷ 実際 − 1",
                          ["数え方", "まん中", "25%点", "75%点", "±30%以内", "比べた組", "キャンペーン", "記録日数"], rows))
        types = _weight_types(m)
        bw = next((t for t in m.get("tables") or [] if str(t.get("title", "")).startswith("重みの種類で分けた差")), None)
        trows = []
        for name in ("手数料が中心", "コインが中心"):
            r = next((r for r in (bw or {}).get("rows") or [] if r[0] == name), None)
            d = types.get(name) or {"campaigns": 0, "campaigns_compared": 0, "pairs": 0}
            n = int(num(r[5]) or 0) if r else 0
            enough = n >= TYPE_MIN_PAIRS and d["campaigns_compared"] >= TYPE_MIN_CAMPAIGNS
            trows.append([name, r[1] if r else "—", r[2] if r else "—", r[3] if r else "—", r[4] if r else "—", str(n),
                          str(d["campaigns_compared"]), "判断できる" if enough else "この種類はまだ判断できない"])
        tables.append(tbl("重みの種類で分けた差（まん中・±30%以内）", ["種類", "A まん中", "B まん中", "A ±30%", "B ±30%", "比べた組",
                                                         "キャンペーン", "判定"], trows,
                          f"種類ごとに「判断できる」とする目安: 比べた組 {TYPE_MIN_PAIRS} 以上・キャンペーン {TYPE_MIN_CAMPAIGNS} 以上。"
                          "種類ごとの 25%点・75%点は、今パソコンで動いている版の答えに入っていないので出していません。"))
        lines.append("A/B は決めていません。探すの見込みは A のままです。")
    else:
        lines.append("条件をまだ満たしていないので、A/B の判断用の数字は出していません（途中の数は「試すの結果」の画面にあります）。")
    return section("M", "Merkl の分母 A/B", "ready" if ok else "wait",
                   "条件をすべて満たしました。" if ok else "条件を満たしていないものがあります（上の「判定の条件」）。", tables, lines)


def _campaigns(m: dict[str, Any]) -> int:
    t = next((t for t in m.get("tables") or [] if t.get("title") == "キャンペーンごと"), None)
    return sum(1 for r in (t or {}).get("rows") or [] if len(r) > 3 and (num(r[3]) or 0) > 0)


def cap(final: dict[str, Any], ab_state: str) -> dict[str, Any]:
    m = final.get("merkl") or {}
    t = next((t for t in m.get("tables") or [] if str(t.get("title", "")).startswith("5% 上限の準備")), None)
    head = (t or {}).get("head") or []
    amounts = [h[:-2] for h in head[2::2]]          # "$100 A" → "$100"
    count = {(a, s): {"ok": 0, "near": 0, "over": 0} for a in amounts for s in ("A", "B")}
    over: list[str] = []
    for r in (t or {}).get("rows") or []:
        for i, a in enumerate(amounts):
            for j, s in enumerate(("A", "B")):
                v = num(r[2 + 2 * i + j])
                if v is None:
                    continue
                k = "over" if v > CAP_PCT else "near" if v >= CAP_NEAR_PCT else "ok"
                count[(a, s)][k] += 1
                if k == "over" and len(over) < 8:
                    over.append(f"{r[0]} {a}（{s}: {v:.2f}%）")
    rows = [[a, s, str(c["ok"]), str(c["near"]), str(c["over"])] for (a, s), c in count.items()]
    state = "lack" if not t or not t.get("rows") else "ready" if ab_state == "ready" else "wait"
    why = ("5% 上限の準備の表がまだありません。" if state == "lack" else
           "Merkl A/B の条件を満たしたので、A と B の両方で数えています。" if state == "ready" else
           "数は出ていますが、Merkl A/B の条件を満たすまでは目安です。")
    return section("5%", "1プール 5% 上限（変えない。比べるだけ）", state, why,
                   [tbl("自分の額が分母の何%か（キャンペーンの数）", ["自分の額", "分母", f"十分余裕（{CAP_NEAR_PCT:g}%未満）",
                                                      f"境目に近い（{CAP_NEAR_PCT:g}〜{CAP_PCT:g}%）", f"超える（{CAP_PCT:g}%超）"], rows)],
                   [f"超える例: {'、'.join(over)}" if over else "超える例はありません。"])


# --- 保険の預け金 ---------------------------------------------------------------------------------------------

def hedge(final: dict[str, Any]) -> dict[str, Any]:
    x = _item(final, "hedge_rise")
    t = (x.get("tables") or [None])[0]
    rows = (t or {}).get("rows") or []
    floor = [r for r in rows if str(r[4]).startswith("床")]
    over = [r for r in rows if not str(r[4]).startswith("床")]
    top = sorted(rows, key=lambda r: -(num(r[3]) or 0))[:6]
    mx = max((num(r[3]) or 0 for r in rows), default=None)
    body = [["市場", str(len(rows))], ["50% の床で決まる市場", str(len(floor))], ["50% を超える市場", str(len(over))],
            ["耐える上げ幅のいちばん大きい値", fmt(mx, 1, "%")],
            ["上位の市場", "、".join(f"{r[0]} {r[3]}" for r in top) or "—"]]
    return section("保", "保険の預け金（14日のいちばんの上げ・最低 50%。決定済みの確認）", (x.get("state") or {}).get("code", "lack"),
                   (x.get("state") or {}).get("why", ""), [tbl("まとめ", ["項目", "値"], body)],
                   ["新しい A/B の判断ではありません。今の決定（市場ごとの 14日のいちばんの上げ、最低 50%）で足りているかの確認です。"])


# --- まとめ ------------------------------------------------------------------------------------------------

def build(final: dict[str, Any], bt_cached: dict[str, Any] | None, records: dict[str, Any] | None) -> dict[str, Any]:
    bt = (bt_cached or {}).get("result") if (bt_cached or {}).get("present") else None
    days = (bt or {}).get("days") or []
    secs = [rebalance(bt), edge(bt, final), stage1(bt, final), stage2(final), stage4(final),
            loss_lines(final, days[0] if days else None)]
    ph = pool_history(records or {})
    ab = merkl_ab(final, records or {})
    secs += [ph, ab, cap(final, ab["state"]), hedge(final)]
    waiting = [f"{s['title']}: {s['state_ja']}（{s['why']}）" for s in secs if s["state"] != "ready" and s["no"] != "歴"]
    for x in final.get("items") or []:
        if x.get("key") in ("risk", "bonus_drop", "reserve") and (x.get("state") or {}).get("code") != "ready":
            waiting.append(f"{x.get('title')}: {(x.get('state') or {}).get('label')}（{(x.get('state') or {}).get('why')}）")
    for n in final.get("n6") or []:
        waiting.append(f"{n.get('title')}: N6 で判断（{n.get('why')}）")
    copy = (records or {}).get("old_copy") or {}
    summary = {"copied_at": copy.get("copied_at"), "copy_checked": copy.get("checked"),
               "backtest_days": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
               "seven_day_ready": len(days) >= MIN_DAYS,
               "fourteen_day_ready": len(days) >= WEEK_MIN_DAYS, "fourteen_day_from": week_ready_from(days[0] if days else None), "merkl_ab_ready": ab["state"] == "ready",
               "merkl_ab_conditions": [r[:2] for r in ab["tables"][0]["rows"]],
               "pool_history": ph["state"], "waiting": waiting,
               "counts": {k: sum(1 for s in secs if s["state"] == k) for k in STATE}}
    return {"summary": summary, "sections": secs, "computed_at": (bt_cached or {}).get("computed_at"),
            "notes": ["ここでは決めません。設定も変えません。おすすめも付けません（案は指示役が出します）。",
                      "数は「あなたのパソコンで確かめた値」です（パソコンの新しい版が計算したもの）。"]}


def render(rep: dict[str, Any]) -> str:
    s = rep["summary"]
    out = ["# N5 最終判定レポート", ""]
    out += [f"- {n}" for n in rep["notes"]]
    out += ["", "## 記録の量",
            f"- 今の版の写し: {s['copied_at'] or '—'}（確かめ {'OK' if s['copy_checked'] else 'なし'}）",
            f"- さかのぼりの暦の日数: {s['backtest_days']} 日（{s['first_day'] or '—'} 〜 {s['last_day'] or '—'}）"
            f"・7日の判定: {'できる' if s['seven_day_ready'] else 'まだ'}"
            f"・14日の判定: {'できる' if s['fourteen_day_ready'] else 'まだ（' + str(s['fourteen_day_from'] or '—') + ' まで入った写しから）'}",
            f"- Merkl A/B の条件: {'満たした' if s['merkl_ab_ready'] else 'まだ'}"
            f"（{'・'.join(f'{c[0]} {c[1]}' for c in s['merkl_ab_conditions'])}）"]
    for sec in rep["sections"]:
        out += ["", f"## {sec['no']} {sec['title']} — {sec['state_ja']}", sec["why"]]
        for t in sec["tables"]:
            out += ["", f"### {t['title']}", "| " + " | ".join(t["head"]) + " |", "|" + "---|" * len(t["head"])]
            out += ["| " + " | ".join(str(c) for c in r) + " |" for r in t["rows"]] or ["（まだありません）"]
            if t.get("note"):
                out.append(t["note"])
        out += [f"- {x}" for x in sec["lines"]]
    out += ["", "## 判断待ち・記録待ちの項目"] + [f"- {w}" for w in s["waiting"]] + [""]
    return "\n".join(out)


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:            # noqa: S310  自分のパソコンの API だけ
        return json.loads(r.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="N5 最終判定レポート（読むだけ）")
    ap.add_argument("--api", default="http://localhost:18001")
    ap.add_argument("--out", default=None, help="レポート（文字）を書くファイル")
    ap.add_argument("--summary", default=None, help="まとめ（JSON）を書くファイル")
    ap.add_argument("--timeout", type=float, default=1800.0)
    a = ap.parse_args(argv)
    try:
        bt = _get(f"{a.api}/api/trial/backtest", a.timeout)          # 写しが変わっていれば、ここで計算し直す（数分かかることがある）
        final = _get(f"{a.api}/api/trial/final", a.timeout)
        records = _get(f"{a.api}/api/trial/records", 300)
    except Exception as exc:  # noqa: BLE001
        print(f"STOP 新しい版の答えを読めませんでした: {type(exc).__name__}: {exc}")
        return 2
    rep = build(final, bt, records)
    text = render(rep)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as f:
            json.dump(rep["summary"], f, ensure_ascii=False, indent=1)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
