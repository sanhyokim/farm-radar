"""ヘッジ先アダプター（SPEC 5.2.1章）と、カードの表示（保険・値段の範囲・両替のずれ）のテスト。"""

import dataclasses
from datetime import UTC, datetime

import pytest

from farm_radar import views
from farm_radar.config import load_config
from farm_radar.hedges import build, status as hstatus
from farm_radar.hedges.base import HedgeAccount, HedgeMarket, mask, round_trip_cost
from farm_radar.hedges.lighter import LighterHedge
from farm_radar.scoring.run import choose_hedges
from farm_radar.tokens import PerpRef, TokenBook

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
NVDA = "0xnvda"


class FakeVenue:
    def __init__(self, hedge_id, taker, funding_hourly):
        self.hedge_id, self.name = hedge_id, hedge_id.capitalize()
        self.taker, self.f = taker, funding_hourly

    def markets(self):
        return {"NVDA": HedgeMarket(self.hedge_id, "NVDA", 7, self.taker, 0.0)}

    def short_funding_hourly(self, market_id, start, end):
        return [] if self.f is None else [(t, self.f) for t in range(start, end, 3600)]

    def account(self, address):
        return None


def _book():
    refs = (PerpRef("a", "NVDA", 7), PerpRef("b", "NVDA", 7))
    return TokenBook(frozenset(), {NVDA: "NVDA"}, {NVDA: refs[0]}, None, {NVDA: refs})


def test_cheapest_hedge_venue_is_chosen():
    # a: 手数料0・資金調達 0.001%/時（1日 0.024%）、b: 手数料 0.01%×2・資金調達0 → b の方が安い
    hedges = {"a": FakeVenue("a", 0.0, 0.00001), "b": FakeVenue("b", 0.01, 0.0)}
    ch = choose_hedges(hedges, _book(), {NVDA}, NOW, 7, 0.0)[NVDA]
    assert ch["hedge_id"] == "b" and ch["name"] == "B" and len(ch["candidates"]) == 2
    assert ch["cost"] == pytest.approx(0.0002)


def test_venue_without_funding_is_not_chosen_and_disabled_venue_is_skipped():
    ch = choose_hedges({"a": FakeVenue("a", 0.0, None), "b": FakeVenue("b", 0.05, 0.0)}, _book(), {NVDA}, NOW, 7, 0.0)
    assert ch[NVDA]["hedge_id"] == "b"
    ch = choose_hedges({"a": FakeVenue("a", 0.0, 0.0)}, _book(), {NVDA}, NOW, 7, 0.0)     # b は使わない設定
    assert [c["hedge_id"] for c in ch[NVDA]["candidates"]] == ["a"]
    assert choose_hedges({}, _book(), {NVDA}, NOW, 7, 0.0) == {}


def test_round_trip_cost_counts_received_funding_as_zero():
    assert round_trip_cost(0.02, -0.001, 0.0) == pytest.approx(0.0004)
    assert round_trip_cost(None, 0.001, 0.03) == pytest.approx(0.0016)
    assert round_trip_cost(0.0, None, 0.0) is None


def test_registry_and_lighter_account_parsing():
    assert set(build(["lighter", "unknown"])) == {"lighter"}

    class Http:
        def get(self, path, params=None):
            assert path == "account" and params == {"by": "l1_address", "value": "0xabc"}
            return {"accounts": [{"collateral": "412.5", "available_balance": "300",
                                  "positions": [{"symbol": "NVDA", "position": "1.5", "sign": -1, "position_value": "280"}]}]}

    class Client:
        http = Http()

    acc = LighterHedge(Client()).account("0xabc")
    assert acc == HedgeAccount("lighter", 412.5, 300.0, acc.positions)
    assert acc.positions[0].size == -1.5 and acc.positions[0].value_usd == 280.0
    assert mask("0x1234567890abcdef1234567890abcdef12345678") == "0x1234…5678"


