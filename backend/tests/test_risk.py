"""練習の見張り（M5b）のテスト。ルールだけのテストと、建玉を実際に動かすテスト。"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from farm_radar import api
from farm_radar.config import RiskSettings
from farm_radar.execution import risk_job
from farm_radar.execution import views as pviews
from farm_radar.execution.jobs import run_paper
from farm_radar.execution.paper import CATS, PaperError, PaperExecutor
from farm_radar.fx import FxRate
from farm_radar.notify.service import EXIT_ALL_YES, Notifier
from farm_radar.risk.rules import PortfolioInput, PositionInput, check_portfolio, check_position
from farm_radar.scheduler import in_fast_window

from .test_notify import OWNER, FakeTelegram
from .test_paper import WETH_POOL, FakeFx, _config, _extend, _open, world  # noqa: F401
from .test_scoring_run import NOW, TOKENS

S = RiskSettings()
PF = PortfolioInput(reward_symbol="UP", reward_change_24h=0.0, today_net_usd=0.0, open_capital_usd=1000)


def _p(**kw):
    base = dict(pair="A/B", lower=0.9, upper=1.1, price=1.0, started_red=False, signal="green",
                rebalance_net_pct=1.0)
    return PositionInput(**{**base, **kw})


# --- ルールだけ -----------------------------------------------------------------------------

def test_nothing_when_calm():
    assert check_position(_p(), PF, S) == []


def test_edge_near_is_caution():
    f = check_position(_p(price=1.095), PF, S)
    assert [x.kind for x in f] == ["edge_near"] and f[0].level == "caution"


def test_out_of_range_rebalances_or_exits_on_low_score():
    f = check_position(_p(price=1.2, minutes_out_of_range=15), PF, S)
    assert f[0].level == "rebalance"
    f = check_position(_p(price=1.2, minutes_out_of_range=10), PF, S)
    assert f == []                                                  # 15分たっていない
    f = check_position(_p(price=1.2, minutes_out_of_range=30, rebalance_net_pct=-0.1), PF, S)
    assert f[0].level == "exit" and f[0].kind == "out_of_range_low_score"


def test_red_signal_exits_but_not_for_red_start():
    assert check_position(_p(signal="red"), PF, S)[0].kind == "signal_red"
    assert check_position(_p(signal="red", started_red=True), PF, S) == []    # 2026-09-29 オーナー決定


def test_reward_token_drop_exits_even_for_red_start():
    pf = dataclasses.replace(PF, reward_change_24h=-0.21)
    f = check_position(_p(started_red=True), pf, S)
    assert f[0].level == "exit" and f[0].kind == "reward_token_drop"


def test_liquidity_drop_is_emergency_and_ranks_first():
    f = check_position(_p(signal="red", liquidity_now=40.0, liquidity_1h_ago=100.0), PF, S)
    assert f[0].level == "emergency" and f[1].kind == "signal_red"


def test_reward_shortfall_waits_for_enough_hours():
    kw = dict(predicted_income_day=100.0, actual_income_day=50.0)
    assert check_position(_p(**kw, income_hours=1.0), PF, S) == []
    assert check_position(_p(**kw, income_hours=3.0), PF, S)[0].kind == "reward_shortfall"


def test_daily_loss_emergency():
    assert check_portfolio(dataclasses.replace(PF, today_net_usd=-49.0), S) == []
    assert check_portfolio(dataclasses.replace(PF, today_net_usd=-50.0), S)[0].level == "emergency"


def test_fast_window_jst():
    assert in_fast_window(datetime(2026, 9, 29, 13, 5, tzinfo=UTC), ("22:00", "23:30"))
    assert not in_fast_window(datetime(2026, 9, 29, 14, 45, tzinfo=UTC), ("22:00", "23:30"))
    assert in_fast_window(datetime(2026, 9, 29, 15, 30, tzinfo=UTC), ("23:00", "01:00"))   # 日をまたぐ窓


# --- 建玉を実際に動かす -------------------------------------------------------------------------

@pytest.fixture
def calm(monkeypatch):
    """報酬トークンの24時間の変化は0（偽の記録の UP の値動きで離脱しないように）。"""
    monkeypatch.setattr(risk_job, "reward_change_24h", lambda *a, **k: ("UP", 0.0))


def _set_score(conn, *, signal="green", net=1.0):
    conn.execute("UPDATE scores SET signal=?, net_daily_pct=?", (signal, net))
    conn.commit()


def _pos(conn, pid):
    return conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()


def _push_out(conn, pid, hours):
    """WETH プールの価格を、建玉のレンジの少し上（+1%）にして記録を足す（損が大きくなりすぎないように）。"""
    import math
    from farm_radar.scoring.prices import Q96
    target = _pos(conn, pid)["upper"] * 1.01
    start = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    _extend(conn, hours)
    sp = str(int(math.sqrt(target * 10 ** (6 - 18)) * Q96))
    conn.execute("UPDATE pool_snapshots SET price=?, sqrt_price_x96=? WHERE pool_id=? AND ts>?",
                 (target, sp, WETH_POOL, start))
    conn.commit()


def _events(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM risk_events ORDER BY id")]


def test_out_of_range_rebalances_and_keeps_totals(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    before = _pos(conn, ref.position_id)
    _push_out(conn, ref.position_id, 3)       # レンジの外に3時間
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=4))
    p = _pos(conn, ref.position_id)
    ev = _events(conn)
    assert p["status"] == "open" and ev[0]["action"] == "rebalanced"
    assert {e["level"] for e in ev[1:]} <= {"caution"}             # 同じ回の注意（報酬が予測より少ない等）は記録だけ
    last_price = conn.execute("SELECT price FROM pool_snapshots WHERE pool_id=? ORDER BY ts DESC LIMIT 1",
                              (WETH_POOL,)).fetchone()[0]
    assert p["lower"] < last_price < p["upper"] and p["lower"] != before["lower"]      # 今の価格を中心に置き直した
    st = json.loads(p["state_json"])
    assert st["rebalances"] == 1 and "out_since" not in st
    # 行ごとの6区分の合計 = 純損益、評価額 = 入れた額 + 累計（置き直しの前後で途切れない）
    rows = conn.execute("SELECT * FROM position_pnl WHERE position_id=? ORDER BY ts", (ref.position_id,)).fetchall()
    for r in rows:
        assert sum(r[c] or 0 for c in CATS) == pytest.approx(r["net"], abs=1e-9)
    assert rows[-1]["value_usd"] == pytest.approx(1000 + sum(r["net"] for r in rows))
    reb = [r for r in rows if json.loads(r["detail_json"]).get("event") == "rebalance"][0]
    assert reb["other"] < 0 and reb["direction"] == pytest.approx(0, abs=1e-9) and reb["gamma"] == pytest.approx(0, abs=1e-9)
    notes = [r[0] for r in conn.execute("SELECT note FROM ledger WHERE position_id=?", (ref.position_id,))]
    assert any(n.startswith("置き直し") for n in notes)
    # 通知の箱にも入る
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='paper_rebalance'").fetchone()[0] == 1
    # 次の回に同じ価格（新しいレンジの中）なら、何もしない
    start = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    _extend(conn, 1)
    conn.execute("UPDATE pool_snapshots SET price=?, sqrt_price_x96=(SELECT sqrt_price_x96 FROM pool_snapshots "
                 "WHERE pool_id=? AND ts=?) WHERE pool_id=? AND ts>?", (last_price, WETH_POOL, start, WETH_POOL, start))
    conn.commit()
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=5))
    assert [e for e in _events(conn) if e["level"] != "caution"] == ev[:1]


def test_out_of_range_with_low_score_closes(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn, net=-0.5)
    ex, ref = _open(conn, path)
    _push_out(conn, ref.position_id, 2)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3))
    p = _pos(conn, ref.position_id)
    assert p["status"] == "closed" and p["close_reason"] == "risk:out_of_range_low_score"
    assert _events(conn)[0]["level"] == "exit"
    # 閉じる手順は付録A 4章の順番で台帳に残る
    notes = [r[0] for r in conn.execute("SELECT note FROM ledger WHERE position_id=? AND note LIKE '手順%' ORDER BY id",
                                        (ref.position_id,))]
    steps = [int(n[2]) for n in notes]
    assert steps == sorted(steps) and steps[0] == 1 and 3 in steps


def test_turning_red_closes_unless_started_red(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn, signal="green")
    ex, ref = _open(conn, path)
    _set_score(conn, signal="red")
    _extend(conn, 1)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    assert _pos(conn, ref.position_id)["status"] == "closed"


def test_red_start_is_not_closed_by_red(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn, signal="red")
    ex, ref = _open(conn, path)
    _extend(conn, 1)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    assert _pos(conn, ref.position_id)["status"] == "open"


def test_high_gas_skips_normal_exit_once(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn, signal="green")
    ex, ref = _open(conn, path)
    _set_score(conn, signal="red")
    cfg = dataclasses.replace(_config(path), risk=dataclasses.replace(S, max_gas_usd_per_tx=0.0))
    _extend(conn, 1)
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2))
    _extend(conn, 1)
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3))
    assert _pos(conn, ref.position_id)["status"] == "open"
    ev = _events(conn)
    assert [e["action"] for e in ev] == ["skipped_gas"]           # 続いている間は1回だけ記録する


def test_liquidity_drop_closes_all_and_stops(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    _extend(conn, 2)
    last = conn.execute("SELECT MAX(ts) FROM pool_snapshots").fetchone()[0]
    conn.execute("UPDATE pool_snapshots SET liquidity_total=? WHERE ts=? AND pool_id=?", (str(10 ** 17), last, WETH_POOL))
    conn.commit()
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3))
    assert _pos(conn, ref.position_id)["status"] == "closed"
    assert risk_job.paper_state(conn)["stopped"] and "緊急離脱" in risk_job.paper_state(conn)["reason"]
    assert _events(conn)[-1]["action"] == "closed_all"
    with pytest.raises(PaperError, match="停止"):
        PaperExecutor(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3)).open_position(
            WETH_POOL, 1000.0)


def test_caution_is_recorded_once_per_episode(world, calm, monkeypatch):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    from farm_radar.risk.rules import Finding
    monkeypatch.setattr(risk_job, "check_position",
                        lambda *a: [Finding("caution", "edge_near", "端が近い")])
    for h in (2, 3):
        _extend(conn, 1)
        run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=h))
    assert [e["kind"] for e in _events(conn)] == ["edge_near"]
    c = pviews.card(conn, _pos(conn, ref.position_id), NOW + timedelta(hours=3))
    assert c["cautions"] == ["レンジの端が近い"]


# --- 実績の日利の見せ方（2026-09-29 オーナー指示） ----------------------------------------------

def test_actual_rate_states_and_payback(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    _extend(conn, 3)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=4))
    c = pviews.card(conn, _pos(conn, ref.position_id), NOW + timedelta(hours=4))
    assert c["actual_state"] == "short" and not c["compare_enabled"]          # 6時間未満・24時間未満
    days = c["days"]
    assert c["actual_daily_pct_with_cost"] == pytest.approx(c["change_usd"] / days / 1000 * 100)
    per_hour = (c["change_usd"] + c["open_cost_usd"]) / (days * 24)
    if per_hour > 0:
        assert c["payback_total_hours"] == pytest.approx(c["open_cost_usd"] / per_hour)
        assert c["payback_left_hours"] == pytest.approx(max(0.0, -c["change_usd"]) / per_hour)
    _extend(conn, 24)
    run_paper(conn, _config(path), TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=28))
    c = pviews.card(conn, _pos(conn, ref.position_id), NOW + timedelta(hours=28))
    assert c["actual_state"] == "ok" and c["compare_enabled"]


# --- オーナーの操作（画面と Telegram） ------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch, calm):  # noqa: F811
    from fastapi.testclient import TestClient
    path, conn = world
    cfg = _config(path)
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(minutes=5))
    monkeypatch.setattr(api, "_paper_tokens", lambda config, venue: TOKENS)
    import farm_radar.execution.paper as paper_mod
    monkeypatch.setattr(paper_mod, "rate_for", lambda conn, when, fx=None, now=None: FxRate(150.0, "2026-09-25"))
    return TestClient(api.app), conn, path


def test_api_stop_resume_exit_all(client):
    c, conn, path = client
    assert c.post("/api/paper/stop").json()["stopped"] is True
    r = c.post("/api/paper/positions", json={"pool_id": WETH_POOL})
    assert r.status_code == 400 and "停止" in r.json()["detail"]
    assert c.post("/api/paper/resume").json()["stopped"] is False
    pid = c.post("/api/paper/positions", json={"pool_id": WETH_POOL}).json()["position_id"]
    assert c.post("/api/paper/exit_all", json={}).status_code == 400          # 確認がなければ閉じない
    d = c.post("/api/paper/exit_all", json={"confirm": True}).json()
    assert d["stopped"] and "1件" in d["message"]
    assert _pos(conn, pid)["status"] == "closed" and _pos(conn, pid)["close_reason"] == "owner_exit_all"
    lst = c.get("/api/paper").json()
    assert [e["kind"] for e in lst["events"]][:3] == ["owner_exit_all", "owner_resume", "owner_stop"]
    assert lst["risk"] and lst["stopped_reason"]


def test_telegram_stop_and_exit_all_needs_button(world, calm):  # noqa: F811
    path, conn = world
    cfg = _config(path)
    ex, ref = _open(conn, path)

    class Tg(FakeTelegram):
        def __init__(self, updates):
            super().__init__(updates)
            self.buttons = []

        def send_buttons(self, text, buttons):
            self.buttons.append(buttons)
            self.sent.append(text)

        def answer_button(self, cid):
            pass

    now = NOW + timedelta(minutes=10)
    msg = lambda uid, text: {"update_id": uid, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text}}  # noqa: E731
    tg = Tg([msg(1, "/stop"), msg(2, "/exit_all")])
    n = Notifier(cfg, tg, home=lambda: {}, now=lambda: now)
    n._tokens = lambda: TOKENS
    n.poll_once()
    assert "停止しました" in tg.sent[0] and tg.buttons == [[("全部閉じる", EXIT_ALL_YES), ("やめる", "exit_all:no")]]
    assert _pos(conn, ref.position_id)["status"] == "open"                    # ボタンを押すまで閉じない
    # ほかの人が押しても無視する
    other = {"update_id": 3, "callback_query": {"id": "x", "from": {"id": 999}, "data": EXIT_ALL_YES,
                                                "message": {"date": now.timestamp()}}}
    mine = {"update_id": 4, "callback_query": {"id": "y", "from": {"id": OWNER}, "data": EXIT_ALL_YES,
                                               "message": {"date": now.timestamp()}}}
    tg._updates = [other, mine]
    n.poll_once()
    assert _pos(conn, ref.position_id)["status"] == "closed"
    assert "全部閉じました" in tg.sent[-1]


# --- 2026-09-29 オーナー決定で加えたルール --------------------------------------------------------

def test_dump_exits_on_1h_or_24h_drop():
    assert check_position(_p(token_moves=(("MEME", -0.10, -0.20),)), PF, S) == []
    f = check_position(_p(token_moves=(("MEME", -0.16, None),)), PF, S)
    assert f[0].level == "exit" and f[0].kind == "dump" and "1時間" in f[0].message_ja
    f = check_position(_p(token_moves=(("MEME", 0.0, -0.31),), started_red=True), PF, S)
    assert f[0].kind == "dump" and "24時間" in f[0].message_ja          # 🔴で始めた練習にも当てはめる


def test_hedge_cost_caution():
    kw = dict(actual_income_day=100.0, income_hours=3.0)
    assert check_position(_p(**kw, hedge_cost_day=50.0), PF, S) == []
    f = check_position(_p(**kw, hedge_cost_day=51.0), PF, S)
    assert f[0].level == "caution" and f[0].kind == "hedge_cost"


def test_usdg_needs_two_readings_below():
    assert check_portfolio(dataclasses.replace(PF, usdg_prices=(0.97,)), S) == []
    assert check_portfolio(dataclasses.replace(PF, usdg_prices=(0.97, 0.99)), S) == []
    f = check_portfolio(dataclasses.replace(PF, usdg_prices=(0.97, 0.979)), S)
    assert f[0].level == "emergency" and f[0].kind == "usdg_depeg"


def test_contract_change_is_emergency_and_daily_loss_uses_total_assets():
    f = check_portfolio(dataclasses.replace(PF, contract_changes=("up. voter: 持ち主の変更",)), S)
    assert f[0].kind == "contract_change"
    f = check_portfolio(dataclasses.replace(PF, today_net_usd=-60.0), S)
    assert "総資産 $1,000" in f[0].message_ja


class FakeRpc:
    """読み取りだけの偽の RPC。values[(address, 関数)] を返す。無い関数は revert、fail=True なら通信の失敗。"""

    def __init__(self):
        from farm_radar.rpc.abi import selector
        self.sel = {"0x" + selector(s).hex(): s for s in
                    ("owner()", "paused()", "governor()", "emergencyCouncil()", "epochGovernor()")}
        self.values: dict[tuple[str, str], str] = {}
        self.code = "0x6000"
        self.fail = False

    def get_code(self, address, block="latest"):
        if self.fail:
            raise RuntimeError("timeout")
        return self.code

    def get_storage(self, address, slot, block="latest"):
        if self.fail:
            raise RuntimeError("timeout")
        return "0x" + "0" * 64

    def eth_call(self, to, data, block):
        from farm_radar.rpc.client import RpcCallError
        if self.fail:
            raise RuntimeError("timeout")
        v = self.values.get((to.lower(), self.sel.get(data, "?")))
        if v is None:
            raise RpcCallError(3, "execution reverted")
        return v


def _addr_word(a: str) -> str:
    return "0x" + "0" * 24 + a.removeprefix("0x").rjust(40, "0")


def test_contract_watch_baseline_change_and_unconfirmed(world):  # noqa: F811
    from farm_radar.config import load_venue
    from farm_radar.execution import contract_watch
    path, conn = world
    cfg = _config(path)
    venue = load_venue("up-robinhood", cfg.root)
    voter = venue["contracts"]["voter"]["address"].lower()
    rpc = FakeRpc()
    rpc.values[(voter, "governor()")] = _addr_word("0x1111")
    assert contract_watch.check(conn, rpc, venue, TOKENS, NOW) == []       # 1回目は記録するだけ
    st = {d["address"]: d for d in contract_watch.status(conn)}
    assert "管理者（governor）" in st[voter]["ok"] and "持ち主（owner）" in st[voter]["unconfirmed"]
    rpc.fail = True                                                        # 通信の失敗は比べない
    assert contract_watch.check(conn, rpc, venue, TOKENS, NOW + timedelta(minutes=15)) == []
    rpc.fail = False
    rpc.values[(voter, "governor()")] = _addr_word("0x2222")
    ch = contract_watch.check(conn, rpc, venue, TOKENS, NOW + timedelta(minutes=30))
    assert len(ch) == 1 and "持ち主の変更" in ch[0]
    assert contract_watch.check(conn, rpc, venue, TOKENS, NOW + timedelta(minutes=45)) == []   # 1回だけ
    rpc.code = "0x6001"                                                    # プログラムの中身が変わった
    ch = contract_watch.check(conn, rpc, venue, TOKENS, NOW + timedelta(hours=1))
    assert ch and all("入れ替え" in c for c in ch)


def test_contract_change_closes_all_and_stops(world, calm):  # noqa: F811
    from farm_radar.config import load_venue
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    cfg = _config(path)
    venue = load_venue("up-robinhood", cfg.root)
    rpc = FakeRpc()
    usdg = next(iter(TOKENS.stablecoins))
    rpc.values[(usdg, "paused()")] = "0x" + "0" * 64
    _extend(conn, 1)
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2), rpc=rpc, venue=venue)
    assert _pos(conn, ref.position_id)["status"] == "open"
    rpc.values[(usdg, "paused()")] = "0x" + "0" * 63 + "1"                  # USDG が止められた
    _extend(conn, 1)
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=3), rpc=rpc, venue=venue)
    assert _pos(conn, ref.position_id)["status"] == "closed"
    ev = _events(conn)[-1]
    assert ev["kind"] == "contract_change" and ev["action"] == "closed_all" and "停止" in ev["message_ja"]
    assert risk_job.paper_state(conn)["stopped"]


class FakeGT:
    def __init__(self, price):
        self.price = price

    def tokens(self, addrs):
        from farm_radar.external.geckoterminal import TokenMarket
        return {a: TokenMarket(a, self.price, None, None) for a in addrs}


def test_usdg_below_twice_closes_all(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    cfg = _config(path)
    _extend(conn, 1)
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2), gt=FakeGT(0.97))
    assert _pos(conn, ref.position_id)["status"] == "open"                 # 1回目はまだ
    run_paper(conn, cfg, TOKENS, fx=FakeFx(), now=NOW + timedelta(hours=2, minutes=15), gt=FakeGT(0.97))
    assert _pos(conn, ref.position_id)["status"] == "closed"
    assert _events(conn)[-1]["kind"] == "usdg_depeg"


def test_card_shows_rebalance_count_and_cost(world, calm):  # noqa: F811
    path, conn = world
    _set_score(conn)
    ex, ref = _open(conn, path)
    res = ex.rebalance(ref, r_pct=3)
    ex.rebalance(ref, r_pct=4)
    c = pviews.card(conn, _pos(conn, ref.position_id), NOW)
    assert c["rebalances"] == 2 and c["rebalance_cost"] > res["cost_usd"] > 0
