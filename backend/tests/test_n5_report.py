"""N5 最終判定レポート（n5_report.py）。決めない・記録が足りなければ案を出さない・パソコンの Python だけで動く。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from farm_radar import n5_report as r


def _bt(days: int) -> dict:
    ds = [f"2026-09-{28 + i:02d}" if 28 + i <= 30 else f"2026-10-{28 + i - 30:02d}" for i in range(days)]
    ranges = [{"r_pct": p, "rebalances": {"pred_median": pred, "real_median": real}}
              for p, pred, real in ((0.5, 36.0, 11.0), (1.0, 9.0, 5.0), (2.0, 2.25, 1.5), (15.0, 0.04, 0.0))]
    grid = [{"buffer": b, "wait_min": w, "rebalances_day": 1.0, "flips_day": 0.1 * (b == 0), "cost_usd_day": 1.5,
             "out_share": 0.05, "net_usd_day": -5.0 + b * 10 + w / 100, "calendar_days": days, "pool_days": 10}
            for b in (0, 0.05, 0.10, 0.15) for w in (15, 30, 60)]
    st1 = [{"threshold_pct": t, "all": {"count": 1}, "big": {"count": 0}} for t in (20, 30, 40, 50)]
    return {"present": True, "computed_at": "2026-10-08T12:00:00+00:00",
            "result": {"days": ds, "ranges": {"all": ranges}, "prep": {"grid": {"rows": grid}, "stage1": st1}}}


def _state(code: str, why: str = "") -> dict:
    return {"code": code, "label": r.STATE[code], "why": why}


def _final(pairs: int = 40, days: float = 3.5, coin_pairs: int = 5) -> dict:
    items = [
        {"key": "stage1", "state": _state("ready"),
         "tables": [{"title": "線ごとの回数とそのあと", "head": ["線", "回数", "大きいプール", "6時間で戻った", "24時間でさらに悪化",
                                                     "空振りらしい"],
                     "rows": [["−20%", "1", "0", "—", "—", "—"], ["−30%", "1", "0", "—", "—", "—"]], "mark": [1]}]},
        {"key": "stage2", "state": _state("ready"),
         "tables": [{"title": "値段の合図", "head": ["合図", "回数", "根拠"], "rows": [["投げ売り 1時間 -15%", "2", "少ない"],
                                                                         ["狙い以下", "427", "数はある"]]}]},
        {"key": "stage4", "state": _state("ready"),
         "tables": [{"title": "倍率ごとの結果", "head": ["倍率", "移った回数"], "rows": [["1倍", "3"], ["2倍", "1"]], "mark": [1]}]},
        {"key": "edge", "state": _state("ready"),
         "tables": [{"title": "余裕 × 待ち時間（1日あたり）", "head": [], "rows": [["0%（今の置き直し）"]] + [["x"]] * 11, "mark": [0]}]},
        {"key": "loss_lines", "state": _state("wait", "14日"), "tables": []},
        {"key": "hedge_rise", "state": _state("ready"),
         "tables": [{"title": "市場", "head": ["市場", "本体の上げ", "RH版の上げ", "耐える上げ幅", "決めているもの"],
                     "rows": [["NBIS", "+85.2%", "—", "85.2%", "市場の上げ"], ["AAPL", "+8%", "—", "50.0%", "床（50%）"],
                              ["SNDK", "+58.4%", "+57.7%", "58.4%", "市場の上げ"]]}]},
        {"key": "risk", "title": "危なさの点数", "state": _state("lack", "材料なし")},
    ]
    merkl = {
        "pairs": pairs, "days": days,
        "table": {"rows": [["A（全員が分母。今の式）", "-5.0%", "-20.0%", "+8.0%", "70%"],
                           ["B（幅の中だけが分母）", "+1.0%", "-9.0%", "+12.0%", "82%"]]},
        "tables": [
            {"title": "重みの種類で分けた差（まん中）", "head": ["キャンペーン", "A", "B", "A ±30%", "B ±30%", "組"],
             "rows": [["手数料が中心", "-4%", "+1%", "72%", "85%", str(pairs - coin_pairs)],
                      ["コインが中心", "-30%", "+2%", "40%", "60%", str(coin_pairs)]]},
            {"title": "キャンペーンごと", "head": [],
             "rows": [["A/B", "幅の中だけ", "50/25/25", "20"], ["C/D", "幅の中だけ", "60/20/20", "10"],
                      ["E/F", "幅の中だけ", "50/25/25", "5"], ["G/H", "全員", "20/40/40", str(coin_pairs)]]},
            {"title": "5% 上限の準備: 自分の額が分母の何%か",
             "head": ["組み合わせ", "B÷A", "$100 A", "$100 B", "$3,000 A", "$3,000 B"],
             "rows": [["A/B", "1.50", "0.10%", "0.15%", "3.00%", "4.50%"], ["C/D", "2.00", "0.20%", "0.40%", "4.00%", "8.00%"]]},
            {"title": "使わなかった区切り・預け方と理由", "head": ["理由", "数"],
             "rows": [["区切り: プールの預け方の歴史をまだ読み終えていない", "7"], ["区切り: チェーンの記録を区切りの終わりまで読めていない", "3"],
                      ["預け方: 名前の形が違う", "11"]]},
        ]}
    return {"items": items, "merkl": merkl, "n6": [{"title": "預かり証 月−3%", "why": "練習の記録"}]}


def _records(full_days: float = 3.2) -> dict:
    to = f"2026-10-0{4 + int(full_days)}T{int((full_days % 1) * 24):02d}:00:00+00:00"
    return {"old_copy": {"copied_at": "2026-10-08T03:00:00+00:00", "checked": True},
            "feeds": {"merkl_sums": {"complete": 50, "from": "2026-10-04T00:00:00+00:00", "to": to},
                      "pool_history": {"targets": 23, "done": 20, "partial": 1, "resumable": 1, "not_started": 2,
                                       "events": 1000, "errors": [{"pool": "x", "error": "読む量が多すぎる（…）"}],
                                       "last_run": {"ts": "t", "calls": 150, "stopped": ""}}}}


def test_seven_days_gives_candidates_and_marks_main_ranges():
    rep = r.build(_final(), _bt(7), _records())
    sec = rep["sections"][0]
    assert sec["state"] == "ready" and rep["summary"]["seven_day_ready"]
    rows = sec["tables"][0]["rows"]
    assert rows[0][0] == "★±0.5%" and rows[1][0] == "±1%" and rows[2][0] == "★±2%" and rows[3][0] == "★±15%"
    assert rows[0][6] == "今の式 × 0.31" and rows[0][3] == "-25.00"


def test_under_seven_days_no_candidate():
    rep = r.build(_final(), _bt(6), _records())
    sec = rep["sections"][0]
    assert sec["state"] == "wait" and not rep["summary"]["seven_day_ready"]
    assert all(row[6] == "（7日たまってから）" for row in sec["tables"][0]["rows"])
    assert rep["summary"]["fourteen_day_from"] == "2026-10-11"


def test_edge_top3_and_current_marks():
    sec = r.build(_final(), _bt(7), _records())["sections"][1]
    grid, top = sec["tables"]
    assert grid["rows"][0][0] == "★0%（今の置き直し）" and len(grid["rows"]) == 12
    assert [t[1] for t in top["rows"]] == ["余裕 15%・待ち 60分", "余裕 15%・待ち 30分", "余裕 15%・待ち 15分"]
    assert "おすすめではありません" in sec["why"]


def test_stage1_stage2_stage4_labels():
    secs = r.build(_final(), _bt(7), _records())["sections"]
    s1, s2, s4 = secs[2], secs[3], secs[4]
    assert s1["tables"][0]["head"][2] == "5万ドル以上のプール" and s1["tables"][0]["rows"][1][0] == "★−30%"
    assert "件数不足" in s1["lines"][0]
    assert s2["tables"][0]["rows"][0][2] == "件数不足" and s2["tables"][0]["rows"][1][2] == "足りる"
    assert s4["tables"][0]["rows"][1][0] == "★2倍"


def test_merkl_ready_shows_ab_and_weight_types():
    rep = r.build(_final(), _bt(7), _records(3.2))
    ab = next(s for s in rep["sections"] if s["no"] == "M")
    assert ab["state"] == "ready" and rep["summary"]["merkl_ab_ready"]
    assert "読めていない区切りは外した（10 件）" in ab["tables"][0]["rows"][3][2]
    main = ab["tables"][1]["rows"]
    assert main[0][:5] == ["A（全員が分母。今の式）", "-5.0%", "-20.0%", "+8.0%", "70%"] and main[0][5] == "40"
    types = {row[0]: row for row in ab["tables"][2]["rows"]}
    assert types["手数料が中心"][-1] == "判断できる" and types["手数料が中心"][6] == "3"
    assert types["コインが中心"][-1] == "この種類はまだ判断できない"


def test_merkl_waits_until_full_page_records_are_three_days():
    rep = r.build(_final(), _bt(7), _records(2.5))
    ab = next(s for s in rep["sections"] if s["no"] == "M")
    assert ab["state"] == "wait" and len(ab["tables"]) == 1          # 判断用の数字は出さない
    assert ab["tables"][0]["rows"][0][1] == "いいえ"
    rep = r.build(_final(pairs=29), _bt(7), _records(3.2))
    assert next(s for s in rep["sections"] if s["no"] == "M")["state"] == "wait"


def test_cap_counts_and_does_not_decide():
    rep = r.build(_final(), _bt(7), _records(3.2))
    cap = next(s for s in rep["sections"] if s["no"] == "5%")
    counts = {(row[0], row[1]): row[2:] for row in cap["tables"][0]["rows"]}
    assert counts[("$100", "A")] == ["2", "0", "0"]
    assert counts[("$3,000", "A")] == ["0", "2", "0"] and counts[("$3,000", "B")] == ["0", "1", "1"]
    assert "C/D $3,000（B: 8.00%）" in cap["lines"][0] and cap["state"] == "ready"


def test_pool_history_and_hedge_and_waiting():
    rep = r.build(_final(), _bt(7), _records())
    ph = next(s for s in rep["sections"] if s["no"] == "歴")
    vals = dict(ph["tables"][0]["rows"])
    assert vals["読み終わり"] == "20" and vals["読まないと決めたもの"] == "1" and vals["エラー"] == "0" and ph["state"] == "wait"
    hd = dict(next(s for s in rep["sections"] if s["no"] == "保")["tables"][0]["rows"])
    assert hd["市場"] == "3" and hd["50% の床で決まる市場"] == "1" and hd["50% を超える市場"] == "2"
    assert hd["耐える上げ幅のいちばん大きい値"] == "85.2%" and hd["上位の市場"].startswith("NBIS 85.2%、SNDK 58.4%")
    w = rep["summary"]["waiting"]
    assert any(x.startswith("危なさの点数") for x in w) and any("N6 で判断" in x for x in w)
    assert not any(x.startswith("預け方の歴史") for x in w)
    text = r.render(rep)
    assert "# N5 最終判定レポート" in text and "決めません" in text


def test_main_writes_files_and_stops_on_fetch_error(tmp_path, monkeypatch):
    answers = {"/api/trial/backtest": _bt(10), "/api/trial/final": _final(), "/api/trial/records": _records()}
    monkeypatch.setattr(r, "_get", lambda url, timeout: answers[url.split("18001")[1]])
    out, summ = tmp_path / "r.txt", tmp_path / "s.json"
    assert r.main(["--out", str(out), "--summary", str(summ)]) == 0
    s = json.loads(summ.read_text(encoding="utf-8"))
    assert s["backtest_days"] == 10 and s["seven_day_ready"] and not s["fourteen_day_ready"]
    assert "① 置き直しの回数 — 判断できる" in out.read_text(encoding="utf-8")

    def boom(url, timeout):
        raise OSError("refused")
    monkeypatch.setattr(r, "_get", boom)
    assert r.main(["--out", str(tmp_path / "x.txt")]) == 2 and not (tmp_path / "x.txt").exists()


def test_report_runs_on_plain_python():
    """パソコンの Python でそのまま動く: 標準の道具だけを読み、アプリの中身も、お金を動かすものも読まない。"""
    src = Path(r.__file__).read_text(encoding="utf-8")
    mods = {(n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
            for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert mods <= {"__future__", "argparse", "json", "re", "statistics", "sys", "urllib", "datetime", "typing"}


def test_1008_script_only_copies_and_reports():
    """10/8 の1行: 写しとレポートだけ。Docker・更新・削除はしない。道具はコミットの印で固定して受け取る。"""
    raw = (Path(__file__).resolve().parents[2] / "scripts/pc/n5-1008.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # Windows PowerShell 5.1 が日本語を読めるように
    text = raw.decode("utf-8-sig")
    code = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    for bad in ("docker", "Remove-Item", "Move-Item", "Rename-Item", "Stop-Process", "compose"):
        assert bad not in code, bad
    assert "$RawBase/$Sha/$path" in code and "'^[0-9a-f]{7,40}$'" in code
    assert "scripts/pc/copy-18000.ps1" in code and "backend/farm_radar/n5_report.py" in code
    for word in ("写し: 成功", "v1（今の版）: そのまま", "記録日数", "Merkl A/B の条件", "7日の判定", "判断待ち・記録待ち", "止めました"):
        assert word in code, word
