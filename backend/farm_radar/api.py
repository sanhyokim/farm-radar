"""APIと画面（M3）。`uvicorn farm_radar.api:app` で起動する。

- /api/... … データ（JSON）
- /       … スマホ向けの画面（frontend/ を build したもの。無ければAPIだけ動く）
"""

from __future__ import annotations

import json
import os
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from . import views
from .collectors.completeness import check
from .config import REPO_ROOT, contract_address, load_config, load_venue
from .db import database as db
from .execution import views as paper_views
from .execution.paper import PaperError, PaperExecutor
from .execution.base import PositionRef
from .execution import risk_job
from .notify.telegram import settings_from_env
from .scoring import volatility as vol
from .scoring.run import EXTERNAL_SOURCE, merge_series, own_series
from .tokens import load_tokens

app = FastAPI(title="Farm Radar")


@contextmanager
def _open():
    config = load_config()
    conn = db.connect(config.database_path)
    try:
        yield config, conn
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(UTC)


@app.get("/api/health")
def health() -> dict:
    config = load_config()
    conn = db.connect(config.database_path)
    try:
        venues = []
        for venue_id in config.venues:
            last = conn.execute(
                "SELECT * FROM collection_runs WHERE venue_id=? ORDER BY id DESC LIMIT 1", (venue_id,)
            ).fetchone()
            last_ok = conn.execute(
                "SELECT finished_at FROM collection_runs WHERE venue_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                (venue_id,),
            ).fetchone()
            stale = True
            if last_ok and last_ok["finished_at"]:
                age = datetime.now(UTC) - datetime.fromisoformat(last_ok["finished_at"])
                stale = age > timedelta(minutes=config.stale_after_minutes)
            r = check(conn, venue_id, minutes=config.snapshot_minutes)
            gaps = [dict(g) for g in db.list_gaps(conn, venue_id, datetime.now(UTC) - timedelta(days=7))]
            venues.append({
                "venue_id": venue_id,
                "last_run": dict(last) if last else None,
                "last_ok_at": last_ok["finished_at"] if last_ok else None,
                "stale": stale,
                "last_24h": {"expected": r.expected, "ok": r.ok, "missing": len(r.missing), "not_ok": len(r.not_ok)},
                "gaps_7d": gaps,   # 収集が止まっていた期間（M3の画面で「欠損」として表示する）
            })
        return {"mode": config.mode, "venues": venues}
    finally:
        conn.close()


@app.get("/api/venues")
def venues() -> dict:
    """会場ごとの確認状況と警告（C4 など）、4条件のランプ、報酬トークン価格と TVL の推移（SPEC 7.2章）。"""
    config = load_config()
    conn = db.connect(config.database_path)
    out = []
    for venue_id in config.venues:
        v = load_venue(venue_id, config.root)
        contracts = {
            name: {
                "address": c.get("address"),
                "unverified": c.get("unverified", True),
                "sourcify_match": c.get("sourcify_match"),
                "checked_at": c.get("checked_at"),
            }
            for name, c in (v.get("contracts") or {}).items()
        }
        mechanics = {
            name: {k: m.get(k) for k in ("value", "unverified", "owner_acknowledged", "evidence_function")}
            for name, m in (v.get("mechanics") or {}).items()
        }
        extra = _venue_extra(conn, config, v)
        out.append({
            "venue_id": venue_id, "name": v.get("name"), "audited": v.get("audited"),
            "launch_date": v.get("launch_date"), "warnings": v.get("warnings") or [],
            "contracts": contracts, "mechanics": mechanics,
            "unverified_contracts": [n for n, c in contracts.items() if c["unverified"]],
            **extra,
        })
    conn.close()
    return {"venues": out}


