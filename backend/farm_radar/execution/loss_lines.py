"""損の線（N4b。SPEC 13.1 の追加の決定 10）。練習の建玉全体の損を、3つの期間 × 3つの段階で見る。

- 期間: 今日（日本時間の0時から）・7日・練習を始めてから。オーナーが「再開」を押したら、どの期間もそこから数え直す
  （止まったあとに再開しても、同じ損ですぐまた止まらないように）。
- 段階: 注意（記録と表示）・新しく入らない（新しい練習を始めない）・すべて止める（全部閉じて、新しく始めるのも止める）。
- 損は position_pnl の行の合計。まだ売っていないボーナスのコインの値下がり（今売った値段）・保険の損益・移る費用とガス代も入る。
- 線を越えたときは、どの損で越えたのか、内訳（プールの値動き・ボーナスのコイン・保険・費用・収入）を記録と画面に出す。
- 割合の分母は、今置いている額（合否に使う建玉）。何も置いていなければ、その期間に動いた建玉の額。
- 参考の練習（2026-10-01 案B）は入れない（今までどおり、参考の練習だけの1日の損で閉じる）。
読み取りと計算だけ。お金を動かすコードはない。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import LOSS_LEVELS, LOSS_PERIODS, GuardSettings
from ..views import JST
from .paper import official_sql

PERIOD_JA = {"day": "1日", "week": "1週間", "since_start": "始めてから"}
LEVEL_JA = {"caution": "注意", "no_new": "新しく入らない", "stop": "すべて止める"}
PART_JA = {"pool": "プールの値動き", "bonus": "ボーナスのコイン", "hedge": "保険", "costs": "費用", "income": "収入"}
ACTIVE_KEY = "loss_active"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def practice_start(conn: sqlite3.Connection) -> str | None:
    """「始めてから」の始まり: オーナーが最後に再開した時刻。なければ、合否に使う最初の建玉を開いた時刻。"""
    row = conn.execute("SELECT MAX(ts) FROM risk_events WHERE kind='owner_resume'").fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(f"SELECT MIN(opened_at) FROM positions WHERE is_paper=1 AND {official_sql()}").fetchone()
    return row[0] if row and row[0] else None


def period_since(conn: sqlite3.Connection, period: str, now: datetime) -> str | None:
    start = practice_start(conn)
    if period == "day":
        t = _iso(now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0))
    elif period == "week":
        t = _iso(now - timedelta(days=7))
    else:
        return start
    resumed = conn.execute("SELECT MAX(ts) FROM risk_events WHERE kind='owner_resume'").fetchone()
    return max(t, resumed[0]) if resumed and resumed[0] else t


def breakdown(conn: sqlite3.Connection, since: str) -> dict[str, float]:
    """since から今までの損益の内訳（合否に使う建玉。ドル）。"""
    r = conn.execute(
        "SELECT SUM(n.income), SUM(n.direction) + SUM(n.gamma), SUM(n.haircut), SUM(n.hedge), SUM(n.other), SUM(n.net) "
        "FROM position_pnl n JOIN positions p ON p.id = n.position_id "
        f"WHERE p.is_paper=1 AND {official_sql('p.')} AND n.ts >= ?", (since,)).fetchone()
    vals = [float(x or 0.0) for x in r]
    return {"income": vals[0], "pool": vals[1], "bonus": vals[2], "hedge": vals[3], "costs": vals[4], "net": vals[5]}


def base_capital(conn: sqlite3.Connection, since: str) -> float:
    row = conn.execute(f"SELECT SUM(capital) FROM positions WHERE is_paper=1 AND status='open' AND {official_sql()}"
                       ).fetchone()
    if row and row[0]:
        return float(row[0])
    row = conn.execute(
        f"SELECT SUM(capital) FROM positions WHERE is_paper=1 AND {official_sql()} AND id IN "
        "(SELECT DISTINCT position_id FROM position_pnl WHERE ts >= ?)", (since,)).fetchone()
    return float(row[0] or 0.0)


def _level(pct: float | None, line: dict[str, float]) -> str | None:
    if pct is None:
        return None
    hit = None
    for k in LOSS_LEVELS:
        if pct <= line[k]:
            hit = k
    return hit


def main_cause(parts: dict[str, float]) -> str | None:
    """いちばん大きく減らした損（マイナスの中で一番大きいもの）。なければ None。"""
    neg = {k: v for k, v in parts.items() if k in PART_JA and k != "income" and v < 0}
    return min(neg, key=lambda k: neg[k]) if neg else None


def status(conn: sqlite3.Connection, g: GuardSettings, now: datetime) -> dict[str, Any]:
    """3つの期間それぞれの今の損と、越えている段階。level はいちばん強い段階（なければ None）。"""
    periods = []
    worst: tuple[int, str, str] | None = None
    for p in LOSS_PERIODS:
        line = g.loss_lines[p]
        since = period_since(conn, p, now)
        parts = breakdown(conn, since) if since else None
        base = base_capital(conn, since) if since else 0.0
        net = parts["net"] if parts else 0.0
        pct = net / base * 100 if base > 0 and parts else None
        level = _level(pct, line)
        if level is not None:
            rank = LOSS_LEVELS.index(level)
            if worst is None or rank > worst[0]:
                worst = (rank, level, p)
        periods.append({
            "period": p, "label": PERIOD_JA[p], "since": since, "base_usd": base, "net_usd": net, "pct": pct,
            "level": level, "level_ja": LEVEL_JA.get(level or ""),
            "lines": {k: {"pct": line[k], "usd": base * line[k] / 100 if base else None, "label": LEVEL_JA[k]}
                      for k in LOSS_LEVELS},
            "breakdown": parts, "main_cause": main_cause(parts) if parts else None,
        })
    return {"periods": periods, "level": worst[1] if worst else None, "level_ja": LEVEL_JA.get(worst[1]) if worst else None,
            "period": worst[2] if worst else None, "provisional": True,
            "note": "数字は仮です（N5 の試しで決め直します）。損には、まだ売っていないボーナスのコインの値下がり・保険の損益・費用も入ります。"}


def parts_ja(parts: dict[str, float]) -> str:
    return "、".join(f"{PART_JA[k]} {_usd(parts[k])}" for k in ("pool", "bonus", "hedge", "costs", "income")
                     if abs(parts.get(k, 0.0)) >= 0.005)


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def message_ja(row: dict[str, Any], level: str) -> str:
    line = row["lines"][level]
    cause = row.get("main_cause")
    text = (f"損の線（{row['label']} {line['pct']:g}%・{LEVEL_JA[level]}）を越えました: "
            f"{row['label']}の損益 {_usd(row['net_usd'])}（置いている額 ${row['base_usd']:,.0f} の{row['pct']:+.1f}%）。")
    if row.get("breakdown"):
        text += f"内訳は {parts_ja(row['breakdown'])}。"
    if cause:
        text += f"いちばん大きいのは「{PART_JA[cause]}」です。"
    return text


def no_new_reason(conn: sqlite3.Connection, g: GuardSettings, now: datetime) -> str | None:
    """「新しく入らない」以上の線を越えていれば、新しい練習を始めない理由（日本語）。越えていなければ None。"""
    st = status(conn, g, now)
    for row in st["periods"]:
        if row["level"] in ("no_new", "stop"):
            return message_ja(row, row["level"]) + "線より戻るまで、新しい練習は始めません（数字は仮）。"
    return None


def active(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value_json FROM guard_state WHERE key=?", (ACTIVE_KEY,)).fetchone()
    return set(json.loads(row[0] or "[]")) if row else set()


def set_active(conn: sqlite3.Connection, keys: set[str], now: datetime) -> None:
    conn.execute("INSERT INTO guard_state(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value_json=excluded.value_json, updated_at=excluded.updated_at",
                 (ACTIVE_KEY, json.dumps(sorted(keys)), _iso(now)))
    conn.commit()
