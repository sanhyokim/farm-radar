"""ボーナスが減ったときの比べ方（2026-09-30 オーナー決定③。SPEC 8.5章）。

練習中のプールのボーナス（今実際に出ている毎秒量）が、木曜の切り替えのあとで前の週の半分以下（0を含む）になったら、
次の切り替えまでの見込みで次の3つを比べ、損がいちばん少ないものと理由を記録して知らせる。
1. そのまま（今の形で持ち続ける）
2. ステークをやめて手数料をもらう（ガス代1回）
3. 抜ける（閉じる費用: 両替・ガス・perp の手数料。閉じたあとは0）

今は記録と知らせだけ（config.yaml の risk.bonus_drop_action: record）。建玉は閉じたり切り替えたりしない。
評価のあとで実際に動かすかは、記録を見てオーナーが決める。

切り替えの直後は、今週のボーナスがまだ配られていない（毎秒量が0）ことがあるので、切り替えの印が消えて、
ボーナスが出るか risk.bonus_drop_wait_hours たってから比べる。比べる数字は、切り替えのあとに計算したスコアを使う。
1つの建玉につき1週1回（建玉の state の bonus_checked に、比べ終わった週の始まりを書く）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..config import Config, ConfigError, load_venue
from ..views import JST, next_epoch_flip
from .base import PositionRef
from .flip import nearest_range, per_day_for_mode, score_inputs
from .paper import WEEKDAY_JA, PaperError, PaperExecutor
from .views import OPTION_JA

log = logging.getLogger(__name__)



def _jst(t: datetime) -> str:
    j = t.astimezone(JST)
    return f"{j.month}/{j.day}({WEEKDAY_JA[j.weekday()]}) {j:%H:%M}"


def _next_flip(config: Config, venue_id: str, now: datetime, snap: sqlite3.Row) -> datetime | None:
    """次の切り替えの時刻。スナップショットのエポックの終わりがあればそれ、なければ会場ファイルの周期から。"""
    if snap["epoch_end"]:
        t = datetime.fromisoformat(snap["epoch_end"])
        if t > now:
            return t
    try:
        epoch = (load_venue(venue_id, config.root).get("mechanics") or {}).get("epoch") or {}
    except (OSError, ConfigError):
        return None
    length = int(epoch.get("length_seconds") or 0)
    return next_epoch_flip(now, length, int(epoch.get("offset_seconds") or 0)) if length else None


def _mark(conn: sqlite3.Connection, pid: int, **fields: Any) -> None:
    """建玉の state に書く（見張りが書いた risk_active などは消さないよう、読み直してから足す）。"""
    row = conn.execute("SELECT state_json FROM positions WHERE id=?", (pid,)).fetchone()
    st = json.loads(row["state_json"] or "{}")
    st.update(fields)
    conn.execute("UPDATE positions SET state_json=? WHERE id=?", (json.dumps(st), pid))
    conn.commit()


def compare(conn: sqlite3.Connection, config: Config, ex: PaperExecutor, pos: sqlite3.Row,
            now: datetime) -> dict[str, Any] | None:
    """ボーナスが減っていたら3つを比べた結果を返す（まだ比べる時でなければ None）。書き込みはしない。

    返り値の "state":
    - "wait": まだ比べない（切り替えの直後・スコアがまだ古い）
    - "none": 減っていない、または比べる必要がない（この週はもう調べなくてよい）
    - "drop": 減っていた。options と best がある
    """
    snap = conn.execute("""SELECT * FROM pool_snapshots WHERE pool_id=? AND price IS NOT NULL
                           ORDER BY ts DESC LIMIT 1""", (pos["pool_id"],)).fetchone()
    if snap is None or not snap["epoch_start"] or snap["reward_rate_effective_raw"] is None:
        return None
    epoch = snap["epoch_start"]
    st = json.loads(pos["state_json"] or "{}")
    if st.get("bonus_checked") == epoch:
        return None
    # 建玉を始めたのが今の週なら、持っている間に切り替えはまだない
    if datetime.fromisoformat(pos["opened_at"]) >= datetime.fromisoformat(epoch):
        return None
    if pos["mode"] == "unstaked":
        # ステークしていない建玉は手数料で稼いでいるので、ボーナスが減っても影響しない
        return {"state": "none", "epoch_start": epoch}
    prev = conn.execute("""SELECT reward_rate_effective_raw FROM pool_snapshots
                           WHERE pool_id=? AND epoch_start<? AND reward_rate_effective_raw IS NOT NULL
                             AND COALESCE(epoch_just_flipped, 0)=0 ORDER BY ts DESC LIMIT 1""",
                        (pos["pool_id"], epoch)).fetchone()
    if prev is None or int(prev[0]) <= 0:
        return {"state": "none", "epoch_start": epoch}
    if snap["epoch_just_flipped"]:
        return {"state": "wait", "epoch_start": epoch}
    prev_rate, cur_rate = int(prev[0]), int(snap["reward_rate_effective_raw"])
    since_flip_h = (datetime.fromisoformat(snap["ts"]) - datetime.fromisoformat(epoch)).total_seconds() / 3600
    s = config.risk
    if cur_rate == 0 and since_flip_h < s.bonus_drop_wait_hours:
        return {"state": "wait", "epoch_start": epoch}          # まだ配られていないだけかもしれない
    ratio = cur_rate / prev_rate
    if ratio > s.bonus_drop_ratio:
        return {"state": "none", "epoch_start": epoch, "ratio": ratio}
    # 比べる数字は、切り替えのあと（ボーナスが変わったあと）に計算したスコアで
    score = conn.execute("""SELECT * FROM scores WHERE pool_id=? AND ts>=? AND COALESCE(epoch_just_flipped, 0)=0
                            ORDER BY ts DESC LIMIT 1""", (pos["pool_id"], snap["ts"])).fetchone()
    if score is None:
        score = conn.execute("""SELECT * FROM scores WHERE pool_id=? AND ts>? AND COALESCE(epoch_just_flipped, 0)=0
                                ORDER BY ts DESC LIMIT 1""", (pos["pool_id"], epoch)).fetchone()
    if score is None:
        return {"state": "wait", "epoch_start": epoch}
    details, inp = score_inputs(score)
    row = nearest_range(details, pos["r"] * 100)
    flip = _next_flip(config, pos["venue_id"], now, snap)
    if row is None or flip is None:
        return {"state": "wait", "epoch_start": epoch}
    hours = max(0.0, (flip - now).total_seconds() / 3600)
    trend = inp.get("reward_token_trend_daily")
    gas = float(inp.get("gas_usd_per_tx") or 0.0)
    stay = per_day_for_mode(row, pos["mode"], trend)
    options: dict[str, dict[str, Any]] = {}
    if stay is not None:
        options["stay"] = {"usd": stay["net"] * hours / 24, "per_day": stay["net"], "income_day": stay["income"]}
    fees = per_day_for_mode(row, "unstaked", trend) if pos["mode"] == "staked" else None
    if fees is not None:
        options["fees"] = {"usd": fees["net"] * hours / 24 - gas, "per_day": fees["net"], "income_day": fees["income"],
                           "switch_cost": gas}
    try:
        close_cost = ex.estimate_close_cost(PositionRef(pos["id"]))
    except PaperError:
        close_cost = None
    if close_cost is not None:
        options["exit"] = {"usd": -close_cost, "per_day": 0.0, "close_cost": close_cost}
    if not options:
        return {"state": "wait", "epoch_start": epoch}
    best = max(options, key=lambda k: options[k]["usd"])
    return {"state": "drop", "epoch_start": epoch, "prev_rate": str(prev_rate), "cur_rate": str(cur_rate),
            "ratio": ratio, "hours_left": hours, "next_flip": flip.astimezone(UTC).isoformat(timespec="seconds"),
            "score_ts": score["ts"], "options": options, "best": best}


def message_ja(pair: str, r: dict[str, Any]) -> str:
    parts = "／".join(f"{OPTION_JA[k]} {_usd(v['usd'])}" for k, v in r["options"].items())
    pct = r["ratio"] * 100
    drop = "0になりました" if r["cur_rate"] == "0" else f"前の週の{pct:.0f}%に減りました"
    return (f"{pair}: 木曜の切り替えでボーナスが{drop}。次の切り替え（{_jst(datetime.fromisoformat(r['next_flip']))}）"
            f"までの見込みは、{parts}。いちばん損が少ないのは「{OPTION_JA[r['best']]}」です。"
            "今は記録と知らせだけで、建玉はそのままです。")


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def run(conn: sqlite3.Connection, config: Config, ex: PaperExecutor, now: datetime | None = None) -> list[int]:
    """持っている建玉を調べて、ボーナスが減っていたら記録と通知をする。記録した risk_events の id を返す。"""
    from .risk_job import record_event        # risk_job からも呼ぶので、ここで読む

    if config.mode != "paper":
        return []
    now = now or datetime.now(UTC)
    out = []
    for pos in conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='open' ORDER BY id").fetchall():
        try:
            r = compare(conn, config, ex, pos, now)
        except (PaperError, ValueError, KeyError) as exc:
            log.warning("bonus drop check failed", extra={"data": {"position": pos["id"], "error": str(exc)}})
            continue
        if r is None or r["state"] == "wait":
            continue
        if r["state"] == "drop":
            p = conn.execute("SELECT token0_symbol, token1_symbol FROM pools WHERE id=?", (pos["pool_id"],)).fetchone()
            pair = f"{p['token0_symbol']}/{p['token1_symbol']}" if p else pos["pool_id"]
            eid = record_event(conn, now, pos["id"], "caution", "bonus_drop", message_ja(pair, r), "none",
                               {**r, "action_mode": config.risk.bonus_drop_action}, pos["venue_id"], pos["pool_id"])
            out.append(eid)
            _mark(conn, pos["id"], bonus_checked=r["epoch_start"],
                  bonus_drop={"at": now.astimezone(UTC).isoformat(timespec="seconds"), "event": eid,
                              "best": r["best"], "options": r["options"], "ratio": r["ratio"],
                              "next_flip": r["next_flip"]})
        else:
            _mark(conn, pos["id"], bonus_checked=r["epoch_start"])
    if out:
        log.info("bonus drop recorded", extra={"data": {"events": out}})
    return out
