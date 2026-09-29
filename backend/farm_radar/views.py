"""画面（M3）用に、保存済みのデータを見やすい形にまとめる（SPEC 7章）。計算式そのものは scoring/ にある。

- 損益は 7.6章の6区分（収入 / 方向 / ガンマ / ヘッジ / 報酬トークン値下がり / その他）で表す。
  M3 はまだ建玉がないので、1時間ごとのスコア（予測）を使う。実際の損益（実現 / 未実現）は M5 の「練習」で出す。
- 「1日」は日本時間の 0:00〜24:00（7.6章）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .scoring import volatility as vol

JST = ZoneInfo("Asia/Tokyo")
LUCK_THRESHOLD = 0.5            # 本業以外の割合がこれを超えたら「運の要素が大きい」（7.6章 3.）
APY_NOTE = "今日のペースが1年続いた場合の仮の数字"
CATEGORIES = ("income", "direction", "gamma", "hedge", "haircut", "other")
CATEGORY_JA = {
    "income": "収入（報酬）", "direction": "方向（値動き）", "gamma": "ガンマ",
    "hedge": "ヘッジ", "haircut": "報酬トークン値下がり", "other": "その他",
}


def breakdown(income: float, gamma: float, rebalance: float, hedge: float, haircut: float,
              direction_risk: float) -> dict[str, float | bool]:
    """スコアの項目（すべて正の「損の大きさ」）を、7.6章の6区分（損はマイナス）に並べ替える。

    リバランス費用（ガス代と両替のずれ）は「その他」に入れる。6つの合計は純損益と一致する。
    """
    parts = {
        "income": income, "direction": -direction_risk, "gamma": -gamma,
        "hedge": -hedge, "haircut": -haircut, "other": -rebalance,
    }
    net = sum(parts.values())
    core = income - gamma                                   # 本業の稼ぎ
    side = parts["direction"] + parts["hedge"] + parts["haircut"] + parts["other"]
    denom = abs(core) + abs(side)
    luck = abs(side) / denom if denom > 0 else 0.0
    return {
        **parts, "net": net,
        "hedge_gap": parts["direction"] + parts["hedge"],   # ヘッジのずれ。0に近いほどヘッジが効いている
        "core": core, "luck_ratio": luck, "lucky": luck > LUCK_THRESHOLD,
    }


def row_breakdown(r: sqlite3.Row | dict) -> dict[str, float | bool] | None:
    if r["net_daily_pct"] is None:
        return None
    return breakdown(r["income"] or 0.0, r["gamma"] or 0.0, r["rebalance"] or 0.0, r["hedge"] or 0.0,
                     r["haircut"] or 0.0, r["direction_risk"] or 0.0)


def scale(b: dict[str, Any], k: float) -> dict[str, Any]:
    """金額の項目だけ k 倍する（1日あたり → 1時間あたり など）。"""
    out = dict(b)
    for key in (*CATEGORIES, "net", "hedge_gap", "core"):
        out[key] = b[key] * k
    return out


def apy(daily_usd: float, capital: float) -> float:
    """1日の金額から年利（%）。単利で365倍（7.6章 5. の注記を付けて表示する）。"""
    return daily_usd * 365 / capital * 100 if capital else 0.0


def average_breakdown(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """区分ごとの平均。「戦略開始以降の1日平均」（7.7章 4.）に使う。"""
    if not items:
        return None
    avg = {k: sum(float(x[k]) for x in items) / len(items) for k in (*CATEGORIES, "net", "hedge_gap", "core")}
    side = avg["direction"] + avg["hedge"] + avg["haircut"] + avg["other"]
    denom = abs(avg["core"]) + abs(side)
    avg["luck_ratio"] = abs(side) / denom if denom > 0 else 0.0
    avg["lucky"] = avg["luck_ratio"] > LUCK_THRESHOLD
    return avg


def hourly_bars(history: list[dict[str, Any]], now: datetime, hours: int = 48) -> dict[str, Any]:
    """1時間ごとの純損益（予測）の棒グラフ用データ（7.7章 3.）。

    その時間のスコアの「1日の純損益 ÷ 24」をその1時間の見込みとする。スコアがない時間は空ける。
    24時間移動平均、いちばん稼いだ時間・損した時間、米国市場が開いている時間の印を付ける。
    """
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours - 1)
    by_hour: dict[datetime, float] = {}
    for h in history:
        if h["net_usd"] is None:
            continue
        t = datetime.fromisoformat(h["ts"]).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        by_hour[t] = h["net_usd"] / 24
    bars = []
    t = start
    while t <= end:
        v = by_hour.get(t)
        last24 = [by_hour[x] for x in (t - timedelta(hours=i) for i in range(24)) if x in by_hour]
        bars.append({
            "ts": t.isoformat(), "jst": t.astimezone(JST).strftime("%m/%d %H時"),
            "net_usd": v, "ma24_usd": sum(last24) / len(last24) if last24 else None,
            "us_open": vol.us_market_open(int((t + timedelta(minutes=30)).timestamp())),
        })
        t += timedelta(hours=1)
    have = [b for b in bars if b["net_usd"] is not None]
    best = max(have, key=lambda b: b["net_usd"]) if have else None
    worst = min(have, key=lambda b: b["net_usd"]) if have else None
    return {"bars": bars, "best": best, "worst": worst}


def total_assets(capital: float, lp: float, margin: float, reserve: float, history: list[dict[str, Any]],
                 now: datetime, hours: int = 48) -> dict[str, Any]:
    """総資産カード（7.7章 2.）。M3 はまだ建玉がないので「この48時間このプールにいたら」の推定。"""
    since = now - timedelta(hours=hours)
    pts = sorted((h for h in history if h["net_usd"] is not None and datetime.fromisoformat(h["ts"]) >= since),
                 key=lambda h: h["ts"])
    value, income, series = capital, 0.0, []
    for h in pts:
        value += h["net_usd"] / 24
        income += (h["income_usd"] or 0.0) / 24
        series.append({"ts": h["ts"], "jst": datetime.fromisoformat(h["ts"]).astimezone(JST).strftime("%m/%d %H時"),
                       "value": value})
    change = value - capital
    return {
        "estimated": True, "capital": capital, "value": value, "change_usd": change,
        "change_pct": change / capital * 100 if capital else 0.0, "series": series,
        "parts": {"lp": capital * lp, "wallet": capital * reserve, "margin": capital * margin,
                  "unclaimed": income},
    }


def jst_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(JST).strftime("%Y-%m-%d")


def pool_history(conn: sqlite3.Connection, pool_id: str, since: datetime) -> list[dict[str, Any]]:
    """プールのスコアの履歴（古い順）。時系列グラフと棒グラフに使う。"""
    rows = conn.execute(
        "SELECT * FROM scores WHERE pool_id=? AND ts>=? ORDER BY ts", (pool_id, since.isoformat(timespec="seconds"))
    ).fetchall()
    out = []
    for r in rows:
        det = json.loads(r["details_json"] or "{}")
        inp = det.get("inputs") or {}
        b = row_breakdown(r)
        out.append({
            "ts": r["ts"], "signal": r["signal"], "net_daily_pct": r["net_daily_pct"],
            "net_usd": b["net"] if b else None, "income_usd": r["income"],
            "breakdown": b, "reward_usd_day": inp.get("reward_usd_day"),
            "liquidity_staked_inrange": inp.get("liquidity_staked_inrange"),
            "staked_inrange_usd": _best_row(det, r["best_r"]).get("staked_inrange_usd"),
            "reward_token_usd": inp.get("reward_token_usd"),
        })
    return out


def _best_row(det: dict[str, Any], best_r: float | None) -> dict[str, Any]:
    for x in det.get("ranges") or []:
        if best_r is not None and abs(x.get("r_pct", -1) - best_r) < 1e-9:
            return x
    return {}


def daily_average_since_start(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """戦略開始（このプールの最初のスコア）以降の1日平均。日本時間の日ごとに平均し、それを平均する。"""
    days: dict[str, list[dict[str, Any]]] = {}
    for h in history:
        if h["breakdown"]:
            days.setdefault(jst_day(h["ts"]), []).append(h["breakdown"])
    per_day = [average_breakdown(v) for v in days.values()]
    avg = average_breakdown([d for d in per_day if d])
    if avg:
        avg["days"] = len(per_day)
        avg["since"] = history[0]["ts"] if history else None
    return avg


def today_breakdown(history: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """今日（日本時間）のスコアの平均。"""
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    return average_breakdown([h["breakdown"] for h in history if h["breakdown"] and jst_day(h["ts"]) == today])


def series_change(points: list[tuple[int, float]], now_s: int, hours: float) -> float | None:
    """価格の並びから、hours 時間前と比べた変化率。"""
    if not points:
        return None
    target = now_s - int(hours * 3600)
    older = [p for p in points if p[0] <= target]
    if not older or older[-1][1] <= 0:
        return None
    return points[-1][1] / older[-1][1] - 1


def hourly_downsample(points: list[tuple[int, float]]) -> list[dict[str, Any]]:
    """1時間に1点に間引く（スパークライン用）。"""
    by: dict[int, float] = {}
    for t, v in points:
        by[t - t % 3600] = v
    return [{"ts": datetime.fromtimestamp(t, UTC).isoformat(), "v": v} for t, v in sorted(by.items())]


def median_or_none(xs: list[float]) -> float | None:
    return median(xs) if xs else None