def _reward_token_prices(conn, config, v: dict, days: float = 7) -> list[tuple[int, float]]:
    token = (contract_address(v, "reward_token") or "").lower()
    if not token:
        return []
    book = load_tokens(v["chain"]["id"], config.root)
    since = _now() - timedelta(days=days)
    tok, _ = own_series(conn, v["id"], book.stablecoins, since)
    # 自分の記録がまだ短い間は、スコア計算でためた外部の1時間足（GeckoTerminal）で昔の分を補う
    return merge_series(db.token_price_series(conn, token, EXTERNAL_SOURCE, since), tok.get(token, []))


def _symbol(conn, token: str | None) -> str | None:
    if not token:
        return None
    t = token.lower()
    row = conn.execute("""SELECT CASE WHEN lower(token0)=? THEN token0_symbol ELSE token1_symbol END FROM pools
                          WHERE lower(token0)=? OR lower(token1)=? LIMIT 1""", (t, t, t)).fetchone()
    return row[0] if row else None


def _venue_extra(conn, config, v: dict) -> dict:
    """会場カードの追加情報: 稼働日数、報酬トークンの価格の推移、TVL の推移、4条件のランプ。"""
    now = _now()
    now_s = int(now.timestamp())
    prices = _reward_token_prices(conn, config, v)
    tvl_rows = conn.execute(
        """SELECT ts, SUM(tvl_usd) AS tvl, COUNT(tvl_usd) AS n FROM scores WHERE venue_id=? AND ts>=?
           GROUP BY ts ORDER BY ts""", (v["id"], (now - timedelta(days=7)).isoformat(timespec="seconds"))).fetchall()
    latest = [dict(r) for r in db.latest_scores(conn, v["id"])]
    launch = v.get("launch_date")
    age_days = (now.date() - date.fromisoformat(str(launch))).days if launch else None
    return {
        "age_days": age_days,
        "reward_token": {
            "symbol": _symbol(conn, contract_address(v, "reward_token")),
            "price_usd": prices[-1][1] if prices else None,
            "change_24h": views.series_change(prices, now_s, 24),
            "change_7d": views.series_change(prices, now_s, 24 * 7 - 1),
            "sparkline": views.hourly_downsample(prices),
        },
        "tvl": [{"ts": r["ts"], "v": r["tvl"]} for r in tvl_rows if r["n"]],
        "conditions": _conditions(v, latest, config),
    }


def _conditions(v: dict, latest: list[dict], config) -> list[dict]:
    """SPEC 2章の4条件のランプ（ok / warn / bad / unknown）と一言の説明。会場全体の目安。"""
    warns = [json.loads(r.get("warnings_json") or "[]") for r in latest]
    flat = [w for ws in warns for w in ws]
    scored = [r for r in latest if r["net_daily_pct"] is not None]
    cap = config.scoring.total_capital_usd
    if not scored:
        return [{"code": c, "state": "unknown", "text": "まだ計算していません"} for c in ("C1", "C2", "C3", "C4")]

    rich = [r for r in scored if (r["income"] or 0) / cap * 100 >= config.scoring.green_min_pct]
    c1 = {"code": "C1", "state": "ok" if rich else "warn",
          "text": f"ボーナスが十分なプール {len(rich)} / {len(scored)} 件（収入が総資産の1日 {config.scoring.green_min_pct}% 以上）"}
    major = next((w for w in flat if w.get("code") == "C2" and w.get("level") == "major"), None)
    c2 = {"code": "C2", "state": "bad" if major else "ok",
          "text": major["message_ja"] if major else "報酬トークンの大きな値下がりはありません"}
    hedge = [r for r in scored if r["has_perp"]]
    c3 = {"code": "C3", "state": "ok" if hedge else "warn",
          "text": f"ヘッジできるプール {len(hedge)} / {len(scored)} 件"}
    c4_notes = [w.get("title_ja") or w.get("key") for w in v.get("warnings") or [] if w.get("code") == "C4"]
    if v.get("audited") is None:
        c4_notes.append("監査の有無が未確認")
    unverified = [n for n, c in (v.get("contracts") or {}).items() if c.get("unverified", True)]
    if unverified:
        c4_notes.append(f"未確認のコントラクト {len(unverified)} 件")
    c4 = {"code": "C4", "state": "bad" if unverified else ("warn" if c4_notes else "ok"),
          "text": " / ".join(c4_notes) if c4_notes else "確認済み"}
    return [c1, c2, c3, c4]


