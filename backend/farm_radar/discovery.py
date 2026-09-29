"""候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1）。

読み取りだけ（DefiLlama と GeckoTerminal の公開API）。お金を動かすコードはない。
会場を自動で追加はしない。オーナーが画面で「調べる / 保留 / 見送り」を記録するだけで、
「調べる」にした会場も、アドレスと報酬の仕組みを出典つきで確かめて venues/*.yaml を作るまでは監視しない（絶対ルール3・4）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config, DiscoverySettings, load_venue
from .db import database as db
from .external.defillama import DefiLlama
from .external.geckoterminal import GeckoTerminal
from .external.http import ExternalError

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
DECISIONS = {"study": "調べる", "hold": "保留", "skip": "見送り"}
_WEEKDAY_NO = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_JA = "月火水木金土日"
STALE_RUNNING_MINUTES = 30          # 「集めている最中」のまま、これより古いものは止まったとみなす
MAX_NEW_DEX_POOLS = 5               # 新しい DEX のプールを読みに行く数（回数制限のため）

# ドルと同じ値段のトークン（値動きがないので、ヘッジはいらない）
STABLES = {"USDG", "USDC", "USDT", "USDE", "DAI", "USDT0", "USD₮0", "USDC.E", "USDBC", "FRAX", "PYUSD", "USDS",
           "SUSDE", "USD0", "GHO", "LUSD", "CRVUSD", "USDB", "RLUSD", "USDH", "USDHL", "FEUSD", "AUSD"}
# 包んだトークン → 先物の銘柄（WETH は ETH の先物でヘッジする、など）
ALIASES = {"WETH": "ETH", "WBTC": "BTC", "CBBTC": "BTC", "UBTC": "BTC", "UETH": "ETH", "WAVAX": "AVAX",
           "WHYPE": "HYPE", "WSOL": "SOL"}


def _now_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def split_symbol(symbol: str | None) -> list[str]:
    # "NVDA / USDG 0.05%" のような名前の、手数料の部分（数字と%）は記号ではないので除く
    return [t for t in re.split(r"[-/ ]+", (symbol or "").upper()) if t and not re.fullmatch(r"[\d.]+%?", t)]


def hedge_match(symbol: str | None, perps: dict[str, list[str]]) -> dict[str, Any]:
    """プールの記号（例: "WETH-USDG"）が、ヘッジ先の先物の銘柄と一致するか。記号の一致だけ（同じトークンかは未確認）。

    返す値: ok = ドル以外のトークンが全部、先物の銘柄と一致した / stable = 全部ドルと同じ値段のトークン /
    tokens = 一致した銘柄、venues = その銘柄を扱っているヘッジ先の名前
    """
    toks = split_symbol(symbol)
    risky = [t for t in toks if t not in STABLES]
    matched = [ALIASES.get(t, t) for t in risky if ALIASES.get(t, t) in perps]
    venues = sorted({v for t in matched for v in perps.get(t, [])})
    return {"ok": bool(risky) and len(matched) == len(risky), "stable": bool(toks) and not risky,
            "tokens": matched, "venues": venues}


def perp_symbols(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """ヘッジ先が扱っている先物の銘柄 → ヘッジ先の名前（hedge_fees 表から。15分ごとの収集で入る）。"""
    from . import hedges as hedge_mod
    names = {hid: getattr(cls, "name", hid) for hid, cls in hedge_mod.REGISTRY.items()}
    out: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT DISTINCT hedge_id, symbol FROM hedge_fees WHERE symbol IS NOT NULL"):
        sym = str(r["symbol"]).upper()
        if sym and not sym.startswith("#"):
            out[sym].add(names.get(r["hedge_id"], r["hedge_id"]))
    return {k: sorted(v) for k, v in out.items()}


def watched_listings(config: Config) -> tuple[set[str], dict[str, set[str]]]:
    """いま見ている会場の、外部の一覧サイトでの名前（venues/*.yaml の listings）。候補から除くため。"""
    slugs: set[str] = set()
    gt: dict[str, set[str]] = defaultdict(set)
    for vid in config.venues:
        try:
            v = load_venue(vid, config.root)
        except Exception:
            continue
        li = v.get("listings") or {}
        slugs |= {str(s) for s in li.get("defillama") or []}
        net = (v.get("chain") or {}).get("geckoterminal_network")
        if net:
            gt[net] |= {str(s) for s in li.get("geckoterminal") or []}
    return slugs, gt


def _pool_row(x: dict[str, Any], perps: dict[str, list[str]], coins: str | None) -> dict[str, Any]:
    rewards = [str(a).lower() for a in x.get("rewardTokens") or [] if a]
    return {
        "pool": x.get("pool"), "symbol": x.get("symbol"), "meta": x.get("poolMeta"),
        "tvl_usd": x.get("tvlUsd"), "apy_reward": x.get("apyReward"), "apy_base": x.get("apyBase"),
        "days": x.get("count"), "reward_tokens": [f"{coins}:{a}" for a in rewards] if coins else [],
        "outlier": bool(x.get("outlier")), "hedge": hedge_match(x.get("symbol"), perps),
    }


def weighted_median(rows: list[dict[str, Any]]) -> float | None:
    """預かり額で重みをつけた、ボーナスの利回りの真ん中の値。極端なプール（外れ値）に引っぱられにくい。"""
    rows = sorted((r for r in rows if r.get("apy_reward") is not None), key=lambda r: r["apy_reward"])
    total = sum(float(r["tvl_usd"] or 0) for r in rows)
    acc = 0.0
    for r in rows:
        acc += float(r["tvl_usd"] or 0)
        if acc >= total / 2:
            return float(r["apy_reward"])
    return None


def build_candidates(protocols: list[dict[str, Any]], pools: list[dict[str, Any]], s: DiscoverySettings,
                     watched: set[str], perps: dict[str, list[str]], now: datetime,
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """DefiLlama の会場とプールから、候補の会場と、新しくボーナスが出始めたプールを作る（SPEC 5.2.2章）。"""
    chains = {c.name: c for c in s.chains}
    prot = {p.get("slug"): p for p in protocols if p.get("slug")}
    dex = {slug for slug, p in prot.items() if p.get("category") == "Dexs"}
    by_venue: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    new_pools: list[dict[str, Any]] = []
    for x in pools:
        ch, proj = x.get("chain"), x.get("project")
        if ch not in chains or proj not in dex:
            continue
        tvl, rew = float(x.get("tvlUsd") or 0), float(x.get("apyReward") or 0)
        if rew <= 0 or tvl < s.pool_min_tvl_usd:
            continue
        row = _pool_row(x, perps, chains[ch].coins)
        by_venue[(proj, ch)].append(row)
        days = x.get("count")
        if proj not in watched and isinstance(days, (int, float)) and days <= s.new_pool_days:
            new_pools.append({**row, "project": proj, "venue_name": prot[proj].get("name") or proj, "chain": ch})

    out: list[dict[str, Any]] = []
    for slug in sorted(dex):
        p = prot[slug]
        if slug in watched:
            continue
        for ch in p.get("chains") or []:
            if ch not in chains:
                continue
            tvl = (p.get("chainTvls") or {}).get(ch)
            if not isinstance(tvl, (int, float)) or tvl < s.min_tvl_usd:
                continue
            listed = p.get("listedAt")
            age = (now.timestamp() - float(listed)) / 86400 if isinstance(listed, (int, float)) else None
            rp = sorted(by_venue.get((slug, ch), []), key=lambda r: -(r["apy_reward"] or 0))
            if not rp and not (age is not None and age <= s.new_days):
                continue
            # 週のボーナスの目安 = ボーナスの利回りの真ん中の値 × ボーナスの出ているプールの預かり額 ÷ 52週。
            # DefiLlama の利回りからの逆算で、チェーンから読んだ値ではない（外れ値のプールがあるので単純な合計は使わない）
            med = weighted_median(rp)
            reward_tvl = sum(float(r["tvl_usd"] or 0) for r in rp)
            weekly = (med or 0) / 100 / 52 * reward_tvl
            out.append({
                "key": f"llama:{slug}@{ch}", "source": "defillama", "slug": slug, "name": p.get("name") or slug,
                "chain": ch, "url": p.get("url"), "tvl_usd": float(tvl),
                "change_7d_pct": p.get("change_7d"), "multi_chain": len(p.get("chains") or []) > 1,
                "listed_at": (datetime.fromtimestamp(float(listed), UTC).date().isoformat()
                              if isinstance(listed, (int, float)) else None),
                "age_days": None if age is None else int(age),
                "audit_links": list(p.get("audit_links") or []),
                "reward_apr_median": med, "reward_pools_tvl_usd": reward_tvl if rp else None,
                "weekly_reward_usd": weekly if rp else None,
                "weekly_ratio_pct": weekly / float(tvl) * 100 if rp and tvl else None,
                "reward_pools_n": len(rp), "top_pools": rp[:5],
                "hedge_pools": list(dict.fromkeys(r["symbol"] for r in rp if r["hedge"]["ok"]))[:8],
                "reward_tokens": sorted({t for r in rp for t in r["reward_tokens"]}),
            })
    out.sort(key=lambda v: (v["weekly_ratio_pct"] is None,
                            -(v["weekly_ratio_pct"] or 0) if v["weekly_ratio_pct"] is not None else -v["tvl_usd"]))
    new_pools.sort(key=lambda r: -(r["apy_reward"] or 0))
    return out, new_pools[:10]


def _attach_tokens(rows: list[dict[str, Any]], info: dict[str, dict[str, Any]]) -> None:
    for v in rows:
        toks = [{"coin": k, **info.get(k, {})} for k in v.get("reward_tokens") or []]
        v["reward_token_info"] = [{"symbol": t.get("symbol"), "price": t.get("price"),
                                   "change_7d_pct": t.get("change_7d_pct")} for t in toks]
        ch = [t["change_7d_pct"] for t in toks if isinstance(t.get("change_7d_pct"), (int, float))]
        v["reward_change_7d_pct"] = min(ch) if ch else None


def _latest_ok(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM discovery_runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1"
                        ).fetchone()


def _upsert(conn: sqlite3.Connection, key: str, source: str, name: str, chain: str, data: dict[str, Any],
            run_id: int, now_s: str) -> bool:
    """あれば更新、なければ入れる。新しく入れたら True。"""
    cur = conn.execute("SELECT first_run FROM discovery_venues WHERE key=?", (key,)).fetchone()
    js = json.dumps(data, ensure_ascii=False)
    if cur is None:
        conn.execute("""INSERT INTO discovery_venues(key, source, name, chain, first_seen, last_seen, first_run, run_id,
                        data_json) VALUES (?,?,?,?,?,?,?,?,?)""", (key, source, name, chain, now_s, now_s, run_id, run_id, js))
        return True
    conn.execute("UPDATE discovery_venues SET name=?, chain=?, last_seen=?, run_id=?, data_json=? WHERE key=?",
                 (name, chain, now_s, run_id, js, key))
    return False


def collect(conn: sqlite3.Connection, config: Config, *, llama: DefiLlama | None = None,
            gts: dict[str, GeckoTerminal] | None = None, now: datetime | None = None,
            trigger: str = "weekly") -> dict[str, Any]:
    """1回分の一覧を作って保存する。読み取りに失敗しても、読めた分は残す（status = partial）。"""
    now = now or datetime.now(UTC)
    s = config.discovery
    now_s = _now_iso(now)
    llama = llama or DefiLlama()
    prev_ok = _latest_ok(conn)
    run_id = conn.execute("INSERT INTO discovery_runs(started_at, trigger, status) VALUES (?,?, 'running')",
                          (now_s, trigger)).lastrowid
    conn.commit()
    errors: list[str] = []
    watched, watched_gt = watched_listings(config)
    perps = perp_symbols(conn)

    try:
        protocols = llama.protocols()
    except ExternalError as exc:
        errors.append(f"DefiLlama の会場一覧: {exc}")
        protocols = []
    try:
        pools = llama.yield_pools()
    except ExternalError as exc:
        errors.append(f"DefiLlama の利回り: {exc}")
        pools = []
    cands, new_pools = build_candidates(protocols, pools, s, watched, perps, now)
    coins = {t for v in cands + new_pools for t in v.get("reward_tokens") or []}
    info: dict[str, dict[str, Any]] = {}
    if coins:
        try:
            info = llama.token_info(coins)
        except ExternalError as exc:
            errors.append(f"DefiLlama のトークン価格: {exc}")
    _attach_tokens(cands, info)
    _attach_tokens(new_pools, info)

    n_new = 0
    for v in cands:
        is_new = _upsert(conn, v["key"], "defillama", v["name"], v["chain"], v, run_id, now_s)
        v["new"] = bool(is_new and prev_ok is not None)
        if v["new"]:
            conn.execute("UPDATE discovery_venues SET data_json=? WHERE key=?",
                         (json.dumps(v, ensure_ascii=False), v["key"]))
            n_new += 1
    for r in new_pools:
        conn.execute("INSERT OR REPLACE INTO discovery_pools(run_id, pool, data_json) VALUES (?,?,?)",
                     (run_id, str(r.get("pool") or r.get("symbol")), json.dumps(r, ensure_ascii=False)))

    # GeckoTerminal: 見るチェーンの DEX の一覧から、前回までになかった DEX を見つける
    gt_new: list[str] = []
    for c in s.chains:
        if not c.geckoterminal:
            continue
        gt = (gts or {}).get(c.geckoterminal) or GeckoTerminal(c.geckoterminal)
        try:
            dexes = gt.dexes()
        except ExternalError as exc:
            errors.append(f"GeckoTerminal の DEX 一覧（{c.geckoterminal}）: {exc}")
            continue
        seen_before = conn.execute("SELECT COUNT(*) FROM discovery_venues WHERE source='geckoterminal' AND chain=?",
                                   (c.name,)).fetchone()[0] > 0
        fetched = 0
        for dex_id, name in dexes:
            key = f"gt:{c.geckoterminal}/{dex_id}"
            exists = conn.execute("SELECT data_json FROM discovery_venues WHERE key=?", (key,)).fetchone()
            data: dict[str, Any] = json.loads(exists["data_json"]) if exists else {}
            is_new = exists is None and seen_before
            data.update({"key": key, "source": "geckoterminal", "name": name, "chain": c.name,
                         "network": c.geckoterminal, "dex_id": dex_id,
                         "watched": dex_id in watched_gt.get(c.geckoterminal, set()), "new": is_new,
                         "url": f"https://www.geckoterminal.com/{c.geckoterminal}/{dex_id}/pools"})
            if is_new and not data["watched"] and fetched < MAX_NEW_DEX_POOLS:
                fetched += 1
                try:
                    dp = gt.dex_pools(dex_id)
                    data["pools"] = [{**p, "hedge": hedge_match(p.get("name"), perps)} for p in dp[:5]]
                    data["tvl_top_usd"] = sum(float(p.get("tvl_usd") or 0) for p in dp)
                except ExternalError as exc:
                    errors.append(f"GeckoTerminal の {name} のプール: {exc}")
            _upsert(conn, key, "geckoterminal", name, c.name, data, run_id, now_s)
            if is_new and not data["watched"]:
                gt_new.append(name)
                n_new += 1

    got_any = bool(cands) or bool(protocols) or bool(pools)
    status = "ok" if not errors else ("partial" if got_any else "error")
    conn.execute("UPDATE discovery_runs SET finished_at=?, status=?, error=?, n_venues=?, n_new=? WHERE id=?",
                 (_now_iso(datetime.now(UTC)), status, "\n".join(errors)[:2000] or None, len(cands), n_new, run_id))
    conn.commit()

    # 新しい候補が出た週は、通知の箱に1件入れる（見送りにした会場は数えない）
    if n_new:
        skipped = {r["key"] for r in conn.execute("SELECT key FROM discovery_decisions WHERE status='skip'")}
        names = [f"{v['name']}（{v['chain']}）" for v in cands if v.get("new") and v["key"] not in skipped] + \
                [f"{n}（新しいDEX）" for n in gt_new]
        if names:
            db.insert_alert(conn, ts=now, venue_id="discovery", pool_id=None, kind="discovery", level="info",
                            message_ja=f"🔎 新しい会場の候補 {len(names)}件: " + "、".join(names[:6])
                            + ("ほか" if len(names) > 6 else "") + "。「会場」タブで見られます。",
                            data={"run_id": run_id, "n": len(names)}, dedupe_key=f"discovery:{run_id}")
            conn.commit()
    log.info("discovery finished", extra={"data": {"run": run_id, "status": status, "venues": len(cands),
                                                   "new": n_new, "errors": len(errors)}})
    return {"run_id": run_id, "status": status, "venues": len(cands), "new": n_new, "errors": errors}


# --- 動かす時期 -----------------------------------------------------------------------------------

def next_run(now: datetime, s: DiscoverySettings) -> datetime:
    local = now.astimezone(JST)
    target = local.replace(hour=s.hour_jst, minute=s.minute_jst, second=0, microsecond=0)
    days = (_WEEKDAY_NO[s.weekday] - local.weekday()) % 7
    target += timedelta(days=days)
    if target <= local:
        target += timedelta(days=7)
    return target.astimezone(UTC)


def due_at_startup(conn: sqlite3.Connection, now: datetime) -> bool:
    """止めていたあとの起動で、前回（うまくいった回）から7日以上たっていれば True。"""
    last = _latest_ok(conn)
    return last is None or datetime.fromisoformat(last["started_at"]) <= now - timedelta(days=7)


def refresh_block_reason(conn: sqlite3.Connection, s: DiscoverySettings, now: datetime) -> str | None:
    """「今すぐ更新」を受け付けられないときの理由。受け付けられるなら None。"""
    last = conn.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
    if last is None:
        return None
    started = datetime.fromisoformat(last["started_at"])
    if last["status"] == "running" and started > now - timedelta(minutes=STALE_RUNNING_MINUTES):
        return "いま集めている最中です。数分後に画面を読み直してください。"
    wait = started + timedelta(minutes=s.refresh_min_minutes) - now
    if wait > timedelta(0):
        return f"前回の更新から{s.refresh_min_minutes}分たっていません（あと{int(wait.total_seconds() // 60) + 1}分）。"
    return None


_run_lock = threading.Lock()


def run_now(config: Config, trigger: str, **kw: Any) -> dict[str, Any] | None:
    """自分の接続で1回動かす（スケジューラーと「今すぐ更新」のボタンから）。同じプロセスで重ならないようにする。"""
    if not _run_lock.acquire(blocking=False):
        return None
    conn = db.connect(config.database_path)
    try:
        return collect(conn, config, trigger=trigger, **kw)
    finally:
        conn.close()
        _run_lock.release()


# --- 画面 ------------------------------------------------------------------------------------------

def summary(conn: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    s = config.discovery
    last = conn.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
    ok = _latest_ok(conn)
    decisions = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM discovery_decisions")}
    nxt = next_run(now, s).astimezone(JST)
    out: dict[str, Any] = {
        "schedule_ja": f"毎週{_WEEKDAY_JA[_WEEKDAY_NO[s.weekday]]}曜 {s.hour_jst:02d}:{s.minute_jst:02d}（日本時間）",
        "next_run": nxt.isoformat(timespec="minutes"),
        "last_run": dict(last) if last else None,
        "last_ok_at": ok["started_at"] if ok else None,
        "refresh_block": refresh_block_reason(conn, s, now),
        "chains": [c.name for c in s.chains],
        "criteria_ja": (f"預かり額 ${s.min_tvl_usd:,.0f} 以上で、ボーナスの出ているプールがあるか、掲載から{s.new_days}日以内の DEX。"
                        f"プールは預かり額 ${s.pool_min_tvl_usd:,.0f} 以上。"),
        "venues": [], "new_dexes": [], "gt_dex_count": 0, "new_pools": [], "decided_elsewhere": [],
    }
    if ok is None:
        return out
    rows = conn.execute("SELECT * FROM discovery_venues WHERE run_id=? ORDER BY rowid", (ok["id"],)).fetchall()
    order = {}
    for r in rows:
        d = json.loads(r["data_json"])
        dec = decisions.get(r["key"])
        d.update({"first_seen": r["first_seen"], "decision": dec["status"] if dec else None,
                  "decided_at": dec["decided_at"] if dec else None})
        if r["source"] == "defillama":
            out["venues"].append(d)
            order[r["key"]] = d
        else:
            out["gt_dex_count"] += 1
            if d.get("new") and not d.get("watched"):
                out["new_dexes"].append(d)
    out["venues"].sort(key=lambda v: (v.get("weekly_ratio_pct") is None,
                                      -(v.get("weekly_ratio_pct") or 0) if v.get("weekly_ratio_pct") is not None
                                      else -(v.get("tvl_usd") or 0)))
    # 判断した会場が、今週の一覧から外れていても見えるようにする（「調べる」にしたものなど）
    for key, dec in decisions.items():
        if key in order:
            continue
        r = conn.execute("SELECT * FROM discovery_venues WHERE key=?", (key,)).fetchone()
        if r is None:
            continue
        d = json.loads(r["data_json"])
        d.update({"first_seen": r["first_seen"], "last_seen": r["last_seen"], "decision": dec["status"],
                  "decided_at": dec["decided_at"], "not_in_latest": True})
        out["decided_elsewhere"].append(d)
    out["new_pools"] = [json.loads(r["data_json"]) for r in
                        conn.execute("SELECT data_json FROM discovery_pools WHERE run_id=?", (ok["id"],))]
    out["new_pools"].sort(key=lambda r: -(r.get("apy_reward") or 0))
    return out


def decide(conn: sqlite3.Connection, key: str, status: str | None, now: datetime) -> None:
    """オーナーの判断を記録する（status=None で取り消し）。"""
    if conn.execute("SELECT 1 FROM discovery_venues WHERE key=?", (key,)).fetchone() is None:
        raise KeyError(key)
    if status is None:
        conn.execute("DELETE FROM discovery_decisions WHERE key=?", (key,))
    elif status in DECISIONS:
        conn.execute("INSERT OR REPLACE INTO discovery_decisions(key, status, decided_at) VALUES (?,?,?)",
                     (key, status, _now_iso(now)))
    else:
        raise ValueError(status)
    conn.commit()
