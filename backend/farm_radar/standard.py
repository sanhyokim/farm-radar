"""決まった形の数字（N1。設計案 1章の図の真ん中）。

読み方は系統・会場ごとに違うが、そこから先（機会の一覧・本当に残る利回り・守る・試す）はこの形だけを使う。
今は2つの入り口から作る:
- 自分で読んだチェーンの記録（会場のファイルがある会場。最新のスコアの回）
- Merkl の一覧（N2a で保存している feeds.sqlite3 の最新の回）

ここでは計算（利回りの見込み・危なさ）はしない。並べて同じ形にするだけ（計算は N2b から）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, load_venue
from .registry import chain_by_evm_id, load_chain


@dataclass(frozen=True)
class StandardOpportunity:
    source: str                     # chain（自分で読んだ） / merkl
    family: str                     # evm / solana
    chain: str | None               # 登録したチェーンの id（chains/<id>.yaml）。登録していないチェーンは None
    chain_name: str | None
    evm_chain_id: int | None
    venue: str                      # 会場の id（自分で読んだ会場）か、Merkl の会場の名前
    venue_name: str | None
    key: str                        # プールの id、または Merkl の機会の id
    name: str | None                # 例: WETH/USDG
    mechanisms: tuple[str, ...]     # 仕組みの型（registry.MECHANISMS）
    tokens: tuple[str, ...]         # コインの記号
    tvl_usd: float | None           # 預かり額
    bonus_usd_per_day: float | None # 1日のボーナス（ドル）
    bonus_token: str | None
    shown_apr_pct: float | None     # 表示の年利（%。割り引く前。Merkl の表示や、自分の計算の表示の値）
    ends_at: str | None             # 配る期間の終わり、または次の切り替え（分かるときだけ）
    days_left: float | None
    observed_at: str | None         # 数字を読んだ時刻（UTC）
    notes: tuple[str, ...] = field(default=())   # 運営の鍵・会場の警告など（そのまま並べる）
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _days_left(end: datetime | None, now: datetime) -> float | None:
    return None if end is None else max(0.0, (end - now).total_seconds() / 86400)


def from_own(conn: sqlite3.Connection, config: Config, now: datetime) -> list[StandardOpportunity]:
    """自分で読んだ会場の最新のスコアの回から作る（チェーンを読まない版では空）。"""
    from .views import epoch_flip_info

    out = []
    for vid in config.venues:
        try:
            v = load_venue(vid, config.root)
        except (OSError, ConfigError):
            continue
        chain = v.get("chain") or {}
        reward_addr = (((v.get("contracts") or {}).get("reward_token") or {}).get("address") or "").lower()
        sym = conn.execute("""SELECT CASE WHEN lower(token0)=? THEN token0_symbol ELSE token1_symbol END FROM pools
                              WHERE venue_id=? AND (lower(token0)=? OR lower(token1)=?) LIMIT 1""",
                           (reward_addr, vid, reward_addr, reward_addr)).fetchone() if reward_addr else None
        flip = epoch_flip_info(v, now)
        end = datetime.fromisoformat(flip["at"]) if flip else None
        notes = tuple(f"{w.get('code', '')} {w.get('title_ja', '')}".strip() for w in v.get("warnings") or [])
        rows = conn.execute(
            """SELECT s.*, p.token0_symbol, p.token1_symbol, p.address FROM scores s
               JOIN (SELECT pool_id, MAX(ts) AS ts FROM scores WHERE venue_id=? GROUP BY pool_id) m
                 ON m.pool_id = s.pool_id AND m.ts = s.ts
               JOIN pools p ON p.id = s.pool_id ORDER BY s.pool_id""", (vid,)).fetchall()
        for r in rows:
            inputs = (json.loads(r["details_json"] or "{}").get("inputs") or {})
            reward_day = inputs.get("reward_usd_day")
            tvl = r["tvl_usd"]
            out.append(StandardOpportunity(
                source="chain", family=chain.get("family", "evm"), chain=chain.get("id"), chain_name=chain.get("name"),
                evm_chain_id=chain.get("chain_id"), venue=vid, venue_name=v.get("name"), key=r["pool_id"],
                name=f"{r['token0_symbol']}/{r['token1_symbol']}",
                mechanisms=tuple(v.get("mechanisms") or ()), tokens=(r["token0_symbol"], r["token1_symbol"]),
                tvl_usd=tvl, bonus_usd_per_day=reward_day, bonus_token=sym[0] if sym else None,
                shown_apr_pct=(reward_day * 365 / tvl * 100) if reward_day is not None and tvl else None,
                ends_at=flip["at"] if flip else None, days_left=_days_left(end, now), observed_at=r["ts"],
                notes=notes, url=v.get("website"),
            ))
    return out


def _feeds_conn(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def from_merkl(feeds_db: Path, config: Config, now: datetime, registered_only: bool = True
               ) -> list[StandardOpportunity]:
    """Merkl の機会（最新の15分の回）から作る。registered_only なら config.yaml の chains のチェーンだけ。"""
    conn = _feeds_conn(feeds_db)
    if conn is None:
        return []
    try:
        by_id = chain_by_evm_id(config.chains, config.root)
        last = conn.execute("SELECT MAX(ts) FROM merkl_opportunity_snaps").fetchone()[0]
        if not last:
            return []
        rows = conn.execute(
            """SELECT s.*, i.name, i.chain AS chain_label, i.info_json FROM merkl_opportunity_snaps s
               JOIN feed_items i ON i.source='merkl_opportunities' AND i.key = s.opportunity_id
               WHERE s.ts=? ORDER BY s.tvl DESC""", (last,)).fetchall()
        now_s = int(now.timestamp())
        rewards: dict[str, set[str]] = {}
        for c in conn.execute("SELECT opportunity_id, reward_symbol FROM merkl_campaigns "
                              "WHERE reward_symbol IS NOT NULL AND (end_ts IS NULL OR end_ts > ?)", (now_s,)):
            rewards.setdefault(str(c["opportunity_id"]), set()).add(c["reward_symbol"])
        out = []
        for r in rows:
            info = json.loads(r["info_json"] or "{}")
            evm_id = info.get("chain_id")
            cid = by_id.get(evm_id) if isinstance(evm_id, int) else None
            if registered_only and cid is None:
                continue
            chain = load_chain(cid, config.root) if cid else {}
            end = datetime.fromtimestamp(info["end"], UTC) if info.get("end") else None
            out.append(StandardOpportunity(
                source="merkl", family=chain.get("family", "evm"), chain=cid,
                chain_name=chain.get("name") or r["chain_label"], evm_chain_id=evm_id,
                venue=str(info.get("protocol") or "merkl"), venue_name=info.get("protocol"), key=str(r["opportunity_id"]),
                name=r["name"], mechanisms=("later",), tokens=tuple(info.get("tokens") or ()),
                tvl_usd=r["tvl"], bonus_usd_per_day=r["daily_rewards"], bonus_token=",".join(sorted(rewards.get(str(r["opportunity_id"]), ()))) or None,
                shown_apr_pct=r["apr"],
                ends_at=end.isoformat(timespec="seconds") if end else None, days_left=_days_left(end, now),
                observed_at=r["ts"], notes=(f"Merkl の型: {info.get('type')}",) if info.get("type") else (),
                url=info.get("deposit_url"),
            ))
        return out
    finally:
        conn.close()


def collect(conn: sqlite3.Connection, config: Config, now: datetime | None = None, registered_only: bool = True
            ) -> list[StandardOpportunity]:
    now = now or datetime.now(UTC)
    return from_own(conn, config, now) + from_merkl(config.feeds.database_path, config, now, registered_only)
