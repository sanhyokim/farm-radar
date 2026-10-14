"""10/14 の14日判定（2026-10-14 指示書「10/14の14日判定を軽量に実行する」）: 損の線だけを軽く見る。

- 新しい版（18001）の /api/trial/backtest（さかのぼり）を1回だけ読み、prep.loss（1日・1週間・始めてから）を並べる。
  写しを取り直したあとなので、ここでさかのぼりが計算し直される（数分かかることがある）。Merkl の答え合わせは計算しない。
- 重いAPI（フルの /api/trial/records・/api/trial/final・/api/trial/summary）は呼ばない。
  記録の確かめが要るときは /api/trial/records?light=1 だけ（light=1 を知っている版のときだけ。知らない版では呼ばない）。
- N6 は /api/n6 と /api/n6/health を読むだけ。N6 の設定や判断には触らない。
- 決めない・線を変えない・設定を変えない。分布と、今の線より悪かった割合を出すだけ（final_prep.loss_lines と同じ考え方）。
- パソコンの Python（標準の道具だけ）で動く:
  python n5_1014_report.py --api http://localhost:18001 --manifest <v2>\\data\\import\\old-18000.json --out <ファイル> --summary <JSON>
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from datetime import date, timedelta
from typing import Any

NEED_DAYS = 14                  # 1週間・始めてからの損の線に要る記録の日数（final_prep.WEEK_MIN_DAYS と同じ）
PCTS = (50, 75, 90, 95, 99)     # final_prep.PCTS と同じ
PERIODS = ("week", "since_start", "day")      # 今回の主目的は 1週間・始めてから。1日は比べるため
PERIOD_JA = {"day": "1日", "week": "1週間", "since_start": "始めてから"}
LEVELS = ("caution", "no_new", "stop")
LEVEL_JA = {"caution": "注意", "no_new": "新しく入らない", "stop": "全部止める"}
UNIT_JA = {"day": "プールと日の組", "week": "7日続いた窓", "since_start": "プール"}
# 指示書に書かれた今の線（パソコンの /api/n6 で読めたら、そちらを使う。違ったら注意を出す）
ORDER_LINES = {"week": {"caution": -7.0, "no_new": -10.0, "stop": -12.0},
               "since_start": {"caution": -10.0, "no_new": -15.0, "stop": -20.0},
               "day": {"caution": -3.0, "no_new": -8.5, "stop": -18.0}}
# 呼ばない重いAPI（試験で確かめる）
HEAVY = ("/api/trial/records", "/api/trial/final", "/api/trial/summary")


def _get(url: str, timeout: float) -> Any:
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    if any(("/" + path).split("?")[0] == h and "light=1" not in path for h in HEAVY):
        raise RuntimeError(f"重いAPIは呼びません: /{path}")
    with urllib.request.urlopen(url, timeout=timeout) as r:            # noqa: S310  自分のパソコンの API だけ
        return json.loads(r.read().decode("utf-8"))


def _get_waiting(url: str, timeout: float, say=print) -> Any:
    """長くかかる読み取り。止まって見えないよう、1分ごとに「まだ計算中」を出す。"""
    done = threading.Event()

    def tick() -> None:
        t0 = time.monotonic()
        while not done.wait(60):
            say(f"   まだ計算中です（{(time.monotonic() - t0) / 60:.0f} 分）。この画面を閉じないでください。")

    th = threading.Thread(target=tick, daemon=True)
    th.start()
    try:
        return _get(url, timeout)
    finally:
        done.set()


# --- 数える ----------------------------------------------------------------------------------------------

def q(xs: list[float], p: float) -> float | None:
    """final_prep.q と同じ（並べて、間をつなぐ）。"""
    if not xs:
        return None
    ys = sorted(xs)
    i = p * (len(ys) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)


def pct(v: float | None, nd: int = 2) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{nd}f}%"
    return s.replace("+0.00%", "0.00%").replace("-0.00%", "0.00%")


def share(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def period_stats(vals: list[float], lines: dict[str, float]) -> dict[str, Any]:
    """分布（50/75/90/95/99%点）と、今の線より悪かった割合。
    「90%点」= 10回のうち9回は、損がこれより小さかった（final_prep と同じ。悪いほうのはしから数える）。
    「線より悪かった」= その線と同じか、もっと悪かった（final_prep と同じ ≤）。"""
    vals = [float(v) for v in vals]
    return {"n": len(vals),
            "pcts": {str(p): q(vals, 1 - p / 100) for p in PCTS},
            "worst": min(vals) if vals else None,
            "lines": [{"level": lv, "line": float(lines[lv]),
                       "worse_share": (sum(1 for v in vals if v <= lines[lv]) / len(vals)) if vals else None,
                       "worse_n": sum(1 for v in vals if v <= lines[lv])} for lv in LEVELS]}


def coverage(days: list[str]) -> dict[str, Any]:
    """写しの中の実際の日付（さかのぼりで比べた日。UTC）。今日の日付からは決めない。"""
    ds = sorted({d for d in days if d})
    if not ds:
        return {"first": None, "last": None, "days": 0, "span": 0, "missing": []}
    a, b = date.fromisoformat(ds[0]), date.fromisoformat(ds[-1])
    have = set(ds)
    span = (b - a).days + 1
    missing = [(a + timedelta(days=i)).isoformat() for i in range(span) if (a + timedelta(days=i)).isoformat() not in have]
    return {"first": ds[0], "last": ds[-1], "days": len(ds), "span": span, "missing": missing}


def lines_from_n6(n6: dict[str, Any] | None) -> tuple[dict[str, dict[str, float]], list[str]]:
    """今の線: パソコンの /api/n6 の rules（N6 が使っている線）。読めなければ指示書の値。違えば注意を返す。"""
    rules = (n6 or {}).get("rules") or {}
    got = {"week": rules.get("loss_week"), "since_start": rules.get("loss_since_start"),
           "day": ((rules.get("new") or {}).get("loss_day"))}
    out, notes = {}, []
    for p in PERIODS:
        g = got.get(p)
        if isinstance(g, dict) and all(isinstance(g.get(lv), (int, float)) for lv in LEVELS):
            out[p] = {lv: float(g[lv]) for lv in LEVELS}
            if any(abs(out[p][lv] - ORDER_LINES[p][lv]) > 1e-9 for lv in LEVELS):
                notes.append(f"{PERIOD_JA[p]}の線がパソコンの設定（{' / '.join(f'{out[p][lv]:g}%' for lv in LEVELS)}）と"
                             f"指示書（{' / '.join(f'{ORDER_LINES[p][lv]:g}%' for lv in LEVELS)}）で違います。パソコンの設定で数えました。")
        else:
            out[p] = dict(ORDER_LINES[p])
            if n6 is not None:
                notes.append(f"{PERIOD_JA[p]}の線をパソコンの N6 から読めなかったので、指示書の値で数えました。")
    return out, notes


def n6_state(n6: dict[str, Any] | None, health: dict[str, Any] | None, n6_err: str | None,
             health_err: str | None) -> dict[str, Any]:
    """N6 の今の様子（読むだけ）。止める理由: 読めない・virtual が True でない・health の重大な異常。"""
    stop: list[str] = []
    notes: list[str] = []
    out: dict[str, Any] = {"read": n6 is not None}
    if n6 is None:
        stop.append(f"N6（/api/n6）を読めません（virtual=True を確かめられません）: {n6_err}")
    else:
        tick = n6.get("last_tick") or {}
        out.update({"enabled": n6.get("enabled"), "virtual": n6.get("virtual"), "last_tick_ok": tick.get("ok") if tick else None,
                    "last_tick_at": tick.get("ts") if tick else None, "ticks": n6.get("ticks")})
        if n6.get("virtual") is not True:
            stop.append(f"N6 が virtual=True ではありません（{n6.get('virtual')}）")
        if n6.get("enabled") is not True:
            notes.append(f"N6 の enabled が True ではありません（{n6.get('enabled')}）")
        if not tick or tick.get("ok") is not True:
            notes.append("N6 の最後の見回りが ok ではありません" + (f"（{tick.get('error')}）" if tick and tick.get("error") else ""))
        pfs = {p.get("id"): p for p in n6.get("portfolios") or []}
        for pid in ("app_1000", "app_10000"):
            p = pfs.get(pid)
            if p is None:
                notes.append(f"{pid} が見つかりません")
                out[pid] = None
                continue
            closed = len(p.get("closed") or [])
            out[pid] = {"status": p.get("status"), "open": len(p.get("open") or []), "closed": closed,
                        "closed_more": closed >= 30,          # /api/n6 は終わった建玉を新しい順に30件まで返す
                        "pnl_usd": p.get("pnl_usd"), "pnl_pct": p.get("pnl_pct")}
    if health is None:
        stop.append(f"N6 の異常のチェック（/api/n6/health）を読めません: {health_err}")
        out["health"] = None
    else:
        probs = [str(x) for x in health.get("problems") or []]
        if "serious" in health:            # PR #44 の版: 重大（記録の食い違い・本物のお金の言葉）だけを分けて返す
            serious = [str(x) for x in (health.get("serious") or [])] + \
                      [str(x) for x in (health.get("realmoney") or []) if str(x) not in (health.get("serious") or [])]
        else:                              # それより前の版: 見回りの回の問題（「見回り」を含む）以外を重大とみる
            serious = [x for x in probs if "見回り" not in x]
        out["health"] = {"ok": health.get("ok"), "problems": probs, "serious": serious, "light_ok": "serious" in health}
        if serious:
            stop.append("N6 health に重大な異常があります: " + " / ".join(serious[:5]))
        for x in probs:
            if x not in serious:
                notes.append(f"N6 health の注意（重大ではない）: {x}")
    out["stop"] = stop
    out["notes"] = notes
    return out


# --- まとめる ---------------------------------------------------------------------------------------------

def build(bt: dict[str, Any], manifest: dict[str, Any] | None, n6: dict[str, Any] | None, health: dict[str, Any] | None,
          n6_err: str | None = None, health_err: str | None = None, light: dict[str, Any] | None = None,
          light_note: str | None = None) -> dict[str, Any]:
    stop: list[str] = []
    notes: list[str] = []
    res = (bt or {}).get("result") or {}
    if not (bt or {}).get("present") or not res:
        stop.append("さかのぼり（/api/trial/backtest）を計算できませんでした: " + str((bt or {}).get("text") or "答えが空です"))
    key = (bt or {}).get("key") or {}
    copied = (manifest or {}).get("copied_at")
    used_new = bool(copied) and key.get("copied_at") == copied
    if manifest is not None and res and not used_new:
        stop.append(f"さかのぼりが、取り直した写し（{copied}）で計算されていません（{key.get('copied_at')}）")
    loss = ((res.get("prep") or {}).get("loss")) or {}
    cov = coverage(res.get("days") or [])
    cal = int(loss.get("calendar_days") or 0)
    ready = bool(res) and cov["days"] >= NEED_DAYS and cal >= NEED_DAYS and bool(loss.get("week")) and bool(loss.get("since_start"))
    if res and not ready:
        stop.append(f"14日分の記録が実際にはまだありません（記録のある日 {cov['days']} 日・損の線の日数 {cal} 日。{NEED_DAYS} 日が要ります）")
    lines, lnotes = lines_from_n6(n6)
    notes += lnotes
    periods = {p: period_stats(loss.get(p) or [], lines[p]) for p in PERIODS}
    n6s = n6_state(n6, health, n6_err, health_err)
    if light_note:
        notes.append(light_note)
    return {"ok": not stop and not n6s["stop"], "ready": ready, "stop": stop + n6s["stop"], "notes": notes + n6s["notes"],
            "copied_at": copied, "backtest_copied_at": key.get("copied_at"), "used_new_copy": used_new,
            "computed_at": (bt or {}).get("computed_at"), "backtest_version": res.get("version"),
            "coverage": cov, "calendar_days": cal, "pools": loss.get("pools"),
            "since_start_days_median": loss.get("since_start_days_median"),
            "lines": lines, "periods": periods, "n6": n6s,
            "light": {k: ((light or {}).get("feeds") or {}).get(k) for k in ("merkl_sums", "pool_history")} if light else None}


def _cov_line(s: dict[str, Any]) -> str:
    c = s["coverage"]
    if not c["first"]:
        return "記録: なし"
    miss = f"抜けた日 {len(c['missing'])} 日（{', '.join(c['missing'])}）" if c["missing"] else "抜けた日なし"
    return f"記録: {c['first']} 〜 {c['last']}（UTC） / 記録のある日 {c['days']} 日・暦の日数 {c['span']} 日 / {miss}"


def _n6_line(n: dict[str, Any]) -> list[str]:
    if not n.get("read"):
        return ["N6: 読めませんでした"]
    h = n.get("health") or {}
    bad = h.get("serious") or []
    probs = h.get("problems") or []

    def pf(pid: str) -> str:
        p = n.get(pid)
        if not p:
            return f"{pid} なし"
        more = "以上" if p["closed_more"] else ""
        return f"{pid} 開いている {p['open']} / 終わった {p['closed']}{more}"
    state = "要確認" if n["stop"] else ("正常（注意あり）" if n["notes"] else "正常")
    return [f"N6: {state} / enabled={n.get('enabled')} / virtual={n.get('virtual')} / last_tick.ok={n.get('last_tick_ok')} / "
            f"見回り {n.get('ticks')} 回",
            f"    {pf('app_1000')} / {pf('app_10000')}",
            "    health の異常: " + ("なし" if not probs else f"{len(probs)} 件（重大 {len(bad)} 件）")]


def display(s: dict[str, Any]) -> list[str]:
    """パソコンの画面のいちばん下に出す分（指示書 12）。写し・v1 の行は PowerShell が足す。"""
    c = s["coverage"]
    out = [f"記録: {c['days']}日（{c['first']} ～ {c['last']}）" + (f" / 抜けた日 {len(c['missing'])} 日" if c["missing"] else ""),
           "14日判定: " + ("できる" if s["ready"] else f"まだ（記録のある日 {c['days']} 日。{NEED_DAYS} 日が要ります）"), ""]
    for p in PERIODS:
        st = s["periods"][p]
        tag = "（比べるため）" if p == "day" else ""
        out.append(f"{PERIOD_JA[p]}{tag}: {UNIT_JA[p]} {st['n']} 件 / いちばん悪い {pct(st['worst'])}")
        for x in st["lines"]:
            out.append(f"  {LEVEL_JA[x['level']]} {x['line']:g}% → この線より悪かった {share(x['worse_share'])}（{x['worse_n']} 件）")
        out.append("  " + " / ".join(f"{p2}%点 {pct(st['pcts'][str(p2)])}" for p2 in PCTS))
        out.append("")
    out += _n6_line(s["n6"])
    return out


def report_text(s: dict[str, Any], v1_before: str = "") -> str:
    L: list[str] = ["10/14 の14日判定（損の線だけ）", "=" * 60,
                    "値の出どころ: あなたのパソコンで確かめた値（今の版 18000 の写しを、新しい版 18001 がさかのぼって計算したもの）。",
                    "決めていません。線も設定も変えていません。分布と、今の線より悪かった割合を出すだけです。",
                    "呼んだAPI: /api/trial/backtest・/api/n6・/api/n6/health（と、版が対応していれば /api/trial/records?light=1）。",
                    "呼ばなかった重いAPI: フルの /api/trial/records・/api/trial/final・/api/trial/summary（Merkl の答え合わせは計算していません）。",
                    ""]
    if v1_before:
        L += [f"今の版（写す前）: {v1_before}", ""]
    L += ["[写しと日付]",
          f"写した時刻（UTC）: {s['copied_at']} / さかのぼりが使った写し: {s['backtest_copied_at']}"
          f"（{'取り直した写しで計算' if s['used_new_copy'] else '一致しません'}）",
          f"さかのぼりの計算: {s['computed_at']}（計算の版 {s['backtest_version']}）",
          _cov_line(s),
          f"損の線の日数（暦）: {s['calendar_days']} 日 / プール {s['pools']} 個 / プールごとの記録の日数（中央値） {s['since_start_days_median']}",
          "14日判定: " + ("できる" if s["ready"] else "まだ"), ""]
    L += ["[損の線]",
          "作り: 今のやり方（その日のいちばん良い幅で置き直す）でプールを1つ持ったときの、総資産あたりの損益（%）。",
          "  1日 = プールと日の組ごと / 1週間 = 記録が7日続いた窓ごと / 始めてから = プールごとの、記録の最初からのいちばん悪いところ。",
          "  保険の資金調達料とボーナスのコインの値下がりは入っていません（final_prep・N5 の判定と同じ作り）。",
          "  「90%点」= 10回のうち9回は、損がこれより小さかった。「線より悪かった」= その線と同じか、もっと悪かった。", ""]
    for p in PERIODS:
        st = s["periods"][p]
        tag = "（比べるため。今回の再決定の主目的ではない）" if p == "day" else ""
        L.append(f"■ {PERIOD_JA[p]}{tag}: 記録件数 {st['n']}（{UNIT_JA[p]}） / いちばん悪い {pct(st['worst'])}")
        for p2 in PCTS:
            L.append(f"   {p2}%点: {pct(st['pcts'][str(p2)])}")
        for x in st["lines"]:
            L.append(f"   {LEVEL_JA[x['level']]} {x['line']:g}% より悪かった割合: {share(x['worse_share'])}（{x['worse_n']} / {st['n']} 件）")
        L.append("")
    L += ["[N6（読むだけ。設定や判断は変えていません）]"] + _n6_line(s["n6"])
    n = s["n6"]
    for pid in ("app_1000", "app_10000"):
        p = n.get(pid)
        if p:
            L.append(f"    {pid}: 状態 {p['status']} / 損益 {p['pnl_usd'] if p['pnl_usd'] is not None else '—'} ドル")
    for x in (n.get("health") or {}).get("problems") or []:
        L.append(f"    health: {x}")
    if s.get("light"):
        ms = (s["light"].get("merkl_sums") or {})
        L.append(f"    記録の確かめ（light=1）: Merkl の全部のページ complete=1 {ms.get('complete')} 件")
    L.append("")
    if s["notes"]:
        L += ["[注意]"] + [f"- {x}" for x in s["notes"]] + [""]
    if s["stop"]:
        L += ["[止める理由]"] + [f"- {x}" for x in s["stop"]] + [""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="10/14 の14日判定（損の線だけ。読むだけ）")
    ap.add_argument("--api", default="http://localhost:18001")
    ap.add_argument("--manifest", default=None, help="写しの確かめの記録（v2 の data/import/old-18000.json）")
    ap.add_argument("--out", required=True, help="レポート（文字）を書くファイル")
    ap.add_argument("--summary", required=True, help="まとめ（JSON）を書くファイル")
    ap.add_argument("--v1-before", default="", help="今の版の写す前の様子（レポートに書くだけ）")
    ap.add_argument("--timeout", type=float, default=3600.0, help="さかのぼりを待つ秒数")
    a = ap.parse_args(argv)
    manifest = None
    if a.manifest:
        try:
            with open(a.manifest, encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"STOP 写しの確かめの記録を読めません: {exc}")
            return 2
    print("   さかのぼりを計算しています（取り直した写しで。数分〜30分ほどかかることがあります）……", flush=True)
    t0 = time.monotonic()
    try:
        bt = _get_waiting(f"{a.api}/api/trial/backtest", a.timeout, lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"STOP さかのぼり（/api/trial/backtest）を計算できませんでした: {type(exc).__name__}: {exc}")
        return 2
    print(f"   さかのぼり: 終わりました（{(time.monotonic() - t0) / 60:.1f} 分）", flush=True)
    n6 = health = light = None
    n6_err = health_err = light_note = None
    try:
        n6 = _get(f"{a.api}/api/n6", 180)
    except Exception as exc:  # noqa: BLE001
        n6_err = f"{type(exc).__name__}: {exc}"
    try:
        health = _get(f"{a.api}/api/n6/health", 120)
    except Exception as exc:  # noqa: BLE001
        health_err = f"{type(exc).__name__}: {exc}"
    if health is not None and "serious" in health:          # light=1 を知っている版（PR #44）のときだけ
        try:
            light = _get(f"{a.api}/api/trial/records?light=1", 60)
        except Exception as exc:  # noqa: BLE001
            light_note = f"記録の確かめ（light=1）は読めませんでした（{type(exc).__name__}）。損の線には関係ありません。"
    else:
        light_note = "この版は記録の軽い確かめ（light=1）に対応していないので、呼びませんでした（重い計算を避けるため）。損の線には関係ありません。"
    s = build(bt, manifest, n6, health, n6_err, health_err, light, light_note)
    s["display"] = display(s)
    # PowerShell 7 は ISO の日時の文字を日時に変えてしまうので、画面に出す分は別の形で渡す
    s["copied_at_text"] = str(s["copied_at"] or "—").replace("T", " ").replace("+00:00", "") + "（UTC）"
    with open(a.out, "w", encoding="utf-8-sig", newline="\r\n") as f:      # メモ帳でも文字化けしないように BOM つき
        f.write(report_text(s, a.v1_before))
    with open(a.summary, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    print("RESULT " + ("OK" if s["ok"] else "STOP " + " / ".join(s["stop"])), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