def test_collateral_status(tmp_path, monkeypatch):
    from farm_radar.db import database as db
    conn = db.connect(tmp_path / "t.sqlite3")
    addr = "0x" + "1" * 40
    monkeypatch.setenv("LIGHTER_ACCOUNT_ADDRESS", addr)
    base = dataclasses.replace(load_config(), mode="observe")
    cfg = dataclasses.replace(base, hedge_venues=type(base.hedge_venues)(
        [dataclasses.replace(base.hedge_venues[0], account_address=addr)]))
    s = hstatus.summary(conn, cfg)
    v = s["venues"][0]
    assert v["address"] == "0x1111…1111" and addr not in str(s)          # アドレスは省略形だけ
    assert v["real"]["state"] == "waiting"
    orig = hstatus.planned_margin
    monkeypatch.setattr(hstatus, "planned_margin", lambda conn, config: 400.0)   # 開いている建玉の保険の預け金の合計
    for coll, want in ((0.0, "none"), (100.0, "short"), (400.0, "ok")):
        conn.execute("INSERT OR REPLACE INTO hedge_accounts(hedge_id, ts, collateral_usd, available_usd, positions_json, "
                     "error) VALUES ('lighter','2026-09-29T00:00:00+00:00',?,?, '[]', NULL)", (coll, coll))
        assert hstatus.summary(conn, cfg)["venues"][0]["status"]["state"] == want
    paper = dataclasses.replace(cfg, mode="paper")
    monkeypatch.setattr(hstatus, "planned_margin", orig)
    st = hstatus.summary(conn, paper)["venues"][0]["status"]
    # 練習では、開いている練習の保険の預け金の合計を預けたものとして扱う（2026-10-03。前は総資産の40%決め打ち）
    assert st["paper"] and st["state"] == "ok" and st["collateral_usd"] == pytest.approx(0.0)
    assert "保険のある練習の建玉はありません" in st["note"]


def test_card_labels():
    det = {"inputs": {"price": 0.0055, "usd": {"USDG": 1.0, "NVDA": 181.8},
                      "hedge": {"NVDA": {"hedge_id": "lighter", "name": "Lighter"}},
                      "slippage": 0.002, "slippage_source": "pool", "fee": 0.0005, "gas_usd_per_tx": 0.01}}
    h = views.hedge_label(det, 1)
    assert h["has"] and h["label"] == "保険あり（Lighter）"
    assert views.hedge_label({"inputs": {"usd": {"USDG": 1.0, "X": 2.0}, "hedge": {"X": {"hedge_id": None}}}}, 0)["label"] == "保険なし"
    # USDG/NVDA（token1 = NVDA）: NVDA のドルの値段は 1/(1+r)〜1/(1−r) 倍
    rp = views.range_prices(det, 5, "USDG", "NVDA")
    assert rp["symbol"] == "NVDA" and rp["low"] == pytest.approx(181.8 / 1.05) and rp["high"] == pytest.approx(181.8 / 0.95)
    rp = views.range_prices({"inputs": {"price": 2.0, "usd": {"A": 3.0, "USDG": 1.0}, "hedge": {"A": None}}}, 10, "A", "USDG")
    assert rp["low"] == pytest.approx(2.7) and rp["high"] == pytest.approx(3.3)
    sw = views.swap_costs(det, 550, 0.5, 550, hedge_notional=275, hedge_taker_pct=0.02)
    assert sw["slippage"] == pytest.approx(275 * 0.002) and sw["slippage_pct"] == pytest.approx(0.2)
    assert sw["open_total"] == pytest.approx(275 * 0.0025 + 0.02 + 275 * 0.0002)
    assert sw["rebalance_total"] == pytest.approx(275 * 0.0025 + 0.02)


def test_faq_file_parses_into_questions():
    from farm_radar.api import parse_faq
    from farm_radar.config import REPO_ROOT
    items = parse_faq((REPO_ROOT / "docs" / "faq.md").read_text(encoding="utf-8"))
    assert len(items) == 17 and items[0]["q"].startswith("流動性とは")
    assert any(p["analogy"] for it in items for p in it["paragraphs"])
    assert parse_faq("<!--\n## Q. 例\n-->\n## Q. A?\n答え1\n\nたとえ: 例え") == [
        {"q": "A?", "paragraphs": [{"text": "答え1", "analogy": False}, {"text": "たとえ: 例え", "analogy": True}]}]
