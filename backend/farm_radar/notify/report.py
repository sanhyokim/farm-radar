"""毎朝のレポートと /status の文面（SPEC 8章・12.5章）。

どちらも画面のホーム（api.home）と同じデータから作る。文面はルールで組み立てる（AIは使わない）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .events import SIGNAL_MARK

JST = ZoneInfo("Asia/Tokyo")
WEEK = "月火水木金土日"
MODE_JA = {"observe": "観察モード（見るだけ。お金は動かしません）"}
PAPER_ONLY = ("このコマンドは練習モード（config.yaml の mode: paper）のときだけ使えます。"
              "今は観察だけなので、止めたり抜けたりするものはありません。")
HELP = ("使えるコマンド\n"
        "/status 今の状況（判定の件数、上位3件、報酬トークンの値動き、データの集まり具合、練習の建玉）\n"
        "/report 今日の朝のレポートをもう一度送る\n"
        "/stop 練習を停止（新しい建玉を作らない。持っている建玉の計算と見張りは続ける）\n"
        "/exit_all 練習の建玉を全部閉じる（確認のボタンが出ます）\n"
        "/resume 練習を再開")


def _pct(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def _jst(ts: str | None, fmt: str = "%m/%d %H:%M") -> str:
    return "—" if not ts else datetime.fromisoformat(ts).astimezone(JST).strftime(fmt)


def _day_label(now: datetime) -> str:
    d = now.astimezone(JST)
    return f"{d.month}/{d.day}（{WEEK[d.weekday()]}）"


def _pair(r: dict[str, Any]) -> str:
    return r.get("pair") or f"{r.get('token0_symbol')}/{r.get('token1_symbol')}"


def _rank_line(i: int, r: dict[str, Any]) -> str:
    rng = f"（±{r['best_r']:g}%）" if r.get("best_r") is not None else ""
    where = f" [{r['venue_name']}]" if r.get("venue_name") else ""   # 会場が2つになったので（M6）
    return f"{i}. {SIGNAL_MARK.get(r['signal'], '')} {_pair(r)}{where} {_pct(r.get('net_daily_pct'))}{rng}"


def _reward_lines(home: dict[str, Any]) -> list[str]:
    out = []
    for t in (home.get("market") or {}).get("reward_tokens") or []:
        price = f"${t['price_usd']:.4g}" if t.get("price_usd") else "—"
        name = "報酬トークン" if t["symbol"] == "報酬トークン" else f"報酬トークン {t['symbol']}"
        out.append(f"{name}: {price}（24時間 {_pct(t['change_24h'] * 100 if t.get('change_24h') is not None else None, 1)}）")
    return out


def _collection_lines(home: dict[str, Any], now: datetime) -> list[str]:
    out = []
    since = now - timedelta(hours=24)
    for c in home.get("collection") or []:
        state = "⚠️ データが古い" if c.get("stale") else "OK"
        gaps = [g for g in c.get("gaps_7d") or [] if datetime.fromisoformat(g["end_slot"]) >= since]
        missed = sum(int(g.get("missed_slots") or 0) for g in gaps)
        gap = f"、24時間の欠損 {missed}回分" if missed else "、24時間の欠損なし"
        out.append(f"データ収集（{c['venue_id']}）: {state}。最後の成功 {_jst(c.get('last_ok_at'))}{gap}")
    return out


def status_text(home: dict[str, Any], now: datetime) -> str:
    """/status の返事（SPEC 12.5章）。"""
    c = home.get("counts") or {}
    top = sorted((home.get("greens") or []) + (home.get("near") or []),
                 key=lambda r: r.get("net_daily_pct") or -1e9, reverse=True)[:3]
    lines = [
        f"📡 Farm Radar {_jst(now.isoformat(), '%m/%d %H:%M')}（日本時間）",
        MODE_JA.get(home.get("mode"), home.get("mode") or ""),
        home.get("summary") or "",
        f"判定: 🟢{c.get('green', 0)} 🟡{c.get('yellow', 0)} 🔴{c.get('red', 0)}（計算 {_jst(home.get('scored_at'))}）",
    ]
    if top:
        lines.append("純日利の上位3件（総資産あたり）")
        lines += [_rank_line(i, r) for i, r in enumerate(top, 1)]
    lines += _reward_lines(home)
    lines += _collection_lines(home, now)
    return "\n".join(x for x in lines if x)


# --- 毎朝のレポート --------------------------------------------------------------------------

COST_JA = {
    "gamma": ("ガンマ損失", "値段が動くたびに、LPの中身が「上がった方を売って下がった方を買う」形に自動で入れかわることで出る損。"
                         "レンジを狭くするほど大きくなります。"),
    "rebalance": ("置き直しの費用", "レンジを外れたときに置き直す手間賃（ガス代と両替のずれ）。値動きが大きいほど回数が増えます。"),
    "hedge": ("ヘッジ費用", "値下がりに備えて perp（先物のようなもの）で反対の取引を持つときの手数料や資金調達の支払い。"),
    "haircut": ("報酬トークンの値下がり", "もらった報酬トークンの値段が下がると、同じ量をもらっても稼ぎが減ります。"),
    "direction_risk": ("値動きの損", "ヘッジできないトークンが値下がりしたときの損の見込み。"),
}


def _changes(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """24時間前と比べて判定が変わったプール。"""
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT p.token0_symbol || '/' || p.token1_symbol AS pair, s.signal AS now_sig,
                  (SELECT s2.signal FROM scores s2 WHERE s2.pool_id = s.pool_id AND s2.ts <= ?
                   ORDER BY s2.ts DESC LIMIT 1) AS old_sig
           FROM scores s JOIN pools p ON p.id = s.pool_id
           WHERE s.ts = (SELECT MAX(ts) FROM scores s3 WHERE s3.pool_id = s.pool_id)
           ORDER BY pair""", (since,)).fetchall()
    order = {"green": 0, "yellow": 1, "red": 2}
    ch = [r for r in rows if r["old_sig"] and r["old_sig"] != r["now_sig"]]
    ch.sort(key=lambda r: (order.get(r["now_sig"], 3), r["pair"]))
    return [f"{r['pair']} {SIGNAL_MARK.get(r['old_sig'], '')}→{SIGNAL_MARK.get(r['now_sig'], '')}" for r in ch]


