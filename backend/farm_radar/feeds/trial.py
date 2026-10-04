"""N5「試す」のための記録を集める（N5a。docs/n5-plan-2026-10-03.md の 2章。2026-10-03 オーナー承認: 判断 1A 2A 3A 4A 5A）。

どれも認証のいらない公開の読み取り口を読むだけ。お金を動かすコードはない。チェーンの読み取り口（RPC）は使わない。
- Merkl の配った額（預け方ごと）: 約2時間ごと。登録したチェーンの、幅に配るキャンペーンだけ（判断②A。キーなし）
  期間ごとの差は Merkl の API キーがないと読めないので、合計を保存して前の回との差を取る（PROGRESS「N5 の計画のための調べ」）
- DefiLlama の利回りと預かり額の毎日の記録（数か月分）: 1日1回。登録したチェーンのプールだけ
- Lighter の資金調達率の過去（1時間ごと）: 1日1回。登録したチェーンの機会に出てくるコインの銘柄と、取引の多い銘柄だけ
- Lighter の値段の過去（1時間の足）: 1日1回。資金調達率と同じ銘柄（保険の預け金の見直し。2026-10-04）
- Aero の公式の住所（公開のコード置き場の deployment-addresses）: 1時間に1回。新しいファイルが出たら印を付ける（判断④A）
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..external.http import ExternalError

MERKL = "https://api.merkl.xyz/v4"
LLAMA_CHART = "https://yields.llama.fi/chart"
LIGHTER = "https://mainnet.zklighter.elliot.ai/api/v1"
# Lighter の Robinhood Chain 版（別の取引所。預け金は USDG。口座・番号は本体と別。2026-10-04 オーナー決定 ②A。
# https://apidocs.lighter.xyz/docs/lighter-rh ・ https://docs.robinhood.com/chain/lighter-domains 2026-10-04 確認）
LIGHTER_RH = "https://api.rh.lighter.xyz/api/v1"


@dataclass(frozen=True)
class LighterBook:
    """どちらの Lighter を読むか（読み取り口と、保存する表）。市場の番号は本体と Robinhood Chain 版で別。"""
    base: str
    markets: str          # 銘柄の表
    funding: str          # 資金調達率の過去の表
    prices: str           # 値段の過去の表
    rh: bool = False


MAIN_BOOK = LighterBook(LIGHTER, "lighter_markets", "lighter_funding_history", "lighter_price_history")
RH_BOOK = LighterBook(LIGHTER_RH, "lighter_rh_markets", "lighter_rh_funding_history", "lighter_rh_price_history", rh=True)
# Aero の公開のコード置き場（README: "Deployment addresses are in `deployment-addresses/`"。2026-10-03 確認）。
# ここに出た住所も、使う前にチェーンの公開の記録（Blockscout・Sourcify）で確かめる（絶対ルール3）
AERO_REPO = "dromos-labs/metadex-public"
AERO_DIR = "deployment-addresses"
# 一覧が読めないとき（回数制限など）に、ファイルがあるかを1つずつ確かめる名前（arc.json だけ 2026-10-03 にあった）
AERO_GUESS = ("arc", "base", "ethereum", "mainnet", "optimism", "op", "ink", "robinhood", "arbitrum")

REWARD_ROWS = 100            # キャンペーンごとに読む「受け取った人×預け方」の行（配った額の多い順の先頭）
MAX_CAMPAIGNS = 150          # 1回に読むキャンペーンの上限
LLAMA_TOP_TVL = 60           # 預かり額の多いプール（貸し出しなど、比べる相手になるもの）
LLAMA_TOP_REWARD = 90        # ボーナスのあるプール
LLAMA_MIN_TVL = 50_000.0
LLAMA_KEEP_DAYS = 400
LIGHTER_DAYS = 90            # 資金調達率の過去を読む日数（最初の回）
LIGHTER_PAGE = 750           # 1回の応答の点の数の上限（2026-10-03 に確かめた: 90日分を頼んでも最後の750点だけ返る）
LIGHTER_TOP_VOLUME = 10
LIGHTER_CANDLE_PAGE = 500    # 値段の足（/candles）は1回に最大500点（2026-10-04 に確かめた）


@dataclass
class TrialContext:
    """試すための記録に使う設定（scheduler が作る）。"""
    chain_ids: tuple[int, ...] = ()                       # 登録したチェーンの番号（8453・4663）
    llama_chains: tuple[str, ...] = ()                    # DefiLlama のチェーン名（Base・Robinhood Chain）
    perp_alias: dict[str, str] = field(default_factory=dict)
    shadow: Callable[[sqlite3.Connection, datetime], dict[str, Any]] | None = None   # 影の記録（feeds/shadow.py）
    # up. のコインの保険に使う Lighter の市場（venues/tokens-robinhood.yaml の perps.map。N5b のさかのぼりで、
    # 今の版の見込みの資金調達料と比べるため。株の銘柄は Merkl の一覧や取引の多い10銘柄に入らないことがある）
    lighter_markets: tuple[tuple[int, str], ...] = ()


def _json(body: str) -> Any:
    return json.loads(body)


# --- Merkl の配った額 ----------------------------------------------------------------------------------

def reward_campaigns(conn: sqlite3.Connection, chain_ids: tuple[int, ...], now: datetime) -> list[sqlite3.Row]:
    """読むキャンペーン: 登録したチェーンの、幅に配る（手数料・コイン0・コイン1の重みがある）もので、
    始まっていて、終わっていないか終わって1日以内（最後の配りを読むため）。"""
    if not chain_ids:
        return []
    now_s = int(now.timestamp())
    marks = ",".join("?" for _ in chain_ids)
    return conn.execute(
        f"""SELECT campaign_id, distribution_chain_id, chain_id, opportunity_id, reward_address, reward_decimals
            FROM merkl_campaigns WHERE chain_id IN ({marks}) AND settings_json LIKE '%weightFees%'
            AND campaign_id LIKE '0x%' AND (start_ts IS NULL OR start_ts <= ?)
            AND (end_ts IS NULL OR end_ts > ?) ORDER BY daily_rewards DESC LIMIT ?""",
        (*chain_ids, now_s, now_s - 86400, MAX_CAMPAIGNS)).fetchall()


def read_merkl_rewards(conn: sqlite3.Connection, text: Callable[..., str], ctx: TrialContext,
                       now: datetime) -> tuple[dict[str, Any], int]:
    """キャンペーンごとに、受け取った人×預け方の配った合計と、キャンペーン全体で配った合計を読む。

    chainId は配る側のチェーン（distributionChainId）。預ける側の番号だと 404 になる（2026-10-03 に確かめた）。"""
    out: dict[str, Any] = {}
    calls = 0
    for c in reward_campaigns(conn, ctx.chain_ids, now):
        dist = c["distribution_chain_id"] or c["chain_id"]
        cid = c["campaign_id"]
        try:
            rows = _json(text(f"{MERKL}/rewards/", {"chainId": str(dist), "campaignId": cid,
                                                     "items": str(REWARD_ROWS), "page": "0"}))
            calls += 1
            total = _json(text(f"{MERKL}/rewards/total", {"chainId": str(dist), "campaignId": cid}))
            calls += 1
        except ExternalError as exc:
            if exc.status == 429:
                raise                       # 回数制限: この回はあきらめる（「不明」）
            out[cid] = {"error": str(exc)[:200], "dist_chain": dist}
            continue
        out[cid] = {"dist_chain": dist, "chain_id": c["chain_id"], "opportunity_id": c["opportunity_id"],
                    "rows": [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [],
                    "total": (total or {}).get("amount") if isinstance(total, dict) else None}
    return out, calls


def write_merkl_rewards(conn: sqlite3.Connection, ts: str, data: dict[str, Any]) -> int:
    """前の回と変わった行だけを書く（同じ額を毎回書かない。差は「前の行との差」で取れる）。書いた行の数を返す。"""
    n = 0
    for cid, d in data.items():
        if not isinstance(d, dict) or d.get("error"):
            continue
        for r in d.get("rows") or []:
            recipient, reason = str(r.get("recipient") or "").lower(), str(r.get("reason") or "")
            if not recipient:
                continue
            amount, claimed, pending = (None if r.get(k) is None else str(r[k]) for k in ("amount", "claimed", "pending"))
            prev = conn.execute("SELECT amount_raw, pending_raw FROM merkl_reward_latest WHERE campaign_id=? AND "
                                "recipient=? AND reason=?", (cid, recipient, reason)).fetchone()
            if prev is not None and prev[0] == amount and prev[1] == pending:
                continue
            conn.execute("INSERT OR REPLACE INTO merkl_reward_snaps(ts, campaign_id, recipient, reason, amount_raw, "
                         "claimed_raw, pending_raw, token) VALUES (?,?,?,?,?,?,?,?)",
                         (ts, cid, recipient, reason, amount, claimed, pending,
                          str(r.get("rewardTokenAddress") or "").lower() or None))
            conn.execute("INSERT OR REPLACE INTO merkl_reward_latest(campaign_id, recipient, reason, amount_raw, "
                         "pending_raw, ts) VALUES (?,?,?,?,?,?)", (cid, recipient, reason, amount, pending, ts))
            n += 1
        total = d.get("total")
        if total is not None:
            last = conn.execute("SELECT amount_raw FROM merkl_reward_totals WHERE campaign_id=? ORDER BY ts DESC LIMIT 1",
                                (cid,)).fetchone()
            if last is None or last[0] != str(total):
                conn.execute("INSERT OR REPLACE INTO merkl_reward_totals(ts, campaign_id, dist_chain_id, amount_raw) "
                             "VALUES (?,?,?,?)", (ts, cid, d.get("dist_chain"), str(total)))
    return n


# --- DefiLlama の毎日の記録 -----------------------------------------------------------------------------

def llama_pools(conn: sqlite3.Connection, llama_chains: tuple[str, ...]) -> list[str]:
    """読むプール: 登録したチェーンで、預かり額の多いもの（比べる相手の貸し出しなど）と、ボーナスのあるもの。"""
    if not llama_chains:
        return []
    rows = []
    marks = ",".join("?" for _ in llama_chains)
    for r in conn.execute(f"SELECT key, info_json FROM feed_items WHERE source='llama_yields' AND chain IN ({marks})",
                          llama_chains):
        info = json.loads(r[1] or "{}")
        tvl = info.get("tvl") or 0
        if tvl >= LLAMA_MIN_TVL:
            rows.append((r[0], tvl, (info.get("apy_reward") or 0) > 0))
    by_tvl = [k for k, _, _ in sorted(rows, key=lambda x: -x[1])]
    reward = [k for k, _, rw in sorted(rows, key=lambda x: -x[1]) if rw]
    return list(dict.fromkeys(by_tvl[:LLAMA_TOP_TVL] + reward[:LLAMA_TOP_REWARD]))


def read_llama_history(conn: sqlite3.Connection, text: Callable[..., str], ctx: TrialContext,
                       now: datetime) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    calls = 0
    for pool in llama_pools(conn, ctx.llama_chains):
        try:
            body = _json(text(f"{LLAMA_CHART}/{pool}"))
        except ExternalError as exc:
            if exc.status == 429:
                raise
            out[pool] = {"error": str(exc)[:200]}
            continue
        finally:
            calls += 1
        pts = (body or {}).get("data") if isinstance(body, dict) else None
        out[pool] = {"points": [p for p in pts or [] if isinstance(p, dict) and p.get("timestamp")]}
    return out, calls


def write_llama_history(conn: sqlite3.Connection, now: datetime, data: dict[str, Any]) -> int:
    n = 0
    for pool, d in data.items():
        for p in (d or {}).get("points") or []:
            day = str(p["timestamp"])[:10]
            conn.execute("INSERT OR REPLACE INTO llama_yield_history(pool, day, tvl_usd, apy, apy_base, apy_reward) "
                         "VALUES (?,?,?,?,?,?)", (pool, day, p.get("tvlUsd"), p.get("apy"), p.get("apyBase"),
                                                  p.get("apyReward")))
            n += 1
    cutoff = (now - timedelta(days=LLAMA_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM llama_yield_history WHERE day < ?", (cutoff,))
    return n


# --- Lighter の資金調達率の過去 ---------------------------------------------------------------------------

def lighter_targets(conn: sqlite3.Connection, ctx: TrialContext) -> list[tuple[int, str]]:
    """読む銘柄: 登録したチェーンの Merkl の機会に出てくるコイン（WETH → ETH などの読み替えつき）と、取引の多い銘柄と、
    up. のコインの保険に使う市場（N5b）。"""
    alias = {k.upper(): v.upper() for k, v in ctx.perp_alias.items()}
    syms: set[str] = set()
    for r in conn.execute("SELECT info_json FROM feed_items WHERE source='merkl_opportunities'"):
        info = json.loads(r[0] or "{}")
        if info.get("chain_id") in ctx.chain_ids:
            for s in info.get("tokens") or []:
                if isinstance(s, str) and s:
                    syms.add(alias.get(s.upper(), s.upper()))
    markets = conn.execute("SELECT market_id, symbol, daily_quote_volume FROM lighter_markets WHERE status='active'"
                           ).fetchall()
    top = sorted(markets, key=lambda m: -(m[2] or 0))[:LIGHTER_TOP_VOLUME]
    want = {int(m[0]): str(m[1]) for m in markets if str(m[1]).upper() in syms}
    want.update({int(m[0]): str(m[1]) for m in top})
    active = {int(m[0]) for m in markets}
    want.update({mid: sym for mid, sym in ctx.lighter_markets if mid in active})
    return sorted(want.items())


def rh_targets(conn: sqlite3.Connection, ctx: TrialContext) -> list[tuple[int, str]]:
    """Robinhood Chain 版で読む銘柄: up. のコインの保険に使う市場（perps.map）のうち、Robinhood Chain 版にもあるもの
    （2026-10-04 に確かめたときは29市場のうち18。番号は Robinhood Chain 版のもの）。"""
    want = {sym.upper() for _, sym in ctx.lighter_markets}
    try:
        rows = conn.execute("SELECT market_id, symbol FROM lighter_rh_markets WHERE status='active'").fetchall()
    except sqlite3.OperationalError:
        return []
    return sorted((int(r[0]), str(r[1])) for r in rows if str(r[1]).upper() in want)


def _targets(conn: sqlite3.Connection, ctx: TrialContext, book: LighterBook) -> list[tuple[int, str]]:
    return rh_targets(conn, ctx) if book.rh else lighter_targets(conn, ctx)


def read_lighter_history(conn: sqlite3.Connection, text: Callable[..., str], ctx: TrialContext,
                         now: datetime, book: LighterBook = MAIN_BOOK) -> tuple[dict[str, Any], int]:
    """銘柄ごとに、前に保存した最後の時刻のあとから今まで（最初は90日分）を、750点ずつ新しい方からさかのぼって読む。"""
    out: dict[str, Any] = {}
    calls = 0
    end_all = int(now.timestamp())
    for mid, sym in _targets(conn, ctx, book):
        last = conn.execute(f"SELECT MAX(ts) FROM {book.funding} WHERE market_id=?", (mid,)).fetchone()[0]
        start = int(last) + 1 if last else end_all - LIGHTER_DAYS * 86400
        pts: dict[int, dict[str, Any]] = {}
        end = end_all
        try:
            while end > start:
                body = _json(text(f"{book.base}/fundings", {"market_id": str(mid), "resolution": "1h",
                                                          "start_timestamp": str(start), "end_timestamp": str(end),
                                                          "count_back": str(LIGHTER_PAGE)}))
                calls += 1
                rows = [f for f in (body or {}).get("fundings") or [] if isinstance(f, dict) and f.get("timestamp")]
                for f in rows:
                    pts[int(f["timestamp"])] = f
                if not rows:
                    break
                first = min(int(f["timestamp"]) for f in rows)
                if first <= start or first >= end or calls > 400:
                    break
                end = first - 1             # 1回で返る点に上限がある（約750点）ので、古い方へさかのぼる
        except ExternalError as exc:
            if exc.status == 429:
                raise
            out[str(mid)] = {"symbol": sym, "error": str(exc)[:200]}
            continue
        out[str(mid)] = {"symbol": sym, "points": [pts[k] for k in sorted(pts) if k >= start]}
    return out, calls


def write_lighter_history(conn: sqlite3.Connection, data: dict[str, Any], book: LighterBook = MAIN_BOOK) -> int:
    """値は応答のまま残す（rate・value・direction の単位と向きは、N5b で公式の資料と照合してから使う）。"""
    n = 0
    for mid, d in data.items():
        for f in (d or {}).get("points") or []:
            conn.execute(f"INSERT OR REPLACE INTO {book.funding}(market_id, ts, symbol, rate, value, direction) "
                         "VALUES (?,?,?,?,?,?)", (int(mid), int(f["timestamp"]), d.get("symbol"),
                                                  None if f.get("rate") is None else float(f["rate"]),
                                                  None if f.get("value") is None else float(f["value"]),
                                                  f.get("direction")))
            n += 1
    return n


def read_lighter_prices(conn: sqlite3.Connection, text: Callable[..., str], ctx: TrialContext,
                        now: datetime, book: LighterBook = MAIN_BOOK) -> tuple[dict[str, Any], int]:
    """値段の過去（1時間の足）。読む銘柄は資金調達率と同じ。前に保存した最後の足から今まで（最初は90日分）を、
    500点ずつ新しい方からさかのぼって読む（/api/v1/candles。認証なし。t はミリ秒、start/end は秒）。"""
    out: dict[str, Any] = {}
    calls = 0
    end_all = int(now.timestamp())
    for mid, sym in _targets(conn, ctx, book):
        last = conn.execute(f"SELECT MAX(ts) FROM {book.prices} WHERE market_id=?", (mid,)).fetchone()[0]
        start = int(last) if last else end_all - LIGHTER_DAYS * 86400     # 最後の足は読み直す（まだ途中だったかもしれない）
        pts: dict[int, dict[str, Any]] = {}
        end = end_all
        try:
            # 幅が1時間より短いと「end_timestamp must be greater than start_timestamp」（400）が返る（2026-10-04 確認。
            # 90日より新しい市場で、いちばん古い足まで読んだとき）。そこで止める
            while end - start >= 3600:
                try:
                    body = _json(text(f"{book.base}/candles", {"market_id": str(mid), "resolution": "1h",
                                                             "start_timestamp": str(start), "end_timestamp": str(end),
                                                             "count_back": str(LIGHTER_CANDLE_PAGE)}))
                except ExternalError as exc:
                    if exc.status == 400 and pts:
                        break                   # さかのぼりの終わり。読めた分は残す
                    raise
                calls += 1
                rows = [c for c in (body or {}).get("c") or [] if isinstance(c, dict) and c.get("t")]
                for c in rows:
                    pts[int(c["t"]) // 1000] = c
                if not rows:
                    break
                first = min(int(c["t"]) // 1000 for c in rows)
                if first <= start or first >= end or calls > 600:
                    break
                end = first - 1
        except ExternalError as exc:
            if exc.status == 429:
                raise
            out[str(mid)] = {"symbol": sym, "error": str(exc)[:200]}
            continue
        out[str(mid)] = {"symbol": sym, "points": [{"ts": k, **{x: pts[k].get(x) for x in "ohlc"}}
                                                   for k in sorted(pts) if k >= start]}
    return out, calls


def write_lighter_prices(conn: sqlite3.Connection, data: dict[str, Any], book: LighterBook = MAIN_BOOK) -> int:
    n = 0
    for mid, d in data.items():
        for c in (d or {}).get("points") or []:
            vals = [None if c.get(x) is None else float(c[x]) for x in "ohlc"]
            conn.execute(f"INSERT OR REPLACE INTO {book.prices}(market_id, ts, symbol, open, high, low, close) "
                         "VALUES (?,?,?,?,?,?,?)", (int(mid), int(c["ts"]), d.get("symbol"), *vals))
            n += 1
    return n


# --- Aero の公式の住所 ---------------------------------------------------------------------------------

def read_aero_addresses(text: Callable[..., str]) -> tuple[dict[str, Any], int]:
    """公開のコード置き場の deployment-addresses の中のファイル（名前と中身の印）。

    まず一覧（GitHub の公開の読み取り口。鍵なしで1時間60回まで）を読む。読めなければ、決まった名前を1つずつ確かめる。"""
    calls = 0
    files: dict[str, Any] = {}
    try:
        listing = _json(text(f"https://api.github.com/repos/{AERO_REPO}/contents/{AERO_DIR}"))
        calls += 1
        if not isinstance(listing, list):
            raise ValueError("一覧の形が違います")
        for f in listing:
            if isinstance(f, dict) and f.get("type") == "file" and f.get("name"):
                files[str(f["name"])] = {"sha": f.get("sha"), "size": f.get("size"), "url": f.get("html_url"),
                                         "via": "list"}
        return files, calls
    except (ExternalError, ValueError):
        calls += 1                          # 読めなかった（回数制限など）。名前を1つずつ確かめる
    for name in AERO_GUESS:
        url = f"https://raw.githubusercontent.com/{AERO_REPO}/main/{AERO_DIR}/{name}.json"
        try:
            body = text(url)
        except ExternalError as exc:
            calls += 1
            if exc.status == 404:
                continue
            raise
        calls += 1
        files[f"{name}.json"] = {"sha": hashlib.sha1(body.encode("utf-8")).hexdigest(), "size": len(body),
                                 "url": url, "via": "guess"}
    return files, calls


def write_aero_addresses(conn: sqlite3.Connection, ts: str, files: dict[str, Any]) -> list[str]:
    """ファイルごとに、初めて見た時刻と、中身が変わった時刻を残す。新しく出た・変わったファイルの名前を返す。

    中身の印は読み方（一覧 / 1つずつ）で作り方が違うので、同じ読み方どうしのときだけ比べる。"""
    changed = []
    for name, f in files.items():
        prev = conn.execute("SELECT sha, via FROM aero_address_files WHERE name=?", (name,)).fetchone()
        if prev is None:
            conn.execute("INSERT INTO aero_address_files(name, sha, via, size, url, first_seen, last_seen, changed_at) "
                         "VALUES (?,?,?,?,?,?,?,NULL)", (name, f.get("sha"), f.get("via"), f.get("size"), f.get("url"),
                                                         ts, ts))
            changed.append(name)
            continue
        moved = prev[1] == f.get("via") and prev[0] != f.get("sha")
        conn.execute("UPDATE aero_address_files SET sha=?, via=?, size=?, url=?, last_seen=?, "
                     "changed_at=CASE WHEN ? THEN ? ELSE changed_at END WHERE name=?",
                     (f.get("sha"), f.get("via"), f.get("size"), f.get("url"), ts, int(moved), ts, name))
        if moved:
            changed.append(name)
    return changed
