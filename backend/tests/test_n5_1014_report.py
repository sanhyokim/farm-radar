"""10/14 の14日判定（損の線だけ）の補助（n5_1014_report.py）。2026-10-14 指示書「10/14の14日判定を軽量に実行する」。"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest

from farm_radar import backtest, final_prep
from farm_radar import n5_1014_report as r


def _days(n: int, start: date = date(2026, 9, 30), skip: int | None = None) -> list[str]:
    out = [(start + timedelta(days=i)).isoformat() for i in range(n + (1 if skip is not None else 0))]
    if skip is not None:
        out.pop(skip)
    return out


def _bt(n_days: int, copied: str = "2026-10-14T05:00:00+00:00", skip: int | None = None) -> dict:
    week = [-1.0, -2.0, -8.0, -11.0, -13.0, 0.5, -0.5, -3.0, -6.0, -7.0]
    since = [-2.0, -10.0, -16.0, -21.0, -5.0]
    day = [-0.1, -3.0, -9.0, 0.2, -1.0, -19.0, -2.0, -0.5]
    return {"present": True, "key": {"copied_at": copied}, "computed_at": "2026-10-14T05:10:00+00:00",
            "result": {"version": 9, "days": _days(n_days, skip=skip),
                       "prep": {"loss": {"day": day, "week": week if n_days >= 7 else [], "since_start": since,
                                         "pools": 5, "since_start_days_median": float(n_days), "calendar_days": n_days}}}}


def _n6(virtual=True, closed=1, lines=None) -> dict:
    rules = {"new": {"loss_day": {"caution": -3.0, "no_new": -8.5, "stop": -18.0}},
             "loss_week": {"caution": -7.0, "no_new": -10.0, "stop": -12.0},
             "loss_since_start": {"caution": -10.0, "no_new": -15.0, "stop": -20.0}}
    rules.update(lines or {})
    pf = lambda pid: {"id": pid, "status": "running", "open": [{"id": 9}], "closed": [{"id": 1}] * closed,  # noqa: E731
                      "pnl_usd": -1.0, "pnl_pct": -0.1}
    return {"enabled": True, "virtual": virtual, "last_tick": {"ts": "2026-10-14T05:00:00+00:00", "ok": True},
            "ticks": 620, "rules": rules, "portfolios": [pf("app_1000"), pf("app_10000")]}


HEALTH_OK = {"ok": True, "problems": [], "serious": [], "realmoney": []}
MAN = {"copied_at": "2026-10-14T05:00:00+00:00"}


def test_14_days_ready_and_numbers():
    s = r.build(_bt(14), MAN, _n6(), HEALTH_OK)
    assert s["ok"] and s["ready"] and not s["stop"]
    assert s["coverage"] == {"first": "2026-09-30", "last": "2026-10-13", "days": 14, "span": 14, "missing": []}
    w = s["periods"]["week"]
    assert w["n"] == 10 and w["worst"] == -13.0
    by = {x["level"]: x for x in w["lines"]}
    assert by["caution"]["line"] == -7.0 and by["caution"]["worse_n"] == 4          # -8 -11 -13 -7（≤ は final_prep と同じ）
    assert by["no_new"]["worse_n"] == 2 and by["stop"]["worse_n"] == 1
    assert by["stop"]["worse_share"] == pytest.approx(0.1)
    st = s["periods"]["since_start"]
    assert [x["worse_n"] for x in st["lines"]] == [3, 2, 1]                         # -10 -16 -21 / -16 -21 / -21
    d = s["periods"]["day"]
    assert [x["line"] for x in d["lines"]] == [-3.0, -8.5, -18.0]
    assert [x["worse_n"] for x in d["lines"]] == [3, 2, 1]
    # 分布は final_prep と同じ数え方（悪いほうのはしから。90%点 = 10回のうち9回はこれより小さい損）
    for p in r.PCTS:
        assert w["pcts"][str(p)] == pytest.approx(final_prep.q([float(x) for x in _bt(14)["result"]["prep"]["loss"]["week"]],
                                                               1 - p / 100))
    text = "\n".join(r.display(s))
    assert "14日判定: できる" in text and "記録: 14日（2026-09-30 ～ 2026-10-13）" in text
    assert "注意 -7% → この線より悪かった 40.0%（4 件）" in text
    assert "N6: 正常" in text and "virtual=True" in text and "見回り 620 回" in text


def test_13_days_is_not_ready():
    s = r.build(_bt(13), MAN, _n6(), HEALTH_OK)
    assert not s["ready"] and not s["ok"]
    assert any("14日分の記録が実際にはまだありません" in x for x in s["stop"])
    assert "14日判定: まだ（記録のある日 13 日。14 日が要ります）" in r.display(s)


def test_days_come_from_the_copy_not_from_today():
    # 暦の上では 15 日の幅でも、記録のある日が 14 日に足りなければ「まだ」。抜けた日を出す
    s = r.build(_bt(13, skip=5), MAN, _n6(), HEALTH_OK)
    assert s["coverage"]["span"] == 14 and s["coverage"]["days"] == 13
    assert s["coverage"]["missing"] == ["2026-10-05"] and not s["ready"]
    assert "抜けた日 1 日" in r.display(s)[0]


def test_backtest_on_old_copy_is_a_stop():
    s = r.build(_bt(15, copied="2026-10-08T01:00:00+00:00"), MAN, _n6(), HEALTH_OK)
    assert not s["ok"] and any("取り直した写し" in x for x in s["stop"])
    s = r.build({"present": False, "text": "写しがない"}, MAN, _n6(), HEALTH_OK)
    assert not s["ok"] and any("さかのぼり" in x for x in s["stop"])


def test_n6_virtual_and_serious_health_stop():
    s = r.build(_bt(15), MAN, _n6(virtual=False), HEALTH_OK)
    assert not s["ok"] and s["ready"] and any("virtual=True ではありません" in x for x in s["stop"])
    bad = {"ok": False, "problems": ["app_1000: 置いた額 $1,200.00 が総額 $1,000 をこえている"],
           "serious": ["app_1000: 置いた額 $1,200.00 が総額 $1,000 をこえている"], "realmoney": []}
    s = r.build(_bt(15), MAN, _n6(), bad)
    assert not s["ok"] and any("重大な異常" in x for x in s["stop"])
    s = r.build(_bt(15), MAN, None, HEALTH_OK, n6_err="timeout")
    assert not s["ok"] and any("読めません" in x for x in s["stop"])


def test_tick_only_problem_is_a_note_also_on_older_version():
    tick = "N6 の見回りが 40 分ありません（30 分より長い）"
    for h in ({"ok": False, "problems": [tick], "serious": [], "realmoney": []},     # PR #44 の版
              {"ok": False, "problems": [tick]}):                                    # それより前の版（serious なし）
        s = r.build(_bt(15), MAN, _n6(), h)
        assert s["ok"], s["stop"]
        assert any(tick in x for x in s["notes"])
    s = r.build(_bt(15), MAN, _n6(), {"ok": False, "problems": ["対 5: 本体の建玉がない"]})
    assert not s["ok"]                                                               # 前の版でも、見回り以外は重大とみる


def test_lines_come_from_the_pc_and_differences_are_noted():
    s = r.build(_bt(15), MAN, _n6(lines={"loss_week": {"caution": -6.0, "no_new": -10.0, "stop": -12.0}}), HEALTH_OK)
    assert s["lines"]["week"]["caution"] == -6.0
    assert any("指示書" in x and "1週間" in x for x in s["notes"])
    s = r.build(_bt(15), MAN, None, HEALTH_OK, n6_err="x")
    assert s["lines"] == r.ORDER_LINES


def test_closed_count_marks_the_30_cap():
    s = r.build(_bt(15), MAN, _n6(closed=30), HEALTH_OK)
    assert s["n6"]["app_1000"]["closed_more"] and "終わった 30以上" in "\n".join(r.display(s))


def test_works_with_the_real_loss_series_shape():
    rows = [{"pool_id": f"p{i % 3}", "day": d, "now": (-1) ** j * 2.0 * (j % 5)}
            for i in range(3) for j, d in enumerate(_days(16))]
    loss = backtest.loss_series(rows, 1000.0)
    bt = {"present": True, "key": {"copied_at": MAN["copied_at"]},
          "result": {"version": backtest.VERSION, "days": _days(16), "prep": {"loss": loss}}}
    s = r.build(bt, MAN, _n6(), HEALTH_OK)
    assert s["ready"] and s["periods"]["week"]["n"] == len(loss["week"]) > 0
    assert s["periods"]["since_start"]["n"] == loss["pools"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_api(monkeypatch, health, calls):
    def urlopen(url, timeout=None):
        calls.append(url.split("18001", 1)[1])
        path = calls[-1]
        if path.startswith("/api/trial/backtest"):
            body = _bt(15)
        elif path.startswith("/api/n6/health"):
            body = health
        elif path.startswith("/api/n6"):
            body = _n6()
        elif path == "/api/trial/records?light=1":
            body = {"feeds": {"light": True, "merkl_sums": {"complete": 7}}}
        else:
            raise AssertionError(f"呼んではいけないAPI: {path}")
        return _Resp(json.dumps(body).encode())
    monkeypatch.setattr(r.urllib.request, "urlopen", urlopen)


def test_main_calls_only_light_apis(monkeypatch, tmp_path):
    calls: list[str] = []
    _fake_api(monkeypatch, HEALTH_OK, calls)
    man = tmp_path / "old-18000.json"
    man.write_text(json.dumps(MAN), encoding="utf-8")
    out, summ = tmp_path / "rep.txt", tmp_path / "s.json"
    assert r.main(["--api", "http://localhost:18001", "--manifest", str(man), "--out", str(out), "--summary", str(summ)]) == 0
    assert calls == ["/api/trial/backtest", "/api/n6", "/api/n6/health", "/api/trial/records?light=1"]
    for heavy in ("/api/trial/final", "/api/trial/summary"):
        assert not any(c.startswith(heavy) for c in calls)
    assert "/api/trial/records" not in calls
    s = json.loads(summ.read_text(encoding="utf-8"))
    assert s["ok"] and s["light"]["merkl_sums"]["complete"] == 7
    assert s["copied_at_text"] == "2026-10-14 05:00:00（UTC）"
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                                           # メモ帳でも文字化けしない
    assert "■ 1週間" in raw.decode("utf-8-sig") and "全部止める -12%" in raw.decode("utf-8-sig")


def test_main_skips_light_on_versions_that_do_not_know_it(monkeypatch, tmp_path):
    calls: list[str] = []
    _fake_api(monkeypatch, {"ok": True, "problems": []}, calls)                     # serious がない = light=1 を知らない版
    man = tmp_path / "old-18000.json"
    man.write_text(json.dumps(MAN), encoding="utf-8")
    assert r.main(["--manifest", str(man), "--out", str(tmp_path / "a.txt"), "--summary", str(tmp_path / "b.json")]) == 0
    assert not any(c.startswith("/api/trial/records") for c in calls)


def test_heavy_apis_are_refused_even_by_mistake():
    for path in ("/api/trial/records", "/api/trial/final", "/api/trial/summary", "/api/trial/records?x=1"):
        with pytest.raises(RuntimeError):
            r._get("http://localhost:18001" + path, 1)


def test_backtest_failure_stops(monkeypatch, tmp_path):
    def urlopen(url, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(r.urllib.request, "urlopen", urlopen)
    assert r.main(["--out", str(tmp_path / "a.txt"), "--summary", str(tmp_path / "b.json")]) == 2
    assert not (tmp_path / "b.json").exists()
