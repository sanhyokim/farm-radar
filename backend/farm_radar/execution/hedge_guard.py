"""保険（Lighter の perp の仮想の売り）の守り（N4b。SPEC 13.1 の練習に入れること1・追加の決定 4 と 11）。

1. 保険に預けるお金（担保）: 2026-10-03 直し（オーナー「預けすぎになっていれば直して」）。前は総額の 40% 決め打ち
   （今の版の 55% / 40% / 5%）で、ドルのコインが半分入るプールでは2倍以上預けていた。今は「探す」と同じ自動の計算:
   売る量（中身の値動きするコインのドル）× margin_need（値段が u 上がっても強制決済されない
   割合 = max(最初に要る割合, u ＋ (1 ＋ u) × 維持の割合)）。u は市場ごとに max(50%, 14日のうちのいちばんの上げ)
   （2026-10-04 オーナー決定 A。withstand_for）。予備はチェーンのガス代の分だけ（$20 など）。残りをプールに置く。
   保険のない建玉は0。守るの画面では、このお金を「置いている場所（Lighter）」として数え、上限に入れる。
2. 強制決済までの余裕（Lighter の決まり。venues/lighter.yaml の margin）:
   - 担保の今の価値 = 預けたお金 ＋ 保険の損益（資金調達料を引いたもの）
   - 維持に要る額 = 売りの額（今の値段）× 維持の割合（feeds の lighter_markets。読めなければ guard.hedge_mmf_fallback の仮の値）
   - 余裕 = 担保の今の価値 − 維持に要る額。はじめの余裕は「預けたお金 − 売りの額（売った値段）× 維持の割合」
   - 余裕がはじめの guard.hedge_alert_buffer_frac（仮 50%）を切ったら知らせる。0以下になったら強制決済（段階1。建玉を閉じる）
   - 知らせへの対応は「お金を足す」（いくら足せば、はじめの余裕に戻るか）か「出る」（閉じる費用の見込み）を出す
3. 値段が幅から出たときの保険（追加の決定 11）: 境目から幅の 10% 分外に出たまま 30分続いたら、保険を中身に合わせる
   （上に出たら中身は値動きしないコインだけ → 保険を閉じる。下に出たら中身は値動きするコインだけ → 保険を大きくする）。
   直したあと1時間は直さない。値段が幅の中（境目から幅の 10% 分内側）に戻って30分続いたときも、同じ決まりで中身に合わせて戻す。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config

# 中身と保険の量の差が、これより小さければ直さない（売りの量の 5%）
MATCH_TOLERANCE = 0.05


def margin_of(pos: sqlite3.Row | dict[str, Any], config: Config) -> float:
    """この建玉で保険に預けているお金（ドル）。保険がなければ0。"""
    st = json.loads(pos["state_json"] or "{}")
    if "hedge_margin" in st:
        return float(st["hedge_margin"])
    hedges = json.loads(pos["hedges_json"] or "[]")
    return float(pos["capital"]) * config.scoring.allocation_hedge_margin if hedges else 0.0


def _mmf_table(path: Path) -> dict[int, float]:
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT market_id, maintenance_margin_fraction FROM lighter_markets").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {int(m): float(v) / 10000 for m, v in rows if m is not None and v is not None}


def mmf_for(config: Config, market_id: int | None, table: dict[int, float] | None = None) -> tuple[float, bool]:
    """維持の割合と、それが Lighter から読んだ値か（False なら仮の値）。"""
    table = _mmf_table(config.feeds.database_path) if table is None else table
    if market_id is not None and int(market_id) in table:
        return table[int(market_id)], True
    return config.guard.hedge_mmf_fallback, False


def lighter_margin_table(path: Path) -> dict[int, tuple[float | None, float | None]]:
    """Lighter の銘柄ごとの (最初に要る割合, 維持の割合)。feeds の lighter_markets（API の値 ÷ 10000）。"""
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT market_id, initial_margin_fraction, maintenance_margin_fraction "
                                "FROM lighter_markets").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {int(m): (None if i is None else float(i) / 10000, None if v is None else float(v) / 10000)
            for m, i, v in rows if m is not None}


def margin_need(imf: float | None, mmf: float | None, withstand_rise: float) -> float:
    """売り（保険）1ドルあたりに預けるお金: max(最初に要る割合, u ＋ (1 ＋ u) × 維持の割合)（「探す」と同じ。2026-10-02 オーナー決定）。

    値段が u 上がると売りは u だけ損をし、残りの担保が「(1 ＋ u) × 維持の割合」を下回ると強制決済になる。
    例: u = 50%、維持の割合 5% → 0.5 ＋ 1.5 × 0.05 = 0.575（売る $300 に $172.5）。
    """
    return max(imf or 0.0, withstand_rise + (1 + withstand_rise) * (mmf or 0.0))


# --- 耐える上げ幅（2026-10-04 オーナー決定 A。SPEC 13.1 の追加の決定 16） ------------------------------------
# 市場ごとに、Lighter の値段の過去（feeds の lighter_price_history。1時間の足）で「いる日数（stay_days。14日）のうちに
# いちばん上がった幅」を出し、それと opportunities.hedge_withstand_rise_pct（50%）の大きい方に耐えるだけ預ける。
# 値段の過去がまだない市場は 50%。計算は1時間に1回まで（足は1日1回しか増えない）。
WITHSTAND_TTL_S = 3600
_withstand_cache: dict[tuple[str, float], tuple[float, dict[int, float]]] = {}


def withstand_from_conn(conn: sqlite3.Connection, window_s: float) -> dict[int, float]:
    """市場ごとの、window_s のうちにいちばん上がった幅（割合。0.584 = 58.4%）。"""
    from ..backtest import max_rise_hl       # backtest は scoring を読むので、ここで読む（循環を避ける）

    try:
        rows = conn.execute("SELECT market_id, ts, high, low, close FROM lighter_price_history WHERE high IS NOT NULL "
                            "AND low IS NOT NULL AND close IS NOT NULL ORDER BY market_id, ts").fetchall()
    except sqlite3.Error:
        return {}
    by: dict[int, list[tuple[int, float, float, float]]] = {}
    for mid, t, h, lo, c in rows:
        by.setdefault(int(mid), []).append((int(t), float(h), float(lo), float(c)))
    out = {}
    for mid, cs in by.items():
        mr = max_rise_hl(cs, window_s)
        if mr:
            out[mid] = mr["rise_pct"] / 100
    return out


def withstand_table(path: Path, window_days: float) -> dict[int, float]:
    key = (str(path), float(window_days))
    hit = _withstand_cache.get(key)
    if hit and time.monotonic() - hit[0] < WITHSTAND_TTL_S:
        return hit[1]
    table: dict[int, float] = {}
    if path.exists():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                table = withstand_from_conn(conn, window_days * 86400)
            finally:
                conn.close()
        except sqlite3.Error:
            table = {}
    _withstand_cache[key] = (time.monotonic(), table)
    return table


def withstand_for(config: Config, market_id: int | None, table: dict[int, float] | None = None) -> float:
    """この市場の保険が耐える上げ幅（割合）。per_market なら max(50%, 14日のうちのいちばんの上げ)、fixed なら 50%。"""
    op = config.opportunities
    floor = op.hedge_withstand_rise_pct / 100
    if op.hedge_withstand_mode != "per_market" or market_id is None:
        return floor
    table = withstand_table(config.feeds.database_path, op.stay_days) if table is None else table
    return max(floor, table.get(int(market_id), 0.0))


def need_for(config: Config, hedge_id: str | None, market_id: int | None,
             table: dict[int, tuple[float | None, float | None]] | None = None) -> dict[str, Any]:
    """この保険の売り1ドルあたりに預けるお金と、その元の数字。Lighter の値が読めなければ維持の割合は仮の値。"""
    table = lighter_margin_table(config.feeds.database_path) if table is None else table
    imf = mmf = None
    if (hedge_id or "lighter") == "lighter" and market_id is not None and int(market_id) in table:
        imf, mmf = table[int(market_id)]
    from_lighter = mmf is not None
    mmf = mmf if mmf is not None else config.guard.hedge_mmf_fallback
    w = withstand_for(config, market_id if (hedge_id or "lighter") == "lighter" else None)
    return {"need": margin_need(imf, mmf, w), "imf": imf, "mmf": mmf, "from_lighter": from_lighter,
            "withstand_rise_pct": w * 100}


def split(capital: float, reserve_usd: float, hedged_shares: list[tuple[float, float]]) -> dict[str, float]:
    """総額を、プールに置く分・保険に預ける分・予備に分ける（ドル）。

    hedged_shares = 保険を掛けるコインごとの (プールの中身のうちそのコインの割合, 売り1ドルあたりに預けるお金)。
    プール P、保険 = P × Σ(割合 × 預ける割合)、予備 = reserve_usd。P ＋ 保険 ＋ 予備 = 総額 になるように P を決める。
    """
    reserve = min(max(reserve_usd, 0.0), capital)
    per_pool = sum(sh * need for sh, need in hedged_shares)
    pool = (capital - reserve) / (1 + per_pool)
    return {"pool": pool, "hedge_margin": pool * per_pool, "reserve": reserve, "margin_per_pool": per_pool}


def _prices(conn: sqlite3.Connection, pos: sqlite3.Row) -> dict[str, float]:
    st = json.loads(pos["state_json"] or "{}")
    return {k: float(v) for k, v in (st.get("last_prices") or {}).items() if v is not None and k != "reward"}


def margin_status(conn: sqlite3.Connection, config: Config, pos: sqlite3.Row, rise_pct: float = 0.0,
                  mmf_table: dict[int, float] | None = None) -> dict[str, Any] | None:
    """強制決済までの余裕。rise_pct を入れると「値動きするコインが今から rise_pct% 上がったら」を計算する（練習の試し）。

    保険のない建玉は None。
    """
    hedges = [h for h in json.loads(pos["hedges_json"] or "[]") if float(h.get("size") or 0.0) > 0]
    margin = margin_of(pos, config)
    if not hedges or margin <= 0:
        return None
    st = json.loads(pos["state_json"] or "{}")
    prices = _prices(conn, pos)
    x = rise_pct / 100
    notional_now = notional_open = maint = maint_open = 0.0
    hedge_now = float((st.get("cum") or {}).get("hedge", 0.0))
    from_api = True
    for h in hedges:
        p = prices.get(h["token"], float(h["entry"]))
        mmf, ok = mmf_for(config, h.get("market_id"), mmf_table)
        from_api = from_api and ok
        size = float(h["size"])
        p_x = p * (1 + x)
        notional_now += size * p_x
        notional_open += size * float(h["entry"])
        maint += size * p_x * mmf
        maint_open += size * float(h["entry"]) * mmf
        hedge_now -= size * (p_x - p)                    # 上がった分だけ売りが損をする
    equity = margin + hedge_now
    buffer = equity - maint
    initial = margin - maint_open
    frac = buffer / initial if initial > 0 else None
    # 今の値段からあと何%上がると強制決済か（余裕が0になる上がり幅。値動きするコインがそろって動くとみなす）
    per_pct = sum(float(h["size"]) * prices.get(h["token"], float(h["entry"])) * (1 + x) for h in hedges) / 100
    mmf_avg = maint / notional_now if notional_now else 0.0
    to_liq_pct = buffer / (per_pct * (1 + mmf_avg)) if per_pct > 0 and buffer > 0 else 0.0
    g = config.guard
    state = "liquidated" if buffer <= 0 else ("alert" if frac is not None and frac < g.hedge_alert_buffer_frac else "ok")
    return {
        "rise_pct": rise_pct, "margin_usd": margin, "hedge_pnl_usd": hedge_now, "equity_usd": equity,
        "notional_usd": notional_now, "notional_open_usd": notional_open, "maintenance_usd": maint,
        "buffer_usd": buffer, "buffer_initial_usd": initial, "buffer_frac": frac, "alert_frac": g.hedge_alert_buffer_frac,
        "to_liquidation_pct": to_liq_pct, "state": state, "mmf_from_lighter": from_api,
        "add_to_restore_usd": max(0.0, initial - buffer) if state != "ok" else 0.0,
    }


def message_ja(pair: str, ms: dict[str, Any]) -> str:
    if ms["state"] == "liquidated":
        return (f"{pair} の保険（Lighter の売り）が強制決済の線に届きました: 担保 ${ms['equity_usd']:,.2f}、"
                f"維持に要る額 ${ms['maintenance_usd']:,.2f}。")
    frac = (ms["buffer_frac"] or 0.0) * 100
    return (f"{pair} の保険の余裕（担保 − 維持に要る額）が ${ms['buffer_usd']:,.2f} で、はじめの{frac:.0f}%です"
            f"（知らせる目安は{ms['alert_frac'] * 100:g}%未満。仮）。あと約{ms['to_liquidation_pct']:.0f}%上がると強制決済です。"
            f"対応は「お金を足す」（${ms['add_to_restore_usd']:,.2f} 足すと、はじめの余裕に戻ります）か「出る」です。")


# --- 値段が幅から出たときの保険（追加の決定 11） ---------------------------------------------------------

def edge_zone(price: float, lower: float, upper: float, r: float, buffer_frac: float) -> str:
    """境目の余裕を入れた、値段の場所: above / below（幅の外で、余裕より外）/ inside（幅の中で、余裕より内）/ edge（その間）。

    余裕 = 幅（±r）の buffer_frac 倍。例: ±2% の幅なら 0.2%。
    """
    b = r * buffer_frac
    if price > upper * (1 + b):
        return "above"
    if price < lower * (1 - b):
        return "below"
    if lower * (1 + b) <= price <= upper * (1 - b):
        return "inside"
    return "edge"


def track_zone(st: dict[str, Any], zone: str, ts: str) -> None:
    """state に、今の場所に入った時刻を書く（場所が変わったら書き直す）。"""
    if st.get("hedge_zone") != zone:
        st["hedge_zone"] = zone
        st["hedge_zone_since"] = ts


def targets(pos: sqlite3.Row, pool: sqlite3.Row, price: float) -> dict[str, float]:
    """今の値段での中身（トークンごとの量）。保険はこれに合わせる。"""
    from .paper import lp_amounts       # paper からもこのファイルを読むので、ここで読む

    x, y = lp_amounts(float(pos["liquidity"]), price, pos["lower"], pos["upper"], int(pool["token0_decimals"]),
                      int(pool["token1_decimals"]))
    return {pool["token0"].lower(): x, pool["token1"].lower(): y}


def adjust_due(pos: sqlite3.Row, pool: sqlite3.Row, price: float, now: datetime, config: Config) -> str | None:
    """保険を中身に合わせる時なら、その理由（above / below / inside）。まだなら None。"""
    hedges = json.loads(pos["hedges_json"] or "[]")
    if not hedges:
        return None
    st = json.loads(pos["state_json"] or "{}")
    zone, since = st.get("hedge_zone"), st.get("hedge_zone_since")
    g = config.guard
    if zone not in ("above", "below", "inside") or not since:
        return None
    # 幅の中に戻ったときに直すのは、幅の外で保険を直したあとだけ（幅の中のふだんの揺れでは保険はそのまま。今の版と同じ）
    if zone == "inside" and not st.get("hedge_matched_out"):
        return None
    if now - datetime.fromisoformat(since) < timedelta(minutes=g.hedge_edge_wait_minutes):
        return None
    last = st.get("hedge_adjusted_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=g.hedge_cooldown_minutes):
        return None
    want = targets(pos, pool, price)
    for h in hedges:
        size, target = float(h.get("size") or 0.0), want.get(h["token"], 0.0)
        ref = max(size, target, float(h.get("size_open") or 0.0))
        if ref > 0 and abs(target - size) > MATCH_TOLERANCE * ref:
            return zone
    return None
