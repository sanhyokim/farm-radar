"""保存する一覧の定義と、読み方（N2a。作り直し（渡り鳥）SPEC 13.4）。

どれも認証のいらない公開の読み取り口。チェーンの読み取り口（RPC）は使わない。お金を動かすコードはない。
- Merkl の機会（動いている・予定のもの）とキャンペーン: 15分ごと（ボーナスが新しく付いたのを早く見つけるため）
- Merkl・DefiLlama・送金サービス（Across・Relay・LI.FI）の一覧: 1日1回
- Aero の公式のお知らせの一覧: 1時間に1回（新しい記事が出たら印を付ける）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

MERKL = "https://api.merkl.xyz/v4"

CADENCE_JA = {"15min": "15分ごと", "hourly": "1時間ごと", "daily": "1日1回"}


@dataclass(frozen=True)
class Item:
    """一覧の1件。key が同じものは同じものとして数える。"""
    key: str
    name: str | None = None
    chain: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    id: str
    label_ja: str
    url: str
    cadence: str                                   # "15min" / "hourly" / "daily"
    parse: Callable[[Any], list[Item]]
    fmt: str = "json"                              # "json" / "text"
    params: dict[str, str] = field(default_factory=dict)
    page_size: int = 0                             # 0 = ページに分かれていない。>0 = page=0,1,... を少なくなるまで読む
    max_pages: int = 40
    raw_every_minutes: int = 0                     # 0 = 毎回残す。>0 = この分数に1回だけ元の応答を残す


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None     # NaN は None


def _int(v: Any) -> int | None:
    x = _num(v)
    return None if x is None else int(x)


# --- Merkl ------------------------------------------------------------------------------------

def merkl_opportunities(data: Any) -> list[Item]:
    out = []
    for o in data if isinstance(data, list) else []:
        if not isinstance(o, dict) or not o.get("id"):
            continue
        chain = o.get("chain") or {}
        proto = o.get("protocol") or {}
        out.append(Item(
            key=str(o["id"]), name=o.get("name"), chain=chain.get("name"),
            info={
                "chain_id": o.get("chainId"), "type": o.get("type"), "action": o.get("action"),
                "protocol": proto.get("name") or proto.get("id"), "status": o.get("status"),
                "tokens": [t.get("symbol") for t in o.get("tokens") or [] if isinstance(t, dict)][:6],
                "apr": _num(o.get("apr")), "tvl": _num(o.get("tvl")), "daily_rewards": _num(o.get("dailyRewards")),
                "start": _int(o.get("earliestCampaignStart")), "end": _int(o.get("latestCampaignEnd")),
                "live_campaigns": _int(o.get("liveCampaigns")), "explorer_address": o.get("explorerAddress"),
                "deposit_url": o.get("depositUrl"), "tags": (o.get("tags") or [])[:6],
                # N2b: コインの住所（値動きの計算に使う）と、同じキャンペーンの続きを見分ける名前
                "token_addrs": [t.get("address") for t in o.get("tokens") or [] if isinstance(t, dict)][:6],
                "identifier": o.get("identifier"),
            }))
    return out


def merkl_opportunity_snap(o: dict[str, Any]) -> tuple:
    """merkl_opportunity_snaps の1行（ts を除く）。"""
    return (str(o["id"]), o.get("status"), _num(o.get("apr")), _num(o.get("maxApr")), _num(o.get("nativeApr")),
            _num(o.get("tvl")), _num(o.get("dailyRewards")), _int(o.get("liveCampaigns")), _num(o.get("maxDailyRewards")))


def merkl_campaigns(o: dict[str, Any]) -> list[dict[str, Any]]:
    """機会に入っているキャンペーン（campaigns=true で読んだとき）。"""
    out = []
    for c in o.get("campaigns") or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("campaignId") or c.get("id")
        if not cid:
            continue
        tok = c.get("rewardToken") or {}
        status = c.get("campaignStatus") or {}
        out.append({
            "campaign_id": str(cid), "merkl_id": None if c.get("id") is None else str(c["id"]),
            "opportunity_id": str(c.get("opportunityId") or o.get("id")),
            "chain_id": _int(c.get("computeChainId")), "distribution_chain_id": _int(c.get("distributionChainId")),
            "type": c.get("type"), "sub_type": None if c.get("subType") is None else str(c["subType"]),
            "distribution_type": c.get("distributionType"),
            "start_ts": _int(c.get("startTimestamp")), "end_ts": _int(c.get("endTimestamp")),
            "amount_raw": None if c.get("amount") is None else str(c["amount"]),
            "reward_symbol": tok.get("symbol"), "reward_address": tok.get("address"),
            "reward_decimals": _int(tok.get("decimals")), "reward_price": _num(tok.get("price")),
            "daily_rewards": _num(c.get("dailyRewards")), "apr": _num(c.get("apr")),
            "creator": c.get("creatorAddress"), "created_at": _int(status.get("createdAt") or c.get("createdAt")),
            **campaign_terms(c),
        })
    return out


# 配り方の細かい設定のうち、自分の利回りの計算に使うもの（N2b。research/redesign-merkl-2026-10-01.md の 1-1・3章）
_TERM_KEYS = ("apr", "targetAPR", "mode", "rewardTokenPricing", "targetTokenPricing", "side")
_WEIGHT_KEYS = ("weightFees", "weightToken0", "weightToken1")


def campaign_terms(c: dict[str, Any]) -> dict[str, Any]:
    """配り方（山分け・上限つき・固定・上乗せ）、参加できる人の制限、報酬の種類（TOKEN か POINT など）。"""
    params = c.get("params") if isinstance(c.get("params"), dict) else {}
    dmp = params.get("distributionMethodParameters") if isinstance(params.get("distributionMethodParameters"), dict) else {}
    settings = dmp.get("distributionSettings") if isinstance(dmp.get("distributionSettings"), dict) else {}
    terms = {k: settings[k] for k in _TERM_KEYS if k in settings}
    terms.update({k: params[k] for k in _WEIGHT_KEYS if k in params})
    tok = c.get("rewardToken") if isinstance(c.get("rewardToken"), dict) else {}
    whitelist = params.get("whitelist") or []
    return {
        "distribution_method": dmp.get("distributionMethod"),
        "settings_json": json.dumps(terms, ensure_ascii=False, sort_keys=True) if terms else None,
        "restricted": int(bool(whitelist) or bool(c.get("isPrivate"))),
        "hidden": int(bool(c.get("hidden"))),
        "reward_type": tok.get("type"),
        "reward_verified": None if tok.get("verified") is None else int(bool(tok.get("verified"))),
    }


def merkl_chains(data: Any) -> list[Item]:
    return [Item(str(c["id"]), c.get("name"), c.get("name"), {"live_campaigns": c.get("liveCampaigns")})
            for c in data if isinstance(c, dict) and c.get("id") is not None] if isinstance(data, list) else []


def merkl_protocols(data: Any) -> list[Item]:
    return [Item(str(p["id"]), p.get("name"), None,
                 {"daily_rewards": _num(p.get("dailyRewards")), "live_campaigns": p.get("numberOfLiveCampaigns"),
                  "url": p.get("url"), "tags": (p.get("tags") or [])[:6]})
            for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []


# --- DefiLlama ----------------------------------------------------------------------------------

def llama_chains(data: Any) -> list[Item]:
    return [Item(c["name"], c["name"], c["name"], {"tvl": _num(c.get("tvl")), "chain_id": c.get("chainId"),
                                                  "token": c.get("tokenSymbol")})
            for c in data if isinstance(c, dict) and c.get("name")] if isinstance(data, list) else []


def llama_protocols(data: Any) -> list[Item]:
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict) or not (p.get("slug") or p.get("name")):
            continue
        chains = p.get("chains") or []
        out.append(Item(str(p.get("slug") or p["name"]), p.get("name"), chains[0] if len(chains) == 1 else None,
                        {"category": p.get("category"), "chains": chains[:12], "tvl": _num(p.get("tvl")),
                         "listed_at": _int(p.get("listedAt")), "url": p.get("url")}))
    return out


def llama_yields(data: Any) -> list[Item]:
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("pool"):
            continue
        out.append(Item(str(r["pool"]), r.get("symbol"), r.get("chain"),
                        {"project": r.get("project"), "tvl": _num(r.get("tvlUsd")), "apy_base": _num(r.get("apyBase")),
                         "apy_reward": _num(r.get("apyReward")), "apy": _num(r.get("apy")),
                         "reward_tokens": (r.get("rewardTokens") or [])[:4], "meta": r.get("poolMeta")}))
    return out


# --- 送金サービス（チェーンからチェーンへお金を移す。N5・N7 で使う） ------------------------------------

def across_chains(data: Any) -> list[Item]:
    return [Item(str(c["chainId"]), c.get("name"), c.get("name"), {})
            for c in data if isinstance(c, dict) and c.get("chainId") is not None] if isinstance(data, list) else []


def relay_chains(data: Any) -> list[Item]:
    rows = (data or {}).get("chains") if isinstance(data, dict) else None
    return [Item(str(c["id"]), c.get("displayName") or c.get("name"), c.get("displayName") or c.get("name"),
                 {"vm": c.get("vmType"), "disabled": c.get("disabled"), "deposit_enabled": c.get("depositEnabled")})
            for c in rows or [] if isinstance(c, dict) and c.get("id") is not None]


def lifi_chains(data: Any) -> list[Item]:
    rows = (data or {}).get("chains") if isinstance(data, dict) else None
    return [Item(str(c["id"]), c.get("name"), c.get("name"), {"type": c.get("chainType"), "key": c.get("key")})
            for c in rows or [] if isinstance(c, dict) and c.get("id") is not None]


# --- Lighter（保険の売り場。N2b） ----------------------------------------------------------------------

LIGHTER = "https://mainnet.zklighter.elliot.ai/api/v1"


def lighter_markets(data: Any) -> list[Item]:
    """perp の銘柄（手数料・証拠金の割合・建玉の量）。単位の確認は venues/lighter.yaml。"""
    rows = (data or {}).get("order_book_details") if isinstance(data, dict) else None
    out = []
    for m in rows or []:
        if not isinstance(m, dict) or m.get("market_id") is None or m.get("market_type") != "perp":
            continue
        out.append(Item(str(m["market_id"]), m.get("symbol"), None, {
            "status": m.get("status"), "taker_pct": _num(m.get("taker_fee")), "maker_pct": _num(m.get("maker_fee")),
            "imf": _int(m.get("default_initial_margin_fraction")), "mmf": _int(m.get("maintenance_margin_fraction")),
            "open_interest": _num(m.get("open_interest")), "daily_quote_volume": _num(m.get("daily_quote_token_volume")),
            "mark_price": _num(m.get("mark_price"))}))
    return out


def lighter_funding(data: Any) -> list[Item]:
    """今の資金調達率（exchange=lighter の行だけ。8時間あたりの割合。external/lighter.py の単位の確認）。"""
    rows = (data or {}).get("funding_rates") if isinstance(data, dict) else None
    return [Item(str(r["market_id"]), r.get("symbol"), None, {"rate_8h": _num(r.get("rate"))})
            for r in rows or [] if isinstance(r, dict) and r.get("exchange") == "lighter" and r.get("market_id") is not None]


def token_prices(data: Any) -> list[Item]:
    """run._read_token_prices がまとめた {"<チェーン>:<住所>": {...}} を一覧の形にする。"""
    return [Item(k, v.get("symbol"), k.partition(":")[0], {"points": len(v.get("prices") or []),
                                                            "confidence": v.get("confidence")})
            for k, v in (data or {}).items() if isinstance(v, dict)] if isinstance(data, dict) else []


def receipts(data: Any) -> list[Item]:
    """run が feeds/receipts.py で確かめた {"<チェーン番号>:<住所>": {...}} を一覧の形にする。"""
    return [Item(k, v.get("symbol"), k.partition(":")[0], {"is_vault": v.get("is_vault"), "asset": v.get("asset"),
                                                            "asset_symbol": v.get("asset_symbol"),
                                                            "verified": v.get("verified")})
            for k, v in (data or {}).items() if isinstance(v, dict)] if isinstance(data, dict) else []


# --- Aero の公式のお知らせ ------------------------------------------------------------------------

_AERO_ENTRY = re.compile(r"^#{2,3} \[(?P<title>[^\]]+)\]\((?P<path>/articles/[^)\s]+)\)\s*$", re.M)
_AERO_DATE = re.compile(r"^\[(?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4})\]\(", re.M)


def aero_articles(text: Any) -> list[Item]:
    """https://aero.xyz/articles/index.md の記事の一覧（題名・場所・日付）。"""
    if not isinstance(text, str):
        return []
    out = []
    matches = list(_AERO_ENTRY.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        d = _AERO_DATE.search(text, m.end(), end)
        slug = m.group("path").strip("/").split("/")[-1]
        out.append(Item(slug, m.group("title"), None,
                        {"url": "https://aero.xyz" + m.group("path"), "date": d.group("date") if d else None}))
    return out


SOURCES: tuple[Source, ...] = (
    Source("merkl_opportunities", "Merkl の機会（動いている・予定）", f"{MERKL}/opportunities", "15min",
           # 並べ方を決めないと、ページの境目で同じものが2回出て、ほかのものが抜ける（2026-10-01 に確かめた。
           # 813件のうち重複13件）。預かり額の多い順に並べると重複しない
           merkl_opportunities, params={"status": "LIVE,SOON", "campaigns": "true", "sort": "tvl", "order": "desc"},
           page_size=100,
           raw_every_minutes=60),
    Source("aero_articles", "Aero の公式のお知らせ", "https://aero.xyz/articles/index.md", "hourly",
           aero_articles, fmt="text"),
    Source("merkl_chains", "Merkl の対応チェーン", f"{MERKL}/chains", "daily", merkl_chains),
    Source("merkl_protocols", "Merkl の会場", f"{MERKL}/protocols", "daily", merkl_protocols, page_size=100),
    Source("llama_chains", "DefiLlama のチェーン", "https://api.llama.fi/v2/chains", "daily", llama_chains),
    Source("llama_protocols", "DefiLlama の会場", "https://api.llama.fi/protocols", "daily", llama_protocols),
    Source("llama_yields", "DefiLlama の利回り", "https://yields.llama.fi/pools", "daily", llama_yields),
    Source("across_chains", "Across の対応チェーン（送金）", "https://app.across.to/api/swap/chains", "daily",
           across_chains),
    Source("relay_chains", "Relay の対応チェーン（送金）", "https://api.relay.link/chains", "daily", relay_chains),
    Source("lifi_chains", "LI.FI の対応チェーン（送金）", "https://li.quest/v1/chains", "daily", lifi_chains),
    # N2b: 保険（Lighter）の銘柄と資金調達率
    Source("lighter_markets", "Lighter の銘柄（保険）", f"{LIGHTER}/orderBookDetails", "daily", lighter_markets),
    Source("lighter_funding", "Lighter の資金調達率（保険の費用）", f"{LIGHTER}/funding-rates", "hourly",
           lighter_funding),
    # N2b: 登録したチェーンの機会に出てくるコインの、1時間ごとの値段（7日分）。値動きの大きさ（σ）に使う。
    # 読むコインは run.wanted_coins が決める（住所はコインごとに違うので、決まった URL ではない）
    Source("token_prices", "コインの値段（7日分。値動きの計算）", "https://coins.llama.fi/chart", "daily",
           token_prices, params={"span": "169", "period": "1h"}),
    # N2c: 値段の記録がないボーナスのコインが、中身のある預かり証か（チェーンの公開の読み取り口で読む。SPEC 13.1 の5）。
    # コインの値段のあとに読む（記録がないものだけ確かめるため、この順番のまま）
    Source("receipts", "預かり証の中身（チェーンの記録）", "eth_call", "daily", receipts),
)

BY_ID = {s.id: s for s in SOURCES}
