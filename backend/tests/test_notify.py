"""通知（M4）のテスト。Telegram には実際にはつながず、送った文を記録する偽物で確かめる。"""

import dataclasses
import json
import logging
from datetime import timedelta

import httpx
import pytest

from farm_radar import api
from farm_radar.config import load_config
from farm_radar.db import database as db
from farm_radar.logging_setup import JsonFormatter
from farm_radar.notify import events, report
from farm_radar.notify.service import Notifier
from farm_radar.notify.telegram import Telegram, TelegramError, TelegramSettings, settings_from_env, split_text
from farm_radar.scoring.run import score_venue

from .test_scoring_run import NOW, FakeGT, _ctx, _fill

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"
OWNER = 555000111


class FakeSender:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> None:
        if self.fail:
            raise TelegramError("sendMessage: 502 Bad Gateway")
        self.sent.append(text)


class FakeTelegram(FakeSender):
    owner_id = OWNER

    def __init__(self, updates=None):
        super().__init__()
        self._updates = list(updates or [])

    def updates(self, offset, wait_seconds=25):
        out, self._updates = self._updates, []
        return out


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    _fill(conn, 24 * 8)
    for h in range(30, -1, -1):
        score_venue(conn, _ctx(FakeGT()), now=NOW - timedelta(hours=h, minutes=-1))
    cfg = dataclasses.replace(load_config(), database_path=path)
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "_now", lambda: NOW + timedelta(minutes=5))
    yield cfg, conn
    conn.close()


def _latest_ts(conn):
    return conn.execute("SELECT MAX(ts) FROM scores").fetchone()[0]


# --- 設定とトークンの扱い ------------------------------------------------------------------

def test_settings_need_both_token_and_owner():
    assert settings_from_env({}) is None
    assert settings_from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
    assert settings_from_env({"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_OWNER_ID": "1"}) is None   # 形がちがう
    assert settings_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_OWNER_ID": "@me"}) is None  # 数字でない
    s = settings_from_env({"TELEGRAM_BOT_TOKEN": f" {TOKEN} ", "TELEGRAM_OWNER_ID": str(OWNER)})
    assert s.owner_id == OWNER and TOKEN not in repr(s)


def test_token_never_appears_in_errors_or_logs():
    def boom(request):
        raise httpx.ConnectError(f"failed to connect {request.url}")
    tg = Telegram(TelegramSettings(TOKEN, OWNER), client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as e:
        tg.send("hi")
    assert TOKEN not in str(e.value) and "***" in str(e.value)
    # ログにそのまま書かれても隠れる
    rec = logging.LogRecord("x", logging.ERROR, "", 0, f"url https://api.telegram.org/bot{TOKEN}/sendMessage", (), None)
    assert TOKEN not in JsonFormatter().format(rec)


def test_telegram_sends_only_to_owner_and_splits_long_text():
    got = []

    def ok(request):
        got.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})
    tg = Telegram(TelegramSettings(TOKEN, OWNER), client=httpx.Client(transport=httpx.MockTransport(ok)))
    tg.send("a\n" * 3000)
    assert len(got) == 2 and all(p["chat_id"] == OWNER for p in got)
    assert all(len(p) <= 4000 for p in split_text("x" * 9000))


# --- 判定の変化とエラーの通知 ----------------------------------------------------------------

def test_new_green_and_green_to_red_are_recorded_once(setup):
    cfg, conn = setup
    ts = _latest_ts(conn)
    pools = [r[0] for r in conn.execute("SELECT pool_id FROM scores WHERE ts=? ORDER BY pool_id", (ts,))]
    prev = conn.execute("SELECT MAX(ts) FROM scores WHERE ts<?", (ts,)).fetchone()[0]
    # 1つ目: 前は🟡 → 今🟢、2つ目: 前は🟢 → 今🔴、3つ目: 前も今も🔴（通知しない）
    for pid, (a, b) in zip(pools, [("yellow", "green"), ("green", "red"), ("red", "red")]):
        conn.execute("UPDATE scores SET signal=? WHERE pool_id=? AND ts=?", (a, pid, prev))
        conn.execute("UPDATE scores SET signal=? WHERE pool_id=? AND ts=?", (b, pid, ts))
    conn.commit()
    assert events.detect_signal_changes(conn, "up-robinhood", "up.", ts) == 2
    assert events.detect_signal_changes(conn, "up-robinhood", "up.", ts) == 0     # 同じ回は二度記録しない
    msgs = {r["kind"]: r["message_ja"] for r in conn.execute("SELECT * FROM alerts")}
    # SPEC 8章の例の形:「🟢 USDG/COIN（up.）純日利 0.42%（最適レンジ±1%）。理由: …」
    assert msgs["signal_green"].startswith("🟢 ") and "（up.）純日利" in msgs["signal_green"]
    assert "最適レンジ±" in msgs["signal_green"] and "理由: " in msgs["signal_green"]
    assert msgs["signal_drop"].startswith("🟢→🔴 ")