@app.get("/api/alerts")
def alerts(days: int = 7) -> dict:
    """記録された通知（報酬の毎秒量の急減など）。M4 で Discord / Telegram にも送る。"""
    config = load_config()
    conn = db.connect(config.database_path)
    try:
        rows = db.list_alerts(conn, datetime.now(UTC) - timedelta(days=days))
        return {"alerts": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/scores")
def scores(venue: str | None = None) -> dict:
    """プールごとの最新の判定（M2）。net_daily_pct は総資産あたりの%で、判定に使う値。"""
    import json

    config = load_config()
    conn = db.connect(config.database_path)
    try:
        rows = db.latest_scores(conn, venue)
        out, counts = [], {"green": 0, "yellow": 0, "red": 0}
        for r in rows:
            d = dict(r)
            d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
            d["details"] = json.loads(d.pop("details_json") or "{}")
            d["pair"] = f"{d.get('token0_symbol')}/{d.get('token1_symbol')}"
            counts[d["signal"]] = counts.get(d["signal"], 0) + 1
            out.append(d)
        return {"counts": counts, "judge_basis": "総資産あたりの純日利（%）", "scores": out}
    finally:
        conn.close()


# --- 画面（M3）用 -------------------------------------------------------------------------

def _score_dict(r) -> dict:
    d = dict(r)
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    d["details"] = json.loads(d.pop("details_json") or "{}")
    d["pair"] = f"{d.get('token0_symbol')}/{d.get('token1_symbol')}"
    return d


def _summary_sentence(rows: list[dict]) -> str:
    """ホーム最上部の「今日の結論」を1文で（SPEC 7.1章）。"""
    if not rows:
        return "まだ判定がありません。最初の計算を待っています。"
    greens = [r for r in rows if r["signal"] == "green"]
    yellows = [r for r in rows if r["signal"] == "yellow"]
    if greens:
        s = f"攻め候補 {len(greens)}件。"
        if all(r.get("is_stock_pair") for r in greens):
            s += "いずれも株ペア。"
        return s
    reds = [r for r in rows if r["signal"] == "red"]
    majors = Counter(w["message_ja"] for r in reds for w in r["warnings"] if w.get("level") == "major")
    if yellows:
        return f"攻め候補はありません。様子見 {len(yellows)}件。"
    if majors:
        msg, n = majors.most_common(1)[0]
        if n == len(rows):
            return f"攻め候補はありません。すべて見送り（{msg}）。"
    return "攻め候補はありません。すべて見送りです。"


@app.get("/api/home")
def home() -> dict:
    """ホーム画面（SPEC 7.1章）: モード、今日の結論、信号の件数、🟢の一覧、市場の状態、収集の状態。"""
    with _open() as (config, conn):
        rows = []
        for r in db.latest_scores(conn):
            d = _score_dict(r)
            d["is_stock_pair"] = conn.execute("SELECT is_stock_pair FROM pools WHERE id=?",
                                              (d["pool_id"],)).fetchone()[0]
            rows.append(d)
        counts = {"green": 0, "yellow": 0, "red": 0}
        for d in rows:
            counts[d["signal"]] = counts.get(d["signal"], 0) + 1
        slim = ["pool_id", "pair", "venue_id", "signal", "net_daily_pct", "net_daily_pct_lp", "best_r", "reason_ja",
                "is_stock_pair", "has_perp", "tvl_usd"]
        greens = [{k: d.get(k) for k in slim} for d in rows if d["signal"] == "green"]
        # 🟢がないときの参考: 判定できたプールを純日利の高い順に3件
        near = [{k: d.get(k) for k in slim} for d in rows
                if d["signal"] != "green" and d["net_daily_pct"] is not None][:3]
        now = _now()
        gas = next((d["details"].get("inputs", {}).get("gas_usd_per_tx") for d in rows if d["details"]), None)
        rewards = []
        health_rows = []
        for venue_id in config.venues:
            v = load_venue(venue_id, config.root)
            prices = _reward_token_prices(conn, config, v, days=2)
            sym = _symbol(conn, contract_address(v, "reward_token")) or "報酬トークン"
            rewards.append({"venue_id": venue_id, "symbol": sym, "price_usd": prices[-1][1] if prices else None,
                            "change_24h": views.series_change(prices, int(now.timestamp()), 24)})
            last_ok = conn.execute(
                "SELECT finished_at FROM collection_runs WHERE venue_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                (venue_id,)).fetchone()
            age = (now - datetime.fromisoformat(last_ok[0])).total_seconds() / 60 if last_ok and last_ok[0] else None
            gaps = [dict(g) for g in db.list_gaps(conn, venue_id, now - timedelta(days=7))]
            health_rows.append({"venue_id": venue_id, "last_ok_at": last_ok[0] if last_ok else None,
                                "stale": age is None or age > config.stale_after_minutes, "gaps_7d": gaps})
        return {
            "mode": config.mode,
            "summary": _summary_sentence(rows),
            "counts": counts, "judge_basis": "総資産あたりの純日利（%）",
            "scored_at": max((d["ts"] for d in rows), default=None),
            "greens": greens, "near": near,
            "market": {"us_open": vol.us_market_open(int(now.timestamp())), "gas_usd_per_tx": gas,
                       "reward_tokens": rewards},
            "collection": health_rows,
        }


@app.get("/api/pools/{pool_id}")
def pool(pool_id: str) -> dict:
    """プール詳細（SPEC 7.3章・7.6章・7.7章）。損益は1時間ごとのスコア（予測）から作る。"""
    with _open() as (config, conn):
        r = conn.execute(
            """SELECT s.*, p.token0_symbol, p.token1_symbol, p.address, p.is_stock_pair FROM scores s
               JOIN pools p ON p.id = s.pool_id WHERE s.pool_id=? ORDER BY s.ts DESC LIMIT 1""", (pool_id,)
        ).fetchone()
        if r is None:
            raise HTTPException(404, "このプールの判定はまだありません")
        d = _score_dict(r)
        snap = conn.execute("SELECT price, tick, ts FROM pool_snapshots WHERE pool_id=? ORDER BY ts DESC LIMIT 1",
                            (pool_id,)).fetchone()
        now = _now()
        s = config.scoring
        history = views.pool_history(conn, pool_id, now - timedelta(days=7))
        first = conn.execute("SELECT MIN(ts) FROM scores WHERE pool_id=?", (pool_id,)).fetchone()[0]
        all_hist = views.pool_history(conn, pool_id, datetime.fromisoformat(first)) if first else []
    b = views.row_breakdown(d)
    capital = s.total_capital_usd
    c_lp = capital * s.allocation_lp
    for h in history:
        h["us_open"] = vol.us_market_open(int(datetime.fromisoformat(h["ts"]).timestamp()))
    return {
        "score": d,
        "price": dict(snap) if snap else None,
        "capital": {"total": capital, "lp": c_lp, "margin": capital * s.allocation_hedge_margin,
                    "reserve": capital * s.allocation_reserve},
        "daily": {
            "breakdown": b, "labels": views.CATEGORY_JA,
            "net_daily_pct": d["net_daily_pct"], "net_daily_pct_lp": d["net_daily_pct_lp"],
            "judge_basis": "判定に使うのは総資産あたり日利",
            "apy_display": views.apy(b["income"], capital) if b else None,
            "apy_net": views.apy(b["net"], capital) if b else None,
            "apy_note": views.APY_NOTE,
            "realized_note": "今は予測だけです。実現損益と未実現損益は「練習」（M5）で表示します。",
        },
        "sell_now": _sell_now(d, b),
        "today": views.today_breakdown(history, now),
        "since_start": views.daily_average_since_start(all_hist),
        "hourly": views.hourly_bars(history, now),
        "assets": views.total_assets(capital, s.allocation_lp, s.allocation_hedge_margin, s.allocation_reserve,
                                     history, now),
        "history": history,
    }


def _sell_now(d: dict, b: dict | None) -> dict | None:
    """参考値「報酬をすぐ売る前提の純日利」（2026-09-29 オーナー指示）。判定には使わない。

    判定（持ち続ける前提）は報酬トークンの値下がりを7日の傾向で引く。こちらは受け取ってから売るまでの
    時間（config の scoring.reward_sell_hours）の分だけ引く。古いスコアには無いので None。
    """
    sn = (d.get("details") or {}).get("sell_now")
    if not sn:
        return None
    return {
        **sn,
        "hold_net_daily_pct": d["net_daily_pct"], "hold_haircut": b["haircut"] if b else None,
        "diff_pct": sn["net_daily_pct"] - d["net_daily_pct"] if d["net_daily_pct"] is not None else None,
        "note": (f"報酬を受け取ってから{sn['hours']:g}時間で売る前提の参考値です。判定には使いません。"
                 "判定は「報酬を持ち続ける前提」（7日の値下がりの傾向を引く）で出しています。"),
    }


@app.get("/api/reports")
def reports(days: int = 30) -> dict:
    """毎朝のレポートと今日の学びの履歴（M4。学ぶタブで表示）。Telegram の設定は「あるかないか」だけ返す。"""
    days = max(1, min(days, 365))
    since = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _open() as (config, conn):
        reps = [dict(r) for r in conn.execute(
            "SELECT day, ts, body_ja, sent_at FROM daily_reports WHERE ts>=? ORDER BY day DESC", (since,))]
        notes = [dict(r) for r in conn.execute(
            "SELECT ts, pool_id, title, body_ja, topic FROM learning_notes WHERE ts>=? ORDER BY ts DESC", (since,))]
        sent = conn.execute("SELECT MAX(notified_at) FROM alerts").fetchone()[0]
        n = config.notify
    return {
        "reports": reps, "learning": notes,
        "telegram": {"configured": settings_from_env(os.environ) is not None, "last_alert_sent_at": sent},
        "schedule_jst": f"{n.daily_report_hour_jst:02d}:{n.daily_report_minute:02d}",
    }


# --- 練習（ペーパートレード。M5a） ------------------------------------------------------------

class OpenRequest(BaseModel):
    pool_id: str


def _paper_tokens(config, pool_venue: str):
    return load_tokens(load_venue(pool_venue, config.root)["chain"]["id"], config.root)


def _paper_status(config, conn) -> dict:
    lim = config.limits
    open_rows = conn.execute("SELECT venue_id, capital FROM positions WHERE is_paper=1 AND status='open'").fetchall()
    st = risk_job.paper_state(conn)
    venue_cap = (float(lim["total_usd"]) * float(lim["per_venue_share"])
                 if "total_usd" in lim and "per_venue_share" in lim else None)
    return {
        "mode": config.mode, "enabled": config.mode == "paper", "stopped": st["stopped"],
        "stopped_reason": st["reason"], "stopped_since": st["since"],
        "capital": config.scoring.total_capital_usd,
        "limits": {k: lim.get(k) for k in ("position_usd", "total_usd", "per_venue_share", "trades_per_day")},
        "venue_cap_usd": venue_cap, "open_total_usd": sum(r["capital"] for r in open_rows),
        "how_to_enable": "config.yaml の mode を paper にして、アプリを起動し直してください。",
    }


@app.get("/api/paper")
def paper() -> dict:
    """練習タブ（SPEC 7.4章）: 状態、上限、建玉カードの一覧。"""
    now = _now()
    with _open() as (config, conn):
        rows = conn.execute("SELECT * FROM positions WHERE is_paper=1 ORDER BY status='open' DESC, opened_at DESC"
                            ).fetchall()
        cards = [paper_views.card(conn, p, now, config.risk) for p in rows]
        return {**_paper_status(config, conn),
                "open": [c for c in cards if c["status"] == "open"],
                "closed": [c for c in cards if c["status"] != "open"][:20],
                "events": paper_views.events(conn, None, limit=20),
                "risk": paper_views.risk_rules(config.risk),
                "watch": paper_views.watch(conn)}


@app.get("/api/paper/positions/{position_id}")
def paper_position(position_id: int) -> dict:
    now = _now()
    with _open() as (config, conn):
        p = conn.execute("SELECT * FROM positions WHERE id=? AND is_paper=1", (position_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "この建玉は見つかりません")
        d = paper_views.detail(conn, p, now, config.risk)
        d["sell_now"]["hours"] = config.scoring.reward_sell_hours
        return d


@app.post("/api/paper/positions")
def paper_open(req: OpenRequest) -> dict:
    """「このプールで $1,000 を試す」（オーナーがボタンを押したときだけ。自動では入らない）。"""
    with _open() as (config, conn):
        pool_row = conn.execute("SELECT venue_id FROM pools WHERE id=?", (req.pool_id,)).fetchone()
        if pool_row is None:
            raise HTTPException(404, "このプールは見つかりません")
        ex = PaperExecutor(conn, config, _paper_tokens(config, pool_row["venue_id"]), now=_now())
        try:
            ref = ex.open_position(req.pool_id, config.scoring.total_capital_usd)
        except PaperError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"position_id": ref.position_id}


@app.post("/api/paper/positions/{position_id}/close")
def paper_close(position_id: int) -> dict:
    with _open() as (config, conn):
        p = conn.execute("SELECT venue_id FROM positions WHERE id=? AND is_paper=1", (position_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "この建玉は見つかりません")
        ex = PaperExecutor(conn, config, _paper_tokens(config, p["venue_id"]), now=_now())
        try:
            res = ex.close_position(PositionRef(position_id))
        except PaperError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"position_id": res.position_id, "net_usd": res.net_usd}


class ConfirmRequest(BaseModel):
    confirm: bool = False


def _first_tokens(config):
    return _paper_tokens(config, config.venues[0]) if config.venues else load_tokens("robinhood", config.root)


@app.post("/api/paper/stop")
def paper_stop() -> dict:
    """停止（新しい建玉を作らない。持っている建玉の計算と見張りは続ける）。SPEC 12.5章。"""
    with _open() as (config, conn):
        return {"message": risk_job.owner_stop(conn, _now(), "画面のボタン"), **risk_job.paper_state(conn)}


@app.post("/api/paper/resume")
def paper_resume() -> dict:
    with _open() as (config, conn):
        return {"message": risk_job.owner_resume(conn, _now(), "画面のボタン"), **risk_job.paper_state(conn)}


@app.post("/api/paper/exit_all")
def paper_exit_all(req: ConfirmRequest) -> dict:
    """全部閉じる。画面で確認してから confirm=true で呼ぶ。"""
    if not req.confirm:
        raise HTTPException(400, "確認がないので、全部閉じるのをやめました。")
    with _open() as (config, conn):
        msg = risk_job.owner_exit_all(conn, config, _first_tokens(config), _now(), "画面のボタン")
        return {"message": msg, **risk_job.paper_state(conn)}


# --- 画面のファイル（frontend/dist） ---------------------------------------------------------

def _ui_dir() -> Path | None:
    for p in (os.environ.get("FARM_RADAR_UI_DIR"), REPO_ROOT / "frontend" / "dist"):
        if p and Path(p, "index.html").exists():
            return Path(p)
    return None


@app.get("/{path:path}", include_in_schema=False)
def ui(path: str):
    """画面のファイルを返す。知らないパスは index.html（画面の中で切り替える）。"""
    if path.startswith("api/"):
        raise HTTPException(404)
    root = _ui_dir()
    if root is None:
        raise HTTPException(404, "画面はまだ build されていません（README の M3 を見てください）")
    f = (root / path).resolve()
    if path and f.is_file() and root.resolve() in f.parents:
        return FileResponse(f)
    return FileResponse(root / "index.html")
