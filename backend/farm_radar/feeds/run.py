"""一覧を読んで保存する（N2a。作り直し（渡り鳥）SPEC 13.4）。

- 読み取りだけ。お金を動かすコードはない。チェーンの読み取り口（RPC）は、預かり証の中身（N2c）を1日1回読むときだけ使う。
- 同じサイトへの呼び出しは1秒に1回まで（ratelimit.wait_turn）。
- 429（回数制限）が返ったら、その回は「rate_limited」（画面では「不明」）と記録して、次の回に回す（13.1 の10）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import FeedSettings
from ..external.http import ExternalError, JsonGetter
from . import pools, receipts, shadow, store, trial, vaults, venues
from .sources import SOURCES, Item, Source

log = logging.getLogger(__name__)
TOKEN_BATCH = 2


class Fetcher:
    """公開の読み取り口から本文を読む（テストでは差し替える）。"""

    def __init__(self, settings: FeedSettings, getter: JsonGetter | None = None):
        # 429 や 5xx は1回だけ待ってやり直す。それでもだめなら、その回はあきらめて次の回に回す
        self.http = getter or JsonGetter("https://api.merkl.xyz", min_interval=settings.min_interval_seconds,
                                         max_retries=1, backoff_seconds=10.0, timeout=settings.timeout_seconds)

    def text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.http.get_text(url, params)


def wanted_coins(conn: sqlite3.Connection, coin_chains: dict[int, str]) -> list[str]:
    """値動きを読むコイン（N2b）: 登録したチェーンの、最新の回の Merkl の機会のコインと報酬のコイン。"""
    last = conn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
    if not last or not coin_chains:
        return []
    out: set[str] = set()
    for r in conn.execute("""SELECT i.info_json FROM merkl_opportunity_snaps s JOIN feed_items i
                             ON i.source='merkl_opportunities' AND i.key=s.opportunity_id WHERE s.ts=?""", (last,)):
        info = json.loads(r[0] or "{}")
        key = coin_chains.get(info.get("chain_id"))
        if key:
            out.update(f"{key}:{a.lower()}" for a in info.get("token_addrs") or [] if isinstance(a, str) and a)
    now_s = int(datetime.fromisoformat(last).timestamp())
    for r in conn.execute("SELECT distribution_chain_id, reward_address FROM merkl_campaigns WHERE reward_address IS NOT "
                          "NULL AND (end_ts IS NULL OR end_ts > ?)", (now_s,)):
        key = coin_chains.get(r[0])
        if key:
            out.add(f"{key}:{r[1].lower()}")
    return sorted(out)


def _read_token_prices(conn: sqlite3.Connection, source: Source, fetcher: Fetcher,
                       coin_chains: dict[int, str]) -> tuple[Any, bytes, int]:
    """DefiLlama の coins の chart を、コイン2個ずつまとめて読む（1秒に1回まで）。

    1回に返せる点の数に上限があるらしく、1時間ごと7日分（169点）だと3個で 400 になった（2026-10-02 に確かめた。
    2個 = 338点は通り、3個 = 507点は 400）。コイン150個なら約75回・約75秒。
    """
    coins = wanted_coins(conn, coin_chains)
    merged: dict[str, Any] = {}
    pages = 0
    for i in range(0, len(coins), TOKEN_BATCH):
        body = fetcher.text(f"{source.url}/{','.join(coins[i:i + TOKEN_BATCH])}", source.params or None)
        pages += 1
        data = json.loads(body)
        merged.update((data or {}).get("coins") or {})
    return merged, json.dumps(merged).encode("utf-8"), pages


@dataclass
class ReceiptContext:
    """チェーンの公開の読み取り口で読むものに使う（預かり証の中身 N2c・幅に配るプールの状態 N3）。
    チェーンの登録・値動きしないコイン・読み取り口の作り方。"""
    chains: dict[int, dict[str, Any]] = field(default_factory=dict)
    stables: dict[int, dict[str, str]] = field(default_factory=dict)
    rpc_factory: Callable[[dict[str, Any]], Any] | None = None
    sleep: Callable[[float], None] | None = None
    known: dict[str, dict[str, Any]] = field(default_factory=dict)   # N4a: 会場の登録（venues/known）


def _read_receipts(conn: sqlite3.Connection, fetcher: Fetcher, coin_chains: dict[int, str],
                   ctx: ReceiptContext | None, now: datetime | None) -> tuple[Any, bytes, int]:
    if ctx is None or not ctx.chains:
        return {}, b"{}", 0
    kw: dict[str, Any] = {}
    if ctx.rpc_factory:
        kw["rpc_factory"] = ctx.rpc_factory
    if ctx.sleep:
        kw["sleep"] = ctx.sleep
    data = receipts.read(conn, ctx.chains, coin_chains, ctx.stables, fetcher.text, now or datetime.now(UTC), **kw)
    return data, json.dumps(data, ensure_ascii=False).encode("utf-8"), len(data)


def _read_pools(conn: sqlite3.Connection, ctx: ReceiptContext | None, now: datetime | None) -> tuple[Any, bytes, int]:
    if ctx is None or not ctx.chains:
        return {}, b"{}", 0
    kw: dict[str, Any] = {}
    if ctx.rpc_factory:
        kw["rpc_factory"] = ctx.rpc_factory
    if ctx.sleep:
        kw["sleep"] = ctx.sleep
    data = pools.read(conn, ctx.chains, now or datetime.now(UTC), **kw)
    return data, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), len(data)


def _read_venues(conn: sqlite3.Connection, ctx: ReceiptContext | None, now: datetime | None) -> tuple[Any, bytes, int]:
    if ctx is None or not ctx.chains:
        return {}, b"{}", 0
    if not ctx.known:
        # 2026-10-03 オーナーのパソコン: 会場の登録のフォルダーがコンテナに無く「ok・0件」に見えた。読めていないことを失敗として出す
        raise ValueError("会場の登録（venues/known）が読めていない。docker-compose.yml の feeds に ./venues があるか確かめる")
    kw: dict[str, Any] = {}
    if ctx.rpc_factory:
        kw["rpc_factory"] = ctx.rpc_factory
    if ctx.sleep:
        kw["sleep"] = ctx.sleep
    data = venues.read(conn, ctx.chains, ctx.known, now or datetime.now(UTC), **kw)
    return data, json.dumps(data, ensure_ascii=False).encode("utf-8"), len(data)


def _read_vaults(conn: sqlite3.Connection, ctx: ReceiptContext | None) -> tuple[Any, bytes, int]:
    if ctx is None or not ctx.chains:
        return {}, b"{}", 0
    if not ctx.known:
        raise ValueError("会場の登録（venues/known）が読めていない。docker-compose.yml の feeds に ./venues があるか確かめる")
    kw: dict[str, Any] = {"rpc_factory": ctx.rpc_factory} if ctx.rpc_factory else {}
    data = vaults.read(conn, ctx.chains, ctx.known, **kw)
    return data, json.dumps(data, ensure_ascii=False).encode("utf-8"), len(data)


def _read(source: Source, fetcher: Fetcher) -> tuple[Any, bytes, int]:
    """(読んだ中身, 残す元の応答, ページ数)。ページに分かれている一覧は、少なくなるまで続けて読む。"""
    if not source.page_size:
        body = fetcher.text(source.url, source.params or None)
        data = json.loads(body) if source.fmt == "json" else body
        return data, body.encode("utf-8"), 1
    rows: list[Any] = []
    pages = 0
    for page in range(source.max_pages):
        body = fetcher.text(source.url, {**source.params, "items": str(source.page_size), "page": str(page)})
        chunk = json.loads(body)
        pages += 1
        if not isinstance(chunk, list):
            raise ExternalError(f"{source.url}: 一覧の形が違います（{type(chunk).__name__}）")
        rows.extend(chunk)
        if len(chunk) < source.page_size:
            break
    return rows, json.dumps(rows, ensure_ascii=False).encode("utf-8"), pages


def _read_trial(conn: sqlite3.Connection, source: Source, fetcher: Fetcher, ctx: trial.TrialContext | None,
                now: datetime) -> tuple[Any, bytes, int]:
    """N5a の記録（feeds/trial.py・feeds/shadow.py）。設定が渡っていなければ何も読まない。"""
    ctx = ctx or trial.TrialContext()
    if source.id == "merkl_rewards":
        data, pages = trial.read_merkl_rewards(conn, fetcher.text, ctx, now)
    elif source.id == "llama_yield_history":
        data, pages = trial.read_llama_history(conn, fetcher.text, ctx, now)
    elif source.id == "lighter_funding_history":
        data, pages = trial.read_lighter_history(conn, fetcher.text, ctx, now)
    elif source.id == "aero_addresses":
        data, pages = trial.read_aero_addresses(fetcher.text)
    else:                                   # shadow_predictions
        if ctx.shadow is None:
            return {}, b"{}", 0
        try:
            data, pages = ctx.shadow(conn, now), 0
        except (ExternalError, ValueError, TypeError):
            raise
        except Exception as exc:            # 計算の途中の失敗も「失敗」として記録し、ほかの一覧は続けて読む
            raise ValueError(f"影の記録の計算に失敗: {type(exc).__name__}: {exc}") from exc
    return data, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), pages


TRIAL_SOURCES = ("merkl_rewards", "llama_yield_history", "lighter_funding_history", "aero_addresses",
                 "shadow_predictions")


def run_source(conn: sqlite3.Connection, source: Source, fetcher: Fetcher, settings: FeedSettings,
               now: datetime | None = None, coin_chains: dict[int, str] | None = None,
               receipt_ctx: ReceiptContext | None = None, trial_ctx: trial.TrialContext | None = None
               ) -> dict[str, Any]:
    """1つの一覧を読んで保存する。結果（status・件数）を返す。"""
    now = now or datetime.now(UTC)
    run_id = store.start_run(conn, source.id, now)
    try:
        if source.id in TRIAL_SOURCES:
            data, raw, pages = _read_trial(conn, source, fetcher, trial_ctx, now)
        elif source.id in ("token_prices", "token_prices_30d"):
            data, raw, pages = _read_token_prices(conn, source, fetcher, coin_chains or {})
        elif source.id == "receipts":
            data, raw, pages = _read_receipts(conn, fetcher, coin_chains or {}, receipt_ctx, now)
        elif source.id == "pool_states":
            data, raw, pages = _read_pools(conn, receipt_ctx, now)
        elif source.id == "venue_checks":
            data, raw, pages = _read_venues(conn, receipt_ctx, now)
        elif source.id == "vault_states":
            data, raw, pages = _read_vaults(conn, receipt_ctx)
        else:
            data, raw, pages = _read(source, fetcher)
        items: list[Item] = source.parse(data)
    except ExternalError as exc:
        status = "rate_limited" if getattr(exc, "status", None) == 429 else "error"
        store.finish_run(conn, run_id, datetime.now(UTC), status, error=str(exc)[:500])
        log.warning("feed failed", extra={"data": {"source": source.id, "status": status, "error": str(exc)}})
        return {"source": source.id, "status": status, "error": str(exc)}
    except (ValueError, TypeError) as exc:      # JSON が壊れていた・形が違った
        store.finish_run(conn, run_id, datetime.now(UTC), "error", error=f"読めない応答: {exc}"[:500])
        log.warning("feed unreadable", extra={"data": {"source": source.id, "error": str(exc)}})
        return {"source": source.id, "status": "error", "error": str(exc)}

    raw_path = None
    last_raw = store.last_raw_at(conn, source.id)
    if not source.raw_every_minutes or last_raw is None or \
            now - last_raw >= timedelta(minutes=source.raw_every_minutes) - timedelta(minutes=2):
        raw_path = store.save_raw(settings.raw_dir, source.id, now, raw, "json" if source.fmt == "json" else "md")

    prev = store.last_ok(conn, source.id, before_id=run_id)
    seen_at = store.iso(now)
    new = store.upsert_items(conn, source.id, items, seen_at, baseline=prev is None)
    gone = store.gone_count(conn, source.id, prev["started_at"] if prev else None)
    extra = 0
    if source.id == "merkl_opportunities":
        extra = store.write_merkl(conn, seen_at, data)
    elif source.id == "llama_yields":
        extra = store.write_yields(conn, now, items)
    elif source.id == "lighter_markets":
        extra = store.write_lighter_markets(conn, seen_at, items)
    elif source.id == "lighter_funding":
        extra = store.write_lighter_funding(conn, seen_at, items)
    elif source.id in ("token_prices", "token_prices_30d"):
        extra = store.write_token_prices(conn, seen_at, data)
    elif source.id == "receipts":
        extra = store.write_receipts(conn, seen_at, data)
    elif source.id == "pool_states":
        extra = store.write_pool_states(conn, seen_at, data)
    elif source.id == "venue_checks":
        extra = store.write_venue_checks(conn, seen_at, data)
    elif source.id == "vault_states":
        extra = store.write_vault_states(conn, seen_at, data)
    elif source.id == "merkl_rewards":
        extra = trial.write_merkl_rewards(conn, seen_at, data)
    elif source.id == "llama_yield_history":
        extra = trial.write_llama_history(conn, now, data)
    elif source.id == "lighter_funding_history":
        extra = trial.write_lighter_history(conn, data)
    elif source.id == "aero_addresses":
        extra = len(trial.write_aero_addresses(conn, seen_at, data))
    elif source.id == "shadow_predictions":
        extra = shadow.write(conn, (data or {}).get("rows") or [])
    store.finish_run(conn, run_id, datetime.now(UTC), "ok", items=len(items), new_items=new if prev else 0,
                     gone_items=gone, pages=pages, bytes=len(raw), raw_path=raw_path)
    log.info("feed saved", extra={"data": {"source": source.id, "items": len(items), "new": new if prev else 0,
                                          "gone": gone, "extra": extra, "pages": pages, "bytes": len(raw)}})
    return {"source": source.id, "status": "ok", "items": len(items), "new": new if prev else 0, "gone": gone}


def daily_due(conn: sqlite3.Connection, source: Source, settings: FeedSettings, now: datetime) -> bool:
    """1日1回の一覧を、今読むべきか。今日（日本時間）まだ読めていなくて、決めた時刻を過ぎていれば読む。

    一度も読めていなければ、時刻にかかわらずすぐ読む（並べて動かし始めた日から保存を始めるため）。
    """
    last = store.last_ok(conn, source.id)
    if last is None:
        return True
    today = now.astimezone(store.JST).date()
    last_day = datetime.fromisoformat(last["started_at"]).astimezone(store.JST).date()
    return last_day < today and now.astimezone(store.JST).hour >= settings.daily_hour_jst


def every_due(conn: sqlite3.Connection, source: Source, now: datetime) -> bool:
    """every_minutes の一覧を、今読むべきか（前に読めてから every_minutes たったか。一度も読めていなければすぐ）。"""
    last = store.last_ok(conn, source.id)
    if last is None:
        return True
    return now - datetime.fromisoformat(last["started_at"]) >= timedelta(minutes=source.every_minutes)


class FeedRunner:
    """読む順番と時刻を決める（scheduler から呼ぶ）。"""

    def __init__(self, settings: FeedSettings, fetcher: Fetcher | None = None,
                 connect: Callable[[], sqlite3.Connection] | None = None, coin_chains: dict[int, str] | None = None,
                 receipt_ctx: ReceiptContext | None = None, trial_ctx: trial.TrialContext | None = None):
        self.settings = settings
        self.trial_ctx = trial_ctx                    # N5a: 試すための記録（無ければ影の記録は作らない）
        self.receipt_ctx = receipt_ctx                # N2c: 預かり証の中身を読むチェーン（無ければ読まない）
        self.coin_chains = coin_chains or {}          # N2b: コインの値動きを読むチェーン（チェーン番号 → DefiLlama の名前）
        self.fetcher = fetcher or Fetcher(settings)
        self._connect = connect or (lambda: store.connect(settings.database_path))

    def run(self, cadence: str, now: datetime | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            out = []
            for s in SOURCES:
                if s.cadence != cadence:
                    continue
                if cadence == "daily" and not daily_due(conn, s, self.settings, now or datetime.now(UTC)):
                    continue
                if s.every_minutes and not every_due(conn, s, now or datetime.now(UTC)):
                    continue
                out.append(run_source(conn, s, self.fetcher, self.settings, now, self.coin_chains, self.receipt_ctx,
                                      self.trial_ctx))
            return out
        finally:
            conn.close()