def _learning_candidates(top: dict[str, Any] | None, home: dict[str, Any]) -> list[tuple[str, str, str]]:
    """今日の学びの候補（話題, 見出し, 本文）。データから作れるものだけを出す。"""
    out: list[tuple[str, str, str]] = []
    counts = home.get("counts") or {}
    if top:
        pair = _pair(top)
        costs = {k: top.get(k) or 0.0 for k in COST_JA}
        k = max(costs, key=lambda x: costs[x])
        if costs[k] > 0:
            name, explain = COST_JA[k]
            out.append(("biggest_cost", f"いちばん大きな損は「{name}」",
                        f"上位の {pair} では、1日の損でいちばん大きいのは「{name}」で、1日 ${costs[k]:.2f} の見込みです。"
                        f"{explain}"))
        det = top.get("details") or {}
        sn = det.get("sell_now")
        if sn and top.get("net_daily_pct") is not None:
            out.append(("hold_vs_sell", "報酬を持ち続けるか、すぐ売るか",
                        f"{pair} の純日利は、報酬を持ち続ける前提（判定に使う）で {_pct(top['net_daily_pct'])}、"
                        f"{sn['hours']:g}時間で売る前提（参考）で {_pct(sn['net_daily_pct'])} です。"
                        "差は報酬トークンの値下がりの引き方のちがいです。どちらが実際に近いかは、M5の練習で確かめます。"))
        irr, r = top.get("in_range_ratio"), top.get("best_r")
        if irr is not None and r is not None:
            out.append(("in_range", "レンジの中にいる時間",
                        f"{pair} の最適レンジ ±{r:g}% では、レンジの中にいる時間は1日の約{irr * 100:.0f}%の見込みです。"
                        "レンジを外れている間は報酬も手数料も入りません。狭いレンジは取り分が大きい代わりに外れやすくなります。"))
    for t in (home.get("market") or {}).get("reward_tokens") or []:
        if t.get("change_24h") is not None:
            out.append(("reward_token", f"報酬トークン {t['symbol']} の値動き",
                        f"{t['symbol']} は24時間で {_pct(t['change_24h'] * 100, 1)} 動きました。"
                        "報酬の価値は報酬トークンの値段で決まるので、値段が下がると、同じ量をもらっても稼ぎが減ります。"))
    if counts and not counts.get("green") and not counts.get("yellow"):
        out.append(("all_red", "今日はすべて見送り",
                    "攻め候補がない日は「何もしない」のが正解です。無理に入らないことも、ファーミングの大事な判断です。"))
    return out