def test_outbox_sends_once_and_retries_after_failure(setup):
    cfg, conn = setup
    now = NOW + timedelta(minutes=5)
    events.record_error(conn, "up-robinhood", "データ収集の失敗", f"https://api.telegram.org/bot{TOKEN}/x", now=now)
    # 同じエラーは1時間に1回だけ
    assert not events.record_error(conn, "up-robinhood", "データ収集の失敗", "again", now=now + timedelta(minutes=5))
    assert events.record_error(conn, "up-robinhood", "データ収集の失敗", "later", now=now + timedelta(minutes=65))
    failing = FakeSender(fail=True)
    assert events.send_pending(conn, failing, now=now + timedelta(minutes=66)) == 0
    s = FakeSender()
    assert events.send_pending(conn, s, now=now + timedelta(minutes=66)) == 2
    assert events.send_pending(conn, s, now=now + timedelta(minutes=67)) == 0
    assert len(s.sent) == 1 and "お知らせ 2件" in s.sent[0] and TOKEN not in s.sent[0]
    # 設定がないときは送らない（記録は残る）
    assert events.send_pending(conn, None) == 0


def test_old_alerts_are_not_sent(setup):
    cfg, conn = setup
    events.record_error(conn, "up-robinhood", "古いエラー", "x", now=NOW - timedelta(hours=30))
    s = FakeSender()
    assert events.send_pending(conn, s, now=NOW, max_age_hours=24) == 0 and not s.sent


def test_reward_drop_alert_gets_pool_name(setup):
    cfg, conn = setup
    pid, pair = conn.execute("SELECT id, token0_symbol || '/' || token1_symbol FROM pools LIMIT 1").fetchone()
    db.insert_alert(conn, ts=NOW, venue_id="up-robinhood", pool_id=pid, kind="reward_rate_drop", level="warning",
                    message_ja="エポックの途中で報酬の毎秒量が 40% 減りました（最大値との比較）。", data={},
                    dedupe_key="k1")
    conn.commit()
    s = FakeSender()
    events.send_pending(conn, s, now=NOW)
    assert s.sent[0].startswith(f"⚠️ {pair}: ")


# --- 毎朝のレポートと /status -----------------------------------------------------------------

def test_daily_report_has_top5_changes_learning_and_is_made_once(setup):
    cfg, conn = setup
    tg = FakeTelegram()
    n = Notifier(cfg, tg, home=api.home, now=lambda: NOW + timedelta(minutes=5))
    assert n.daily_report() is True
    assert n.daily_report() is False            # 同じ日は1回だけ
    assert len(tg.sent) == 1
    body = tg.sent[0]
    assert body.startswith("☀️ Farm Radar 朝のレポート")
    assert "■ 純日利の上位" in body and "■ 判定の変化（24時間）" in body and "■ 今日の学び" in body
    row = conn.execute("SELECT * FROM daily_reports").fetchone()
    assert row["sent_at"] and row["body_ja"] == body
    note = conn.execute("SELECT * FROM learning_notes").fetchone()
    assert note["topic"] and note["title"] in body


def test_learning_topic_rotates(setup):
    cfg, conn = setup
    cands = [("a", "A", "x"), ("b", "B", "y")]
    assert report.pick_learning(conn, cands)[0] == "a"
    conn.execute("INSERT INTO learning_notes(ts, title, body_ja, topic) VALUES ('2026-01-01', 'A', 'x', 'a')")
    assert report.pick_learning(conn, cands)[0] == "b"


def test_report_is_kept_when_telegram_is_not_set(setup):
    cfg, conn = setup
    n = Notifier(cfg, None, home=api.home, now=lambda: NOW + timedelta(minutes=5))
    assert n.daily_report() is True
    assert conn.execute("SELECT sent_at FROM daily_reports").fetchone()[0] is None
    d = api.reports()
    assert d["reports"] and d["learning"] and d["schedule_jst"] == "08:00"
    assert "configured" in d["telegram"] and "token" not in json.dumps(d).lower()


def test_status_and_commands_only_for_owner(setup):
    cfg, conn = setup
    tg = FakeTelegram(updates=[
        {"update_id": 1, "message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "/status"}},
        {"update_id": 2, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "/status"}},
        {"update_id": 3, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "/exit_all"}},
        {"update_id": 4, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "hello"}},
    ])
    n = Notifier(cfg, tg, home=api.home, now=lambda: NOW + timedelta(minutes=5))
    assert n.poll_once() == 4
    assert len(tg.sent) == 3                        # オーナー以外（999）には返事をしない
    status, m5, help_ = tg.sent
    assert "観察モード" in status and "判定: 🟢" in status and "上位3件" in status
    assert "データ収集" in status and "報酬トークン" in status
    assert "練習モード" in m5 and "/status" in help_
