"""venues/tokens-robinhood.yaml を公式の情報から作り直す。

使い方（リポジトリの直下で）: python -m farm_radar.tools.refresh_tokens [--db data/farm_radar.sqlite3]

書き出すもの（どれも出典URLと確認日つき。推測では埋めない）:
- ステーブルコインとWETH: Robinhood Chain 公式の Token Contracts ページに載っているアドレス
- 株トークン: Robinhood 公式の Stock Token API（/rhj/assets）にある、チェーンID 4663 のアドレス
- perp（ヘッジ先）の対応: Lighter で「同じものを追いかける」と公式に確認できたものだけ
  - WETH → Lighter の ETH
  - 株トークン → Lighter の RWA 市場仕様表で、同じティッカーの株・ETF（equity / index）として載っている perp
  - 記号が同じだけで確認できないもの（ミームコインなど）は perp_candidates_unverified に並べるだけで、使わない
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..external.http import JsonGetter
from ..external.lighter import Lighter

CHAIN_ID = 4663
CONTRACTS_URL = "https://docs.robinhood.com/chain/contracts"
ASSETS_URL = "https://api.robinhood.com/rhj/assets"
LIGHTER_ORDERBOOKS_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
LIGHTER_RWA_SPEC_URL = "https://docs.lighter.xyz/trading/real-world-assets-rwas/market-specifications.md"

# 公式の Token Contracts ページに載っているトークン（ページにこのアドレスがあることを実行時に確かめる）
OFFICIAL = {
    "WETH": {"address": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", "kind": "wrapped_native", "perp": "ETH"},
    "USDG": {"address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", "kind": "stablecoin", "perp": None},
}
# 株・ETF として扱う Lighter の市場の種類（RWA 市場仕様表の Type 列）
STOCK_MARKET_TYPES = {"equity", "index", "pre-ipo equity"}


def parse_rwa_spec(markdown: str) -> dict[str, dict[str, str]]:
    """Lighter の RWA 市場仕様表（HTMLの表）から {市場: {type, tracked}} を取り出す。"""
    rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td>", markdown)
    return {m.strip(): {"type": t.strip(), "tracked": tr.strip()} for m, t, _cap, tr in rows}


def build(db_path: Path | None = None) -> dict:
    today = datetime.now(UTC).date().isoformat()
    docs = JsonGetter("https://docs.robinhood.com", min_interval=0.5)
    page = docs.get_text(CONTRACTS_URL)
    for sym, t in OFFICIAL.items():
        if t["address"] not in page:
            raise SystemExit(f"{sym} のアドレスが公式ページに見つかりません。手で確認してください: {CONTRACTS_URL}")

    assets = JsonGetter("https://api.robinhood.com", min_interval=0.5).get("rhj/assets")["assets"]
    stock: dict[str, str] = {}
    for a in assets:
        if a.get("status") != "ASSET_STATUS_ACTIVE":
            continue
        for d in a.get("deployments") or []:
            if int(d.get("chainId", 0)) == CHAIN_ID:
                stock[d["contractAddress"].lower()] = a["tokenSymbol"]

    perps = Lighter().active_perps()
    spec_md = docs.get_text(LIGHTER_RWA_SPEC_URL)
    spec = parse_rwa_spec(spec_md)

    perp_map: dict[str, dict] = {}
    weth = OFFICIAL["WETH"]
    if "ETH" in perps:
        perp_map[weth["address"].lower()] = {
            "token_symbol": "WETH", "venue": "lighter", "symbol": "ETH", "market_id": perps["ETH"].market_id,
            "evidence": "WETH は公式 Token Contracts ページのアドレス。Lighter の ETH perp（有効）",
        }
    for addr, sym in sorted(stock.items(), key=lambda x: x[1]):
        row = spec.get(sym)
        if sym in perps and row and row["type"] in STOCK_MARKET_TYPES:
            perp_map[addr] = {
                "token_symbol": sym, "venue": "lighter", "symbol": sym, "market_id": perps[sym].market_id,
                "evidence": f"Robinhood 株トークン {sym}。Lighter RWA 仕様表: {row['type']} / {row['tracked'][:80]}",
            }

    candidates = []
    if db_path and db_path.exists():
        conn = sqlite3.connect(str(db_path))
        seen = set()
        for t, s in conn.execute(
            "SELECT token0, token0_symbol FROM pools UNION SELECT token1, token1_symbol FROM pools"
        ):
            t = (t or "").lower()
            if not s or t in perp_map or t in seen:
                continue
            seen.add(t)
            if s in perps:
                why = ("株トークンだが Lighter の RWA 仕様表に載っていない" if t in stock
                       else "記号が同じだけで、同じトークンか確認できない")
                candidates.append({"address": t, "token_symbol": s, "lighter_symbol": s, "why_unverified": why})
        conn.close()

    return {
        "chain_id": CHAIN_ID,
        "checked_at": today,
        "stablecoins": {sym: {"address": t["address"], "source_url": CONTRACTS_URL, "checked_at": today}
                        for sym, t in OFFICIAL.items() if t["kind"] == "stablecoin"},
        "wrapped_native": {sym: {"address": t["address"], "source_url": CONTRACTS_URL, "checked_at": today}
                           for sym, t in OFFICIAL.items() if t["kind"] == "wrapped_native"},
        "stock_tokens": {"source_url": ASSETS_URL, "checked_at": today, "tokens": dict(sorted(stock.items()))},
        "perps": {
            "source_urls": [LIGHTER_ORDERBOOKS_URL, LIGHTER_RWA_SPEC_URL, CONTRACTS_URL, ASSETS_URL],
            "checked_at": today,
            "map": perp_map,
            "perp_candidates_unverified": sorted(candidates, key=lambda c: c["token_symbol"]),
        },
    }


HEADER = """# Robinhood Chain のトークンの分類（自動生成。手で直さず、次のコマンドで作り直す）
#   python -m farm_radar.tools.refresh_tokens --db data/farm_radar.sqlite3
# ルール: 公式の情報で確認できたものだけを載せる。記号が同じだけのものは perp_candidates_unverified に並べ、使わない。
# - stablecoins: 値動き σ を0として扱うトークン（ドル建ての損を計算しない）
# - stock_tokens: 株トークン（米国市場の時間中と時間外で σ を分けて計算する。SPEC 3.1章）
# - perps.map: ヘッジに使える perp（ヘッジできないトークンは direction_risk を引き、判定は最高でも🟡。SPEC 3.2章・4章）
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "farm_radar.sqlite3")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "venues" / "tokens-robinhood.yaml")
    args = ap.parse_args()
    data = build(args.db)
    args.out.write_text(HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200),
                        encoding="utf-8")
    print(f"{args.out} を書きました: 株トークン {len(data['stock_tokens']['tokens'])} 個、"
          f"perp 対応 {len(data['perps']['map'])} 個、未確認の候補 {len(data['perps']['perp_candidates_unverified'])} 個")


if __name__ == "__main__":
    main()