def pick_learning(conn: sqlite3.Connection, cands: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """最近3日と同じ話題にならないように選ぶ。全部使っていたら、いちばん前のものにする。"""
    recent = [r[0] for r in conn.execute("SELECT topic FROM learning_notes ORDER BY ts DESC LIMIT 3")]
    for c in cands:
        if c[0] not in recent:
            return c
    return cands[0] if cands else None


def build_daily_report(conn: sqlite3.Connection, home: dict[str, Any], top_rows: list[dict[str, Any]],
                       now: datetime) -> tuple[str, tuple[str, str, str] | None]:
    """毎朝のレポート（SPEC 8章: 上位5件、判定の変化、今日の学び）。(本文, 今日の学び) を返す。"""
    c = home.get("counts") or {}
    lines = [f"☀️ Farm Radar 朝のレポート {_day_label(now)}", home.get("summary") or "",
             f"判定: 🟢{c.get('green', 0)} 🟡{c.get('yellow', 0)} 🔴{c.get('red', 0)}", ""]
    lines.append(f"■ 純日利の上位{len(top_rows)}件（総資産あたり）" if top_rows else "■ 上位: まだ判定がありません")
    lines += [_rank_line(i, r) for i, r in enumerate(top_rows, 1)]
    ch = _changes(conn, now)
    lines += ["", "■ 判定の変化（24時間）"]
    lines += ch[:10] if ch else ["変化なし"]
    if len(ch) > 10:
        lines.append(f"ほか {len(ch) - 10}件")
    lines += ["", "■ 市場"] + _reward_lines(home) + _collection_lines(home, now)
    learning = pick_learning(conn, _learning_candidates(top_rows[0] if top_rows else None, home))
    if learning:
        lines += ["", f"■ 今日の学び: {learning[1]}", learning[2]]
    lines += ["", "くわしくは画面の「学ぶ」タブ → 毎朝のレポート"]
    return "\n".join(lines).replace("\n\n\n", "\n\n"), learning


def top_scores(conn: sqlite3.Connection, n: int) -> list[dict[str, Any]]:
    from ..db import database as db
    out = []
    for r in db.latest_scores(conn):
        if r["net_daily_pct"] is None:
            continue
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        out.append(d)
        if len(out) >= n:
            break
    return out


def jst_day(now: datetime) -> str:
    return now.astimezone(JST).strftime("%Y-%m-%d")


def save_daily_report(conn: sqlite3.Connection, now: datetime, body: str,
                      learning: tuple[str, str, str] | None, pool_id: str | None) -> bool:
    """その日のレポートを保存する。すでにあれば何もしない（False）。"""
    ts = now.astimezone(UTC).isoformat(timespec="seconds")
    cur = conn.execute("INSERT OR IGNORE INTO daily_reports(day, ts, body_ja) VALUES (?,?,?)",
                       (jst_day(now), ts, body))
    if cur.rowcount and learning:
        conn.execute("INSERT INTO learning_notes(ts, pool_id, title, body_ja, topic) VALUES (?,?,?,?,?)",
                     (ts, pool_id, learning[1], learning[2], learning[0]))
    conn.commit()
    return cur.rowcount > 0
