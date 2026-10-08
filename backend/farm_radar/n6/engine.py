"""N6 の15分ごとの回（2026-10-07 指示書）。100% 仮想。本物のお金・署名・秘密鍵は使わない。

練習のまとまり（n6_portfolios）:
  app_1000 / app_10000  … アプリ任せ（探すの候補から自分で選ぶ。オーナーを待たずに始める）
  own_1000 / own_10000  … 自分で選ぶ（「探す」のボタンで申し込んだときに始まる）
建玉ごとに、保険あり／なしの反対側を「対」として同じ額・同じ時刻・同じ幅で並べる（総額・上限・損の線には数えない）。

1回にすること: 申し込みを入れる → 建玉を進める（sim.step）→ 4つの段階 → ボーナスを売ったとしたら →
損の線 → アプリ任せは空いたお金で入れる → 記録（n6_marks・n6_portfolio_marks・n6_events）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import app_settings, riskscore
from .. import opportunities as opps
from ..config import LOSS_LEVELS, Config
from ..db import database as db
from ..feeds.store import JST
from ..standard import _feeds_conn
from . import sim
from .market import Market, rules_config

log = logging.getLogger(__name__)

KINDS = ("pool_range", "pool_full", "hold")
LEVEL_RANK = {lv: i for i, lv in enumerate(riskscore.LEVELS)}
PICKER_JA = {"app": "アプリ任せ", "owner": "自分で選ぶ"}
REASONS_JA = {"profit": "利益確定", "worry": "不安", "move": "別の場所へ移る", "test": "テスト", "other": "その他"}
LEVEL_JA = {"caution": "注意", "no_new": "新しく入らない", "stop": "すべて止める"}
PERIOD_JA = {"day": "今日（日本時間）", "week": "7日", "since_start": "始めてから"}
HEDGE_KEYS = ("book", "market_id", "rh_market_id", "symbol", "mmf", "imf", "taker_pct")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _t(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def pf_id(picker: str, total: float) -> str:
    return f"{'app' if picker == 'app' else 'own'}_{total:g}"


# --- 記録 --------------------------------------------------------------------------------------------

def event(conn: sqlite3.Connection, now: datetime, pf: str | None, pos: int | None, action: str, msg: str,
          rule: str | None = None, stage: int | None = None, data: Any = None, shadow: bool = False,
          opp_key: str | None = None) -> None:
    conn.execute("INSERT INTO n6_events(ts, portfolio_id, position_id, opp_key, action, rule, stage, shadow, message_ja, "
                 "data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (_iso(now), pf, pos, opp_key, action, rule, stage, int(shadow), msg,
                  json.dumps(data, ensure_ascii=False, default=str) if data is not None else None))


def ensure_portfolio(conn: sqlite3.Connection, picker: str, total: float, now: datetime) -> str:
    pid = pf_id(picker, total)
    if conn.execute("SELECT 1 FROM n6_portfolios WHERE id=?", (pid,)).fetchone() is None:
        conn.execute("INSERT INTO n6_portfolios(id, picker, total_usd, started_at, status, cash_usd, state_json) "
                     "VALUES (?,?,?,?,?,?,?)", (pid, picker, total, _iso(now), "running", total, "{}"))
        event(conn, now, pid, None, "start", f"{PICKER_JA[picker]}の ${total:,.0f} の練習を始めた（仮想のお金）")
    return pid


def _pf_state(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["state_json"] or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _save_pf_state(conn: sqlite3.Connection, pid: str, st: dict[str, Any]) -> None:
    conn.execute("UPDATE n6_portfolios SET state_json=? WHERE id=?", (json.dumps(st, ensure_ascii=False), pid))


# --- 見込み ----------------------------------------------------------------------------------------

def variant(o: Any, amount: float, hedge: bool | None, case: str = "cautious") -> Any:
    """入れる先の見込み（N6 の額の段）。hedge None = 良い方。"""
    if o is None:
        return None
    row = (o.calc.get(opps._akey(amount)) or {}).get(case) or {}
    if hedge is None:
        vs = [v for v in row.values() if v is not None]
        return max(vs, key=lambda v: v.net_after_move) if vs else None
    return row.get("hedge" if hedge else "no_hedge")


def est_of(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    return {"apr_pct": v.apr_pct, "net_day": v.net, "net_after_move": v.net_after_move, "income_day": v.income,
            "move_cost": v.move_cost, "rebalances_per_day": v.rebalances_per_day, "in_range_ratio": v.in_range_ratio,
            "range_pct": v.range_pct, "hedge": v.hedge, "split": v.split, "hedge_cost_day": v.hedge_cost,
            "gamma_day": v.gamma, "rebalance_day": v.rebalance}


def _hedge_markets(mk: Market, o: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for hm in o.hedge_markets or []:
        p = mk.data.perp(hm.get("coin"), chain_id=o.base.evm_chain_id)
        if p is not None:
            out[str(hm.get("coin") or "").upper()] = {k: p.get(k) for k in HEDGE_KEYS}
    return out


class Danger:
    """入れる先の危なさ（探すと同じ。1回の中で覚えておく）。"""

    def __init__(self, target: float, ref_amount: float):
        self.target = target
        self.ref = ref_amount
        self._c: dict[str, dict[str, Any]] = {}

    def of(self, o: Any) -> dict[str, Any]:
        k = o.base.key
        if k not in self._c:
            try:
                self._c[k] = o.to_dict(self.ref, self.target).get("safety") or {}
            except Exception:  # noqa: BLE001  危なさが出せない入れる先は「とても高い」として扱う
                self._c[k] = {"level": "very_high", "label": riskscore.LABEL["very_high"]}
        return self._c[k]


# --- 上限（練習だけの上限。本番の limits は変えない） ----------------------------------------------

def _open_mains(conn: sqlite3.Connection, pid: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM n6_positions WHERE portfolio_id=? AND status='open' AND twin_of IS NULL",
                        (pid,)).fetchall()


def room(config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row], o: Any, danger: dict[str, Any],
         cash_extra: float = 0.0, skip_id: int | None = None) -> tuple[float, str]:
    """この入れる先に入れてよい額（建玉の額）と、いちばん効いた上限の名前。"""
    usd, why, _code = _room(config, pf, mains, o, danger, cash_extra, skip_id)
    return usd, why


def _room(config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row], o: Any, danger: dict[str, Any],
          cash_extra: float = 0.0, skip_id: int | None = None) -> tuple[float, str, str]:
    """room と同じ。いちばん効いた上限の記号（DROP_JA のキー）も返す（見回りの内訳の記録のため）。"""
    n6 = config.n6
    total = float(pf["total_usd"])
    small = total < riskscore.SMALL_CAPITAL_USD
    level = danger.get("level") or "very_high"
    rows = [r for r in mains if r["id"] != skip_id]
    caps = [(float(pf["cash_usd"]) + cash_extra, "置いていないお金", "cash")]
    rec = riskscore.recommend(level, {"total_usd": total, "per_venue_share": None if small else n6.per_venue_share},
                              None)
    caps.append((rec["usd"] or 0.0, f"危なさ {riskscore.LABEL.get(level, level)} の上限（資金の {rec['pct']:g}%）", "danger_cap"))
    venue_cap = total if small else n6.per_venue_share * total
    used = sum(float(r["amount_usd"]) for r in rows if r["venue"] == o.base.venue)
    caps.append((venue_cap - used, f"1つの会場の上限（{'小資金なので全部' if small else f'{n6.per_venue_share * 100:g}%'}）",
                 "venue_cap"))
    if level in ("high", "very_high") and not small:
        hi = 0.0
        for r in rows:
            e = json.loads(r["entry_json"] or "{}")
            if e.get("danger") in ("high", "very_high"):
                hi += float(r["amount_usd"])
        caps.append((total * riskscore.HIGH_TOTAL_PCT / 100 - hi, f"危なさが高い場所の合計（{riskscore.HIGH_TOTAL_PCT:g}%）",
                     "high_total"))
    usd, why, code = min(caps, key=lambda c: c[0])
    return max(0.0, usd), why, code


def lighter_room(config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row], skip_id: int | None = None) -> float:
    """保険の預け金（Lighter）に置いてよい残り。Lighter も1つの会場として数える。"""
    total = float(pf["total_usd"])
    if total < riskscore.SMALL_CAPITAL_USD:
        return total
    used = 0.0
    for r in mains:
        if r["id"] == skip_id or not r["hedge"]:
            continue
        used += float((json.loads(r["entry_json"] or "{}").get("split") or {}).get("hedge_margin") or 0.0)
    return config.n6.per_venue_share * total - used


def fit(config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row], o: Any, danger: dict[str, Any], hedge: bool | None,
        amount: float | None = None, cash_extra: float = 0.0, skip_id: int | None = None
        ) -> tuple[float | None, Any, str]:
    """上限に収まるいちばん大きい額の段と、その見込み（控えめ）。収まらなければ (None, None, 理由)。

    プールに置く分は、プールの預かり額の 5%（max_pool_share）まで（指示書 13）。保険の預け金は Lighter の上限まで。"""
    a, v, why, _code = _fit(config, pf, mains, o, danger, hedge, amount, cash_extra, skip_id)
    return a, v, why


def _fit(config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row], o: Any, danger: dict[str, Any], hedge: bool | None,
         amount: float | None = None, cash_extra: float = 0.0, skip_id: int | None = None
         ) -> tuple[float | None, Any, str, str]:
    """fit と同じ。収まらなかったときに効いた上限の記号（DROP_JA のキー）も返す。"""
    cap, why, code = _room(config, pf, mains, o, danger, cash_extra, skip_id)
    lroom = lighter_room(config, pf, mains, skip_id)
    ladder = sorted(config.n6.amounts_usd, reverse=True)
    if amount is not None:
        ladder = [amount]
    reason = f"入れられる額 ${cap:,.0f}（{why}）"
    tried = hit = False
    for a in ladder:
        if a > cap + 1e-9:
            continue
        tried = True
        cands = []
        for h in ((True, False) if hedge is None else (hedge,)):
            v = variant(o, a, h)
            if v is None:
                continue
            if o.cap_usd is not None and v.split.get("pool", 0.0) > o.cap_usd + 1e-9:
                reason = f"プールに置く分 ${v.split.get('pool', 0):,.0f} がプールの {config.opportunities.max_pool_share * 100:g}% " \
                         f"（${o.cap_usd:,.0f}）をこえる"
                code, hit = "pool_5pct", True
                continue
            if v.split.get("hedge_margin", 0.0) > lroom + 1e-9:
                reason = f"保険の預け金 ${v.split.get('hedge_margin', 0):,.0f} が Lighter の上限の残り ${lroom:,.0f} をこえる"
                code, hit = "lighter", True
                continue
            cands.append(v)
        if cands:
            return a, max(cands, key=lambda v: v.net_after_move), why, code
    if tried and not hit:
        code = "no_variant"
    return None, None, reason, code


def _blocked(conn: sqlite3.Connection, pid: str, key: str, now: datetime, hours: float) -> str | None:
    since = _iso(now - timedelta(hours=hours))
    r = conn.execute("SELECT closed_at FROM n6_positions WHERE portfolio_id=? AND opp_key=? AND twin_of IS NULL "
                     "AND status='closed' AND closed_at > ? ORDER BY closed_at DESC LIMIT 1", (pid, key, since)).fetchone()
    if r:
        return f"{hours:g}時間以内に出たところ（行ったり来たりを防ぐ）"
    r = conn.execute("SELECT ts FROM n6_events WHERE opp_key=? AND rule='pool_funds_drop' AND ts > ? LIMIT 1",
                     (key, since)).fetchone()
    if r:
        return "プールのお金が1時間で大きく減った（新しく入るのを止めている）"
    return None


def candidates(conn: sqlite3.Connection, mk: Market, config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row],
               danger: Danger, target: float, now: datetime, cash_extra: float = 0.0,
               skip_id: int | None = None) -> list[dict[str, Any]]:
    """アプリが選べる入れる先（指示書 17）。本当に残る利回り（控えめ・入る出る費用のあと）が狙い以上、
    危なさは「高い」まで、5%・会場・危なさ・Lighter の上限、ガス代の予備（見込みの分け方に入っている）、保険の量。"""
    held = {r["opp_key"] for r in mains if r["id"] != skip_id}
    out = []
    max_rank = LEVEL_RANK[config.n6.max_danger]
    for o in mk.ops.values():
        if not o.computable or o.excluded or o.uncertain_venue or o.kind not in KINDS or o.base.key in held:
            continue
        b = o.best(config.n6.amounts_usd[0])
        if b is None:
            continue
        d = danger.of(o)
        if LEVEL_RANK.get(d.get("level") or "very_high", 99) > max_rank:
            continue
        if _blocked(conn, pf["id"], o.base.key, now, config.n6.reenter_block_hours):
            continue
        a, v, why = fit(config, pf, mains, o, d, None, cash_extra=cash_extra, skip_id=skip_id)
        if a is None or v is None or v.apr_pct < target:
            continue
        out.append({"op": o, "amount": a, "variant": v, "danger": d, "cap_why": why})
    out.sort(key=lambda c: -c["variant"].apr_pct)
    return out


# --- 見回りの内訳（記録だけ。入る・出るの判断には使わない） ------------------------------------------------

# 入れなかった理由の記号と日本語。candidates と同じ順に調べる
DROP_JA = {
    "not_computable": "計算できない", "excluded": "外す印がある", "venue_uncertain": "会場の見分けが不確か",
    "kind": "種類が対象外（貸し出しなど）", "held": "もう入っている", "below_target": "狙い利回りに届かない",
    "danger": "危なさが上限をこえる", "blocked": "出たばかり・新しく入るのを止めている",
    "cash": "置いていないお金が足りない", "danger_cap": "危なさごとの1か所の上限", "venue_cap": "1つの会場の上限",
    "high_total": "危なさ「高い」の合計の上限", "pool_5pct": "プールの5%上限", "lighter": "保険の預け金（Lighter の上限）",
    "no_variant": "この額で計算できる形がない", "below_target_at_amount": "入れられる額では狙い利回りに届かない",
    "ok": "入れられる",
}
CAP_CODES = ("cash", "danger_cap", "venue_cap", "high_total", "pool_5pct", "lighter", "no_variant", "below_target_at_amount")


def _reach_apr(o: Any, amounts: list[float]) -> float | None:
    """額の段のどれかで出せるいちばん高い控えめの年利（上限を考えない）。狙いに届く入れる先かを数えるため。"""
    aprs = [b.apr_pct for a in amounts if (b := o.best(a)) is not None]
    return max(aprs) if aprs else None


def funnel(conn: sqlite3.Connection, mk: Market, config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row],
           danger: Danger, target: float, now: datetime) -> dict[str, Any]:
    """15分ごとの回の「なぜ入らなかったか」の内訳（指示書 2026-10-08 の 4・5・7）。

    candidates と同じ順・同じ判断で、入れる先ごとに最初に引っかかった理由を1つ数える（読むだけ。何も書かない）。
    final は candidates の数と同じになる。狙いに届く（reach）= 計算できる入れる先で、額の段のどれかで控えめの年利が
    狙い以上（上限・印・会場の見分けを考えない）。reach_dropped はそのうち入れなかった理由の数。
    best_miss（いちばん惜しかった）は、印・会場の見分け・種類で外れていないものの中で、年利がいちばん高い入れなかった先。"""
    held = {r["opp_key"] for r in mains}
    max_rank = LEVEL_RANK[config.n6.max_danger]
    amounts = list(config.n6.amounts_usd)
    counts: dict[str, int] = {}
    reach_codes: dict[str, str] = {}      # 狙いに届く入れる先ごとの理由（$1,000 と $10,000 の差を比べるため）
    reach_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for o in mk.ops.values():
        k = o.base.key
        if not o.computable:
            code = "not_computable"
        elif o.excluded:
            code = "excluded"
        elif o.uncertain_venue:
            code = "venue_uncertain"
        elif o.kind not in KINDS:
            code = "kind"
        elif k in held:
            code = "held"
        else:
            code = None
        reach = _reach_apr(o, amounts) if o.computable else None
        why = ""
        fit_apr: float | None = None
        d: dict[str, Any] = {}
        if code is None:
            if o.best(amounts[0]) is None:
                code = "not_computable"
            else:
                d = danger.of(o)
                blocked = None
                if LEVEL_RANK.get(d.get("level") or "very_high", 99) > max_rank:
                    code = "danger"
                elif (blocked := _blocked(conn, pf["id"], k, now, config.n6.reenter_block_hours)):
                    code, why = "blocked", blocked
                else:
                    a, v, why, cap_code = _fit(config, pf, mains, o, d, None)
                    if a is None or v is None:
                        code = cap_code
                    else:
                        fit_apr = v.apr_pct
                        if v.apr_pct < target:
                            code = "below_target" if reach is None or reach < target else "below_target_at_amount"
                            why = f"${a:,.0f} では {v.apr_pct:.1f}%"
                        else:
                            code = "ok"
                if code in ("danger", "blocked") or code in CAP_CODES:
                    # 狙いに届かない入れる先は、ほかの理由より「狙いに届かない」を先に数える（指示書の数え方）
                    if reach is None or reach < target:
                        code = "below_target"
        counts[code] = counts.get(code, 0) + 1
        if reach is not None and reach >= target:
            reach_codes[k] = code
            reach_rows.append({"key": k, "name": o.base.name, "venue": o.base.venue_name or o.base.venue, "code": code,
                               "apr_pct": round(reach, 2), "why": why or None})
        if code not in ("not_computable", "excluded", "venue_uncertain", "kind", "held") and reach is not None:
            apr = fit_apr if fit_apr is not None else reach
            rows.append({"key": k, "name": o.base.name, "venue": o.base.venue_name or o.base.venue,
                         "chain": o.base.chain, "chain_name": o.base.chain_name, "apr_pct": round(apr, 2),
                         "reach_apr_pct": round(reach, 2), "danger": d.get("level"), "danger_label": d.get("label"),
                         "code": code, "reason": DROP_JA.get(code, code) + (f"（{why}）" if why and code != "ok" else ""),
                         "gap_pt": round(target - apr, 2)})
    reach_n = len(reach_codes)
    misses = sorted((r for r in rows if r["code"] != "ok"), key=lambda r: (-r["apr_pct"], r["key"]))
    return {
        "total": len(mk.ops), "target": target, "counts": counts,
        "usable": sum(n for c, n in counts.items() if c not in ("not_computable", "excluded", "venue_uncertain", "kind")),
        "reach": reach_n,
        "reach_dropped": {c: n for c, n in _count(reach_codes.values()).items() if c != "ok"},
        "final": counts.get("ok", 0),
        "reach_rows": reach_rows,
        "best_miss": misses[0] if misses else None,
        "near": misses[:5],
    }


def _count(xs: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def pick_reason(c: dict[str, Any], target: float) -> str:
    v, o, d = c["variant"], c["op"], c["danger"]
    parts = [f"控えめの見込みの年利 {v.apr_pct:.1f}%（狙い {target:g}% 以上・入る出る費用のあと）",
             f"危なさ {d.get('label') or d.get('level')}",
             f"会場 {o.base.venue_name or o.base.venue}",
             f"${c['amount']:,.0f}（{c['cap_why']}）",
             "保険あり（売りで値動きを消す方が残る額が多い）" if v.hedge else "保険なし（こちらの方が残る額が多い）"]
    if o.cap_usd is not None:
        parts.append(f"プールに置く分 ${v.split.get('pool', 0):,.0f} はプールの 5% ${o.cap_usd:,.0f} 以内")
    return "・".join(parts)


# --- 始める・閉じる ---------------------------------------------------------------------------------

def open_position(conn: sqlite3.Connection, mk: Market, config: Config, pid: str, o: Any, amount: float, v: Any,
                  now: datetime, reason: str, danger: dict[str, Any], target: float, move_from: int | None = None,
                  twin_of: int | None = None, r_force: float | None = None) -> int:
    old = variant(mk.ops_old.get(o.base.key), amount, v.hedge)
    r = (r_force if r_force is not None else (v.range_pct or config.opportunities.merkl_range_pct) / 100) \
        if o.kind == "pool_range" else None
    est = {"new": est_of(v), "old": est_of(old), "target": target, "danger": danger.get("level"),
           "danger_label": danger.get("label"), "bonus_usd_per_day": o.base.bonus_usd_per_day, "tvl_usd": o.base.tvl_usd,
           "days_left": o.base.days_left, "denominators": o.denominators, "split": v.split, "cap_usd": o.cap_usd}
    st = sim.open_state(mk=mk, op=o, variant=v, r=r, hedge_markets=_hedge_markets(mk, o) if v.hedge else {},
                        now=now, est=est)
    st["denoms"] = {str(d["campaign_id"]): d["in_range_share"] for d in o.denominators if d.get("use") == "B"}
    st["started_below_target"] = v.apr_pct < target
    if "old" in st and old is not None:
        st["old"]["est_per_day"] = old.rebalances_per_day
    entry = {"danger": danger.get("level"), "split": v.split, "range_pct": (r or 0) * 100 if r else None,
             "geo": st["geo"], "price_src": st["price_src"], "est_new": est["new"], "est_old": est["old"],
             "denominators": o.denominators, "flags": [f.code for f in o.flags]}
    nums = sim.value(st)
    cur = conn.execute(
        """INSERT INTO n6_positions(portfolio_id, twin_of, opp_key, name, pair, chain, venue, kind, hedge, amount_usd,
           opened_at, status, est_apr_pct, est_old_apr_pct, pick_reason, move_from, entry_json, state_json, last_ts,
           value_usd, pnl_usd, rebalances) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, twin_of, o.base.key, o.base.name, "/".join(t["sym"] or "?" for t in st["tokens"]), o.base.chain,
         o.base.venue, o.kind, int(bool(v.hedge)), amount, _iso(now), "open", v.apr_pct, old.apr_pct if old else None,
         reason, move_from, json.dumps(entry, ensure_ascii=False, default=str),
         json.dumps(st, ensure_ascii=False, default=str), _iso(now), nums["value"], nums["value"] - amount, 0))
    pos_id = int(cur.lastrowid)
    if twin_of is None:
        conn.execute("UPDATE n6_portfolios SET cash_usd = cash_usd - ? WHERE id=?", (amount, pid))
        event(conn, now, pid, pos_id, "enter", f"入った: {o.base.name}（${amount:,.0f}・{'保険あり' if v.hedge else '保険なし'}）。{reason}",
              rule="move" if move_from else "pick", stage=4 if move_from else None, opp_key=o.base.key,
              data={"est_apr_pct": v.apr_pct, "est_old_apr_pct": old.apr_pct if old else None})
        other = variant(o, amount, not v.hedge)
        if other is not None:
            try:
                open_position(conn, mk, config, pid, o, amount, other, now,
                              f"対（{'保険なし' if v.hedge else '保険あり'}）: 本体と同じ額・同じ時刻・同じ幅で比べる",
                              danger, target, twin_of=pos_id, r_force=r)
            except sim.SimError as exc:
                event(conn, now, pid, pos_id, "info", f"対（保険あり／なしの反対側）は作れなかった: {exc}", opp_key=o.base.key)
    return pos_id


def _numbers_sql(nums: dict[str, float]) -> tuple[str, list[Any]]:
    cols = ["value_usd", "bonus_usd", "fees_usd", "price_move_usd", "rebalance_cost_usd", "hedge_usd", "hedge_cost_usd",
            "funding_usd", "gas_usd", "entry_exit_cost_usd", "max_drawdown_usd", "rebalances"]
    return ", ".join(f"{c}=?" for c in cols), [nums[c] for c in cols]


def close_position(conn: sqlite3.Connection, pos: sqlite3.Row, now: datetime, rule: str, reason_ja: str,
                   stage: int | None = None, move_reason: str | None = None, st: dict[str, Any] | None = None
                   ) -> dict[str, Any] | None:
    """建玉を閉じる（仮想）。出る費用を引いた額を、置いていないお金に戻す。対も一緒に閉じる。
    ほかで先に閉じていたら None（15分ごとの回と「出る」ボタンが重なったとき）。"""
    st = st if st is not None else json.loads(pos["state_json"])
    nums = sim.close_numbers(st)
    amount = float(pos["amount_usd"])
    nums["pnl_usd"] = nums["value_usd"] - amount
    sets, vals = _numbers_sql(nums)
    cur = conn.execute(f"UPDATE n6_positions SET status='closed', closed_at=?, exit_reason=?, exit_rule=?, exit_stage=?, "
                       f"move_reason=?, state_json=?, last_ts=?, pnl_usd=?, {sets} WHERE id=? AND status='open'",
                       (_iso(now), reason_ja, rule, stage, move_reason, json.dumps(st, ensure_ascii=False, default=str),
                        _iso(now), nums["pnl_usd"], *vals, pos["id"]))
    if cur.rowcount == 0:
        return None
    if pos["twin_of"] is None:
        conn.execute("UPDATE n6_portfolios SET cash_usd = cash_usd + ? WHERE id=?", (nums["value_usd"], pos["portfolio_id"]))
        event(conn, now, pos["portfolio_id"], pos["id"], "exit", f"出た: {pos['name']}。{reason_ja}。損益 "
              f"${nums['pnl_usd']:+,.2f}（出る費用 ${st['costs']['exit_swap'] + st['costs']['exit_gas'] + st['costs']['exit_taker'] + st['costs']['exit_bonus']:,.2f} のあと）",
              rule=rule, stage=stage, opp_key=pos["opp_key"], data=nums)
        for tw in conn.execute("SELECT * FROM n6_positions WHERE twin_of=? AND status='open'", (pos["id"],)).fetchall():
            close_position(conn, tw, now, "main_closed", "本体が閉じたので、対も同じ時刻に閉じた")
    return nums


# --- 4つの段階 --------------------------------------------------------------------------------------

def _mark_ago(conn: sqlite3.Connection, pos_id: int, now: datetime, lo_min: float, hi_min: float) -> float | None:
    r = conn.execute("SELECT value_usd FROM n6_marks WHERE position_id=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                     (pos_id, _iso(now - timedelta(minutes=hi_min)), _iso(now - timedelta(minutes=lo_min)))).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _price_ago(conn: sqlite3.Connection, key: str, now: datetime, lo_min: float, hi_min: float) -> float | None:
    r = conn.execute("SELECT value FROM n6_prices WHERE key=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                     (key, _iso(now - timedelta(minutes=hi_min)), _iso(now - timedelta(minutes=lo_min)))).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _once(conn: sqlite3.Connection, pos_id: int, rule: str, since: datetime) -> bool:
    """同じ決まりの注意を、since からまだ出していなければ True。"""
    return conn.execute("SELECT 1 FROM n6_events WHERE position_id=? AND rule=? AND ts > ? LIMIT 1",
                        (pos_id, rule, _iso(since))).fetchone() is None


def rules(conn: sqlite3.Connection, mk: Market, config: Config, pos: sqlite3.Row, st: dict[str, Any], now: datetime,
          target: float, step_events: list[dict[str, Any]]) -> tuple[str, int, str] | None:
    """早く出る4つの段階のうち、段階1〜3（段階4は別）。出るなら (決まり, 段階, 理由)。注意は記録だけ。"""
    risk, guard = config.risk, config.guard
    pid, key = pos["portfolio_id"], pos["opp_key"]
    o = mk.ops.get(key)
    # 段階1: 保険の強制決済
    if any(e["action"] == "liquidation" for e in step_events):
        return "liquidation", 1, "保険（Lighter の売り）が強制的に閉じられた"
    # 段階1: 自分の建玉の値打ちが1時間で −10%
    v_now = sim.value(st)["value"]
    v_1h = _mark_ago(conn, pos["id"], now, 55, 90)
    if v_1h and v_1h > 0 and (v_now / v_1h - 1) * 100 <= -risk.emergency_own_value_drop_1h_pct:
        return "own_value_drop", 1, f"建玉の値打ちが1時間で {(v_now / v_1h - 1) * 100:.1f}%（線 −{risk.emergency_own_value_drop_1h_pct:g}%）"
    # 段階1: プールのお金（Merkl の預かり額）が1時間で −30%
    tvl_now = (mk.tvl.get(key) or (None, None))[0]
    tvl_1h = mk.opportunity_tvl_hours_ago(key, 1.0)
    if tvl_now is not None and tvl_1h and (1 - tvl_now / tvl_1h) * 100 >= risk.emergency_pool_funds_drop_1h_pct:
        msg = f"プールのお金（預かり額）が1時間で {(tvl_now / tvl_1h - 1) * 100:.0f}%（${tvl_1h:,.0f} → ${tvl_now:,.0f}。線 −{risk.emergency_pool_funds_drop_1h_pct:g}%）"
        if risk.pool_funds_drop_action == "exit":
            return "pool_funds_drop", 1, msg
        if _once(conn, pos["id"], "pool_funds_drop", now - timedelta(hours=1)):
            event(conn, now, pid, pos["id"], "caution", f"{msg}。{risk.pool_funds_block_hours:g}時間、新しく入らない（出るのは手で）",
                  rule="pool_funds_drop", stage=1, opp_key=key)
    if o is not None and any(f.code == "VAULT_CHANGED" for f in o.flags) and _once(conn, pos["id"], "vault_changed", now - timedelta(days=1)):
        event(conn, now, pid, pos["id"], "caution", "金庫の運用先が変わった（運営が仕組みを変えた合図）", rule="vault_changed",
              stage=1, opp_key=key)
    # 段階2: ボーナスのコインが24時間で −15%
    for c in mk.campaigns(key):
        rk = f"reward:{c.get('distribution_chain_id')}:{str(c.get('reward_address') or '').lower()}"
        px = mk.reward_price(c)
        if not px:
            continue
        p24 = _price_ago(conn, rk, now, 22 * 60, 26 * 60) or mk.defillama_hours_ago(
            c.get("distribution_chain_id"), c.get("reward_address"), c.get("reward_symbol"), 24)
        if p24 and (px / p24 - 1) * 100 <= risk.exit_reward_token_24h_pct:
            return "reward_drop", 2, f"ボーナスのコイン（{c.get('reward_symbol')}）が24時間で {(px / p24 - 1) * 100:.1f}%（線 {risk.exit_reward_token_24h_pct:g}%）"
    # 段階2: 投げ売り（値動きするコインが1時間で −15% か 24時間で −30%）
    for i, t in enumerate(st["tokens"]):
        if t["stable"]:
            continue
        k = f"usd:{st['chain_id']}:{t['addr'] or t['sym']}"
        now_p = st["u"][i]
        p1 = _price_ago(conn, k, now, 45, 90)
        p24 = _price_ago(conn, k, now, 22 * 60, 26 * 60) or mk.defillama_hours_ago(st["chain_id"], t["addr"], t["sym"], 24)
        if p1 and (now_p / p1 - 1) * 100 <= risk.exit_dump_1h_pct:
            return "dump", 2, f"{t['sym']} が1時間で {(now_p / p1 - 1) * 100:.1f}%（投げ売りの線 {risk.exit_dump_1h_pct:g}%）"
        if p24 and (now_p / p24 - 1) * 100 <= risk.exit_dump_24h_pct:
            return "dump", 2, f"{t['sym']} が24時間で {(now_p / p24 - 1) * 100:.1f}%（投げ売りの線 {risk.exit_dump_24h_pct:g}%）"
    # 段階3: キャンペーンが終わった・入れる先が一覧から消えた
    if not mk.campaigns(key):
        return "campaign_end", 3, "ボーナスを配るキャンペーンが終わった"
    if o is None:
        return "gone", 3, "入れる先が探すの一覧から消えた（計算できない）"
    cur = variant(o, float(pos["amount_usd"]), bool(pos["hedge"])) or variant(o, float(pos["amount_usd"]), None)
    # 段階2: 残る利回りが狙いを3回続けて下回った（始めたときから下回っていた建玉は数えない）
    if cur is not None and cur.apr_pct < target:
        st["below_target_n"] = st.get("below_target_n", 0) + 1
    else:
        st["below_target_n"] = 0
    if st["below_target_n"] >= guard.below_target_times and not st.get("started_below_target"):
        return "below_target", 2, f"残る利回り（控えめ）が狙い {target:g}% を {st['below_target_n']} 回続けて下回った（今 {cur.apr_pct:.1f}%）"
    # 段階3: 終わりが近い（残りの日数 × 今の1日の残り < 出る費用）
    ex = sim.exit_cost(st)["total"]
    left = o.base.days_left
    if cur is not None and left is not None and left * max(cur.net, 0.0) < ex:
        return "end_near", 3, f"残り {left:.1f} 日で残る見込み ${left * max(cur.net, 0):,.2f} が、出る費用 ${ex:,.2f} より少ない"
    # 段階3（注意）: ボーナスが始めたときの半分以下
    b0 = (st.get("est") or {}).get("bonus_usd_per_day")
    b1 = o.base.bonus_usd_per_day
    if b0 and b1 is not None and b1 <= risk.bonus_drop_ratio * b0 and _once(conn, pos["id"], "bonus_drop", now - timedelta(days=1)):
        event(conn, now, pid, pos["id"], "caution", f"配るボーナスが始めたときの {b1 / b0 * 100:.0f}%（1日 ${b0:,.0f} → ${b1:,.0f}）",
              rule="bonus_drop", stage=3, opp_key=key)
    return None


def better_place(conn: sqlite3.Connection, mk: Market, config: Config, pf: sqlite3.Row, mains: list[sqlite3.Row],
                 pos: sqlite3.Row, st: dict[str, Any], danger: Danger, target: float, now: datetime
                 ) -> dict[str, Any] | None:
    """段階4: もっと良い場所（（1日の差 × いる日数）> 移る費用 × 2）。移る先の候補と理由。"""
    o = mk.ops.get(pos["opp_key"])
    amount = float(pos["amount_usd"])
    cur = variant(o, amount, bool(pos["hedge"])) if o else None
    if cur is None:
        return None
    exit_c = sim.exit_cost(st)["total"]
    cands = candidates(conn, mk, config, pf, mains, danger, target, now,
                       cash_extra=sim.value(st)["value"] - exit_c, skip_id=pos["id"])
    for c in cands:
        if c["op"].base.key == pos["opp_key"]:
            continue
        alt = c["variant"]
        days = min(config.opportunities.stay_days, c["op"].base.days_left or config.opportunities.stay_days)
        gain = (alt.net / alt.amount - cur.net / amount) * c["amount"] * days
        cost = exit_c + alt.move_cost / 2
        if gain > config.guard.better_cost_multiple * cost:
            return {**c, "gain": gain, "cost": cost, "days": days,
                    "why": f"{c['op'].base.name} の方が {days:.0f} 日で ${gain:,.2f} 多く残る見込み（移る費用 ${cost:,.2f} の "
                           f"{config.guard.better_cost_multiple:g} 倍より大きい。今 {cur.apr_pct:.1f}% → {alt.apr_pct:.1f}%）"}
        break
    return None


# --- 損の線 ----------------------------------------------------------------------------------------

def _equity(conn: sqlite3.Connection, pid: str) -> tuple[float, float, float]:
    pf = conn.execute("SELECT cash_usd FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    rows = conn.execute("SELECT amount_usd, value_usd FROM n6_positions WHERE portfolio_id=? AND status='open' "
                        "AND twin_of IS NULL", (pid,)).fetchall()
    placed = sum(float(r[0]) for r in rows)
    return float(pf[0]) + sum(float(r[1] or r[0]) for r in rows), placed, float(pf[0])


def loss_status(conn: sqlite3.Connection, config: Config, pf: sqlite3.Row, now: datetime) -> dict[str, Any]:
    """練習のまとまりの損の線（%）。1日は N6 の新しい線、7日・始めてからは今の線（指示書 8・9）。旧の1日の線は影。"""
    pid = pf["id"]
    st = _pf_state(pf)
    eq_now, placed_now, _ = _equity(conn, pid)
    start = _t(pf["started_at"])
    resumed = _t(st["resumed_at"]) if st.get("resumed_at") else None
    day0 = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    since = {"day": day0, "week": now - timedelta(days=7), "since_start": start}
    lines = {"day": config.n6.new.loss_day, "week": config.guard.loss_lines["week"],
             "since_start": config.guard.loss_lines["since_start"]}
    out: dict[str, Any] = {"equity": eq_now, "placed": placed_now, "periods": {}}
    for p, t0 in since.items():
        t0 = max(t0, start, resumed) if resumed else max(t0, start)
        if t0 == start and not resumed:
            eq0 = float(pf["total_usd"])           # 始めたときは総額（全部が置いていないお金）
        else:
            r = conn.execute("SELECT equity_usd FROM n6_portfolio_marks WHERE portfolio_id=? AND ts >= ? ORDER BY ts LIMIT 1",
                             (pid, _iso(t0))).fetchone()
            eq0 = float(r[0]) if r else eq_now
        mx = conn.execute("SELECT MAX(placed_usd) FROM n6_portfolio_marks WHERE portfolio_id=? AND ts >= ?",
                          (pid, _iso(t0))).fetchone()[0]
        base = max(float(mx or 0.0), placed_now) or float(pf["total_usd"])
        pct = (eq_now - eq0) / base * 100
        level = None
        for lv in LOSS_LEVELS:
            if pct <= lines[p][lv]:
                level = lv
        out["periods"][p] = {"pct": pct, "usd": eq_now - eq0, "base": base, "level": level, "line": lines[p]}
    old_line = config.n6.old.loss_day
    d = out["periods"]["day"]["pct"]
    out["old_day_level"] = next((lv for lv in reversed(LOSS_LEVELS) if d <= old_line[lv]), None)
    return out


def apply_loss_lines(conn: sqlite3.Connection, config: Config, pf: sqlite3.Row, now: datetime) -> dict[str, Any]:
    pid = pf["id"]
    stt = _pf_state(pf)
    ls = loss_status(conn, config, pf, now)
    active = set(stt.get("loss_active") or [])
    day = now.astimezone(JST).strftime("%Y-%m-%d")
    if stt.get("loss_day") != day:
        active = {a for a in active if not a.startswith("day:")}
        stt["old_active"] = []
        stt["loss_day"] = day
    now_active = set()
    for p, row in ls["periods"].items():
        if row["level"] is None:
            continue
        for lv in LOSS_LEVELS[:LOSS_LEVELS.index(row["level"]) + 1]:
            now_active.add(f"{p}:{lv}")
    stop = None
    for a in sorted(now_active - active, key=lambda x: LOSS_LEVELS.index(x.split(":")[1])):
        p, lv = a.split(":")
        row = ls["periods"][p]
        event(conn, now, pid, None, "stop" if lv == "stop" else ("no_new" if lv == "no_new" else "caution"),
              f"損の線（{PERIOD_JA[p]}）「{LEVEL_JA[lv]}」: {row['pct']:+.2f}%（${row['usd']:+,.2f}・線 {row['line'][lv]:g}%）",
              rule=f"loss_{p}", data=row)
        if lv == "stop":
            stop = (p, row)
    # 旧の1日の線（影。前の設定ならどうなっていたか）
    old_active = set(stt.get("old_active") or [])
    if ls["old_day_level"]:
        for lv in LOSS_LEVELS[:LOSS_LEVELS.index(ls["old_day_level"]) + 1]:
            if lv not in old_active:
                event(conn, now, pid, None, lv, f"（影・前の1日の線）「{LEVEL_JA[lv]}」: {ls['periods']['day']['pct']:+.2f}%"
                      f"（前の線 {config.n6.old.loss_day[lv]:g}%）", rule="loss_day_old", shadow=True)
                old_active.add(lv)
    stt["old_active"] = sorted(old_active)
    stt["loss_active"] = sorted(now_active)
    stt["no_new"] = any(a.endswith(":no_new") or a.endswith(":stop") for a in now_active)
    _save_pf_state(conn, pid, stt)
    if stop is not None:
        for pos in _open_mains(conn, pid):
            close_position(conn, pos, now, f"loss_{stop[0]}", f"損の線（{PERIOD_JA[stop[0]]}）「すべて止める」に届いた")
        conn.execute("UPDATE n6_portfolios SET status='stopped', stopped_at=?, stopped_reason=? WHERE id=?",
                     (_iso(now), f"損の線（{PERIOD_JA[stop[0]]}）{stop[1]['pct']:+.2f}%", pid))
    return ls


# --- 1回 ---------------------------------------------------------------------------------------------

def _record_prices(conn: sqlite3.Connection, mk: Market, st: dict[str, Any], key: str, now: datetime) -> None:
    rows = []
    for i, t in enumerate(st["tokens"]):
        if not t["stable"]:
            rows.append((f"usd:{st['chain_id']}:{t['addr'] or t['sym']}", _iso(now), st["u"][i]))
    for c in mk.campaigns(key):
        px = mk.reward_price(c)
        if px:
            rows.append((f"reward:{c.get('distribution_chain_id')}:{str(c.get('reward_address') or '').lower()}",
                         _iso(now), px))
    conn.executemany("INSERT OR REPLACE INTO n6_prices(key, ts, value) VALUES (?,?,?)", rows)


def _mark(conn: sqlite3.Connection, mk: Market, pos: sqlite3.Row, st: dict[str, Any], now: datetime) -> dict[str, Any]:
    v = sim.value(st)
    o = mk.ops.get(pos["opp_key"])
    amount = float(pos["amount_usd"])
    cur = variant(o, amount, bool(pos["hedge"]))
    cur_old = variant(mk.ops_old.get(pos["opp_key"]), amount, bool(pos["hedge"]))
    detail = {**{k: round(x, 6) for k, x in v.items()}, "price_src": st.get("price_src"), "u": st["u"],
              "est_now": cur.apr_pct if cur else None, "est_old_now": cur_old.apr_pct if cur_old else None,
              "bonus_day": st.get("last_bonus_day"), "rebalances": st["reb"]["count"],
              "hedge_equity": sim.hedge_equity(st), "maintenance": sim.maintenance(st),
              "in_range_pct": st["in_range_s"] / st["total_s"] * 100 if st["total_s"] else None}
    if "old" in st:
        old = st["old"]
        detail["old"] = {"rebalances": old["count"], "cost": old["cost"], "bonus_usd": old["bonus_usd"],
                         "pool_value": old["pool_value"],
                         "in_range_pct": old["in_range_s"] / st["total_s"] * 100 if st["total_s"] else None}
    in_range = None
    if st["geo"] in sim.RANGE_GEOS:
        in_range = 1.0 if st["lower"] <= st["P"] <= st["upper"] else 0.0
    conn.execute("INSERT OR REPLACE INTO n6_marks(position_id, ts, value_usd, pnl_usd, in_range, price, detail_json) "
                 "VALUES (?,?,?,?,?,?,?)", (pos["id"], _iso(now), v["value"], v["value"] - amount, in_range, st["P"],
                                            json.dumps(detail, ensure_ascii=False, default=str)))
    return detail


def _save(conn: sqlite3.Connection, pos: sqlite3.Row, st: dict[str, Any], now: datetime) -> None:
    v = sim.value(st)
    amount = float(pos["amount_usd"])
    conn.execute("""UPDATE n6_positions SET state_json=?, last_ts=?, value_usd=?, pnl_usd=?, bonus_usd=?, fees_usd=?,
                    price_move_usd=?, rebalance_cost_usd=?, hedge_usd=?, hedge_cost_usd=?, funding_usd=?,
                    entry_exit_cost_usd=?, max_drawdown_usd=?, rebalances=? WHERE id=? AND status='open'""",
                 (json.dumps(st, ensure_ascii=False, default=str), _iso(now), v["value"], v["value"] - amount, v["bonus"],
                  v["fees"], v["price_move"], v["rebalance_cost"], v["hedge_pnl"], v["hedge_cost"], v["funding"],
                  v["entry_cost"], st.get("max_dd", 0.0), st["reb"]["count"], pos["id"]))


def _step_position(conn: sqlite3.Connection, mk: Market, config: Config, pos: sqlite3.Row, now: datetime
                   ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    st = json.loads(pos["state_json"])
    o = mk.ops.get(pos["opp_key"])
    if o is not None:
        st["denoms"] = {str(d["campaign_id"]): d["in_range_share"] for d in o.denominators if d.get("use") == "B"}
    evs = sim.step(st, mk, pos["opp_key"], now, config.n6.new, config.n6.old, config.guard)
    twin = pos["twin_of"] is not None
    for e in evs:
        msg = ("（対）" if twin else "") + e["message_ja"]
        event(conn, now, pos["portfolio_id"], pos["id"], e["action"], msg, rule=e.get("rule"), stage=e.get("stage"),
              data=e.get("data"), shadow=twin, opp_key=pos["opp_key"])
    for sale in sim.sell_bonus(st, now, config.n6.bonus_sale_hour_jst):
        conn.execute("INSERT OR REPLACE INTO n6_bonus_sales(position_id, day, symbol, ts, units, price, usd, cost_usd) "
                     "VALUES (?,?,?,?,?,?,?,?)", (pos["id"], sale["day"], sale["symbol"], _iso(now), sale["units"],
                                                  sale["price"], sale["usd"], sale["cost"]))
    _record_prices(conn, mk, st, pos["opp_key"], now)
    return st, evs


def process_requests(conn: sqlite3.Connection, mk: Market, config: Config, danger: Danger, target: float,
                     now: datetime) -> int:
    """自分で選ぶ練習の申し込みを入れる（探すのボタン）。上限をこえるものは理由を書いて断る。"""
    n = 0
    for rq in conn.execute("SELECT * FROM n6_requests WHERE status='waiting' ORDER BY id").fetchall():
        pid = rq["portfolio_id"]
        total = float(pid.split("_", 1)[1])
        ensure_portfolio(conn, "owner", total, now)
        pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
        o = mk.ops.get(rq["opp_key"])
        msg, pos_id = None, None
        if pf["status"] != "running":
            msg = "この練習は損の線で止まっている（再開してから）"
        elif _pf_state(pf).get("no_new"):
            msg = "損の線「新しく入らない」に届いている"
        elif o is None or not o.computable or o.excluded or o.kind not in KINDS:
            msg = "この入れる先は今計算できない（外す印・キャンペーンの終わりなど）"
        elif any(r["opp_key"] == rq["opp_key"] for r in _open_mains(conn, pid)):
            msg = "同じ入れる先にもう入っている"
        else:
            hedge = {"yes": True, "no": False}.get(rq["hedge"])
            d = danger.of(o)
            a, v, why = fit(config, pf, _open_mains(conn, pid), o, d, hedge, amount=rq["amount_usd"])
            if a is None:
                msg = f"上限に収まらない: {why}"
            else:
                why_txt = f"自分で選んだ（{datetime.fromisoformat(rq['ts']).astimezone(JST):%m/%d %H:%M} に申し込み）。" \
                          f"控えめの見込みの年利 {v.apr_pct:.1f}%・危なさ {d.get('label')}・{'保険あり' if v.hedge else '保険なし'}"
                try:
                    pos_id = open_position(conn, mk, config, pid, o, a, v, now, why_txt, d, target)
                    msg = f"入った（${a:,.0f}）"
                    n += 1
                except sim.SimError as exc:
                    msg = f"入れなかった: {exc}"
        conn.execute("UPDATE n6_requests SET status=?, done_at=?, position_id=?, message_ja=? WHERE id=?",
                     ("done" if pos_id else "refused", _iso(now), pos_id, msg, rq["id"]))
    return n


def run_portfolio(conn: sqlite3.Connection, mk: Market, config: Config, pid: str, danger: Danger, target: float,
                  now: datetime) -> dict[str, Any]:
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    out = {"id": pid, "entered": 0, "exited": 0, "moved": 0}
    rows = conn.execute("SELECT * FROM n6_positions WHERE portfolio_id=? AND status='open' ORDER BY twin_of IS NOT NULL, id",
                        (pid,)).fetchall()
    states: dict[int, tuple[sqlite3.Row, dict[str, Any], list[dict[str, Any]]]] = {}
    for pos in rows:
        st, evs = _step_position(conn, mk, config, pos, now)
        states[pos["id"]] = (pos, st, evs)
    for pos, st, evs in states.values():
        _save(conn, pos, st, now)
        _mark(conn, mk, pos, st, now)
    # 段階1〜3（本体だけ。対は本体と一緒に閉じる。対の強制決済は上で記録した）
    # 一覧が読めない回（feeds が空など）は、決まりを当てない（「キャンペーンが終わった」と間違えないため）
    blind = not mk.ops or not mk.camps
    for pos, st, evs in list(states.values()):
        if pos["twin_of"] is not None or blind:
            continue
        hit = rules(conn, mk, config, pos, st, now, target, evs)
        _save(conn, pos, st, now)
        if hit is not None:
            rule, stage, why = hit
            if close_position(conn, pos, now, rule, why, stage=stage, st=st):
                out["exited"] += 1
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    # 段階4（アプリ任せは移る。自分で選ぶは注意だけ）。1回に1つまで
    if pf["status"] == "running" and not blind:
        mains = _open_mains(conn, pid)
        for pos in mains:
            st = json.loads(pos["state_json"])
            alt = better_place(conn, mk, config, pf, mains, pos, st, danger, target, now)
            if alt is None:
                continue
            if pf["picker"] == "app" and not _pf_state(pf).get("no_new"):
                nums = close_position(conn, pos, now, "better_place", f"もっと良い場所へ移る: {alt['why']}", stage=4,
                                      move_reason=alt["why"], st=st)
                if nums:
                    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
                    a, v, why = fit(config, pf, _open_mains(conn, pid), alt["op"], alt["danger"], None)
                    if a is not None:
                        new_id = open_position(conn, mk, config, pid, alt["op"], a, v, now,
                                               f"移ってきた（段階4）: {pick_reason({**alt, 'amount': a, 'variant': v, 'cap_why': why}, target)}",
                                               alt["danger"], target, move_from=pos["id"])
                        conn.execute("UPDATE n6_positions SET move_to=? WHERE id=?", (new_id, pos["id"]))
                    out["moved"] += 1
                break
            if _once(conn, pos["id"], "better_place", now - timedelta(days=1)):
                event(conn, now, pid, pos["id"], "caution", f"もっと良い場所がある（段階4・自分で選ぶ練習なので移らない）: {alt['why']}",
                      rule="better_place", stage=4, opp_key=pos["opp_key"])
    # 損の線
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    ls = apply_loss_lines(conn, config, pf, now) if pf["status"] == "running" else None
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    # アプリ任せ: 空いたお金で入れる（オーナーを待たない）
    waiting = None
    fun: dict[str, Any] | None = None
    if pf["picker"] == "app" and not blind:
        # 入る前の内訳（記録だけ。失敗しても見回りは止めない）
        try:
            fun = funnel(conn, mk, config, pf, _open_mains(conn, pid), danger, target, now)
            fun["state"] = "stopped" if pf["status"] != "running" else ("no_new" if _pf_state(pf).get("no_new") else "running")
        except Exception as exc:  # noqa: BLE001
            fun = {"error": f"{type(exc).__name__}: {exc}"}
    if blind:
        waiting = "入れる先の一覧が読めない回（見送り）"
    elif pf["picker"] == "app" and pf["status"] == "running" and not _pf_state(pf).get("no_new"):
        bad: set[str] = set()
        for _ in range(12):
            pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
            cands = [c for c in candidates(conn, mk, config, pf, _open_mains(conn, pid), danger, target, now)
                     if c["op"].base.key not in bad]
            if not cands:
                waiting = "狙い利回り以上で、上限に収まる入れる先がない" if float(pf["cash_usd"]) >= min(config.n6.amounts_usd) \
                    else "置いていないお金が、いちばん小さい額の段より少ない"
                break
            c = cands[0]
            try:
                open_position(conn, mk, config, pid, c["op"], c["amount"], c["variant"], now, pick_reason(c, target),
                              c["danger"], target)
            except sim.SimError as exc:
                bad.add(c["op"].base.key)
                event(conn, now, pid, None, "info", f"{c['op'].base.name} に入れなかった: {exc}", opp_key=c["op"].base.key)
                continue
            out["entered"] += 1
    eq, placed, cash = _equity(conn, pid)
    detail = {"loss": ls, "waiting": waiting, "status": pf["status"]}
    if fun is not None:
        fun["entered"] = out["entered"]
        detail["funnel"] = fun
    conn.execute("INSERT OR REPLACE INTO n6_portfolio_marks(portfolio_id, ts, equity_usd, placed_usd, cash_usd, detail_json) "
                 "VALUES (?,?,?,?,?,?)", (pid, _iso(now), eq, placed, cash, json.dumps(detail, ensure_ascii=False, default=str)))
    out.update(equity=eq, placed=placed, cash=cash, status=pf["status"], waiting=waiting,
               positions=len(_open_mains(conn, pid)))
    return out


def tick(config: Config, now: datetime | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """15分ごとの1回（feeds の n6_practice から呼ぶ）。100% 仮想。"""
    now = now or datetime.now(UTC)
    if not config.n6.enabled:
        return {"enabled": False, "ts": _iso(now)}
    own = conn is None
    conn = conn or db.connect(config.database_path)
    fconn = _feeds_conn(config.feeds.database_path)
    try:
        cfg_new = rules_config(config, config.n6.new)
        cfg_old = rules_config(config, config.n6.old)
        ops = opps.collect(conn, cfg_new, now)
        ops_old = opps.collect(conn, cfg_old, now)
        mk = Market(fconn, cfg_new, now, ops, ops_old)
        target = app_settings.target_apr_pct(conn, config)
        danger = Danger(target, config.n6.amounts_usd[0])
        for total in config.n6.app_portfolios:
            ensure_portfolio(conn, "app", total, now)
        requested = process_requests(conn, mk, cfg_new, danger, target, now)
        results = {}
        for r in conn.execute("SELECT id FROM n6_portfolios ORDER BY id").fetchall():
            results[r["id"]] = run_portfolio(conn, mk, cfg_new, r["id"], danger, target, now)
        out = {"ts": _iso(now), "ok": True, "target": target, "opportunities": len(ops), "requests": requested,
               "portfolios": results}
        conn.execute("INSERT OR REPLACE INTO n6_ticks(ts, ok, detail_json) VALUES (?,?,?)",
                     (_iso(now), 1, json.dumps(out, ensure_ascii=False, default=str)))
        conn.commit()
        return out
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute("INSERT OR REPLACE INTO n6_ticks(ts, ok, detail_json) VALUES (?,?,?)",
                         (_iso(now), 0, json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)))
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        if fconn is not None:
            fconn.close()
        if own:
            conn.close()


def manual_exit(conn: sqlite3.Connection, pos_id: int, reason: str, note: str | None, now: datetime | None = None
                ) -> dict[str, Any]:
    """手動の「出る」（指示書 19）。直近の15分ごとの計算の値で閉じる（仮想）。理由とそのときの見込みを残す。"""
    now = now or datetime.now(UTC)
    if reason not in REASONS_JA:
        raise ValueError("理由は 利益確定・不安・別の場所へ移る・テスト・その他 のどれかにしてください。")
    pos = conn.execute("SELECT * FROM n6_positions WHERE id=?", (pos_id,)).fetchone()
    if pos is None:
        raise LookupError("その建玉はありません。")
    if pos["status"] != "open":
        raise ValueError("その建玉はもう閉じています。")
    if pos["twin_of"] is not None:
        raise ValueError("対（比べるための反対側）は、本体と一緒に閉じます。本体の「出る」を使ってください。")
    st = json.loads(pos["state_json"])
    last = conn.execute("SELECT detail_json FROM n6_marks WHERE position_id=? ORDER BY ts DESC LIMIT 1", (pos_id,)).fetchone()
    est = {"start": st.get("est"), "last_mark": json.loads(last[0]) if last else None, "valued_at": st.get("last_ts")}
    why = f"手動で出た（{REASONS_JA[reason]}）" + (f": {note.strip()[:300]}" if note and note.strip() else "")
    snapshot = {k: pos[k] for k in pos.keys() if k != "state_json"}
    nums = close_position(conn, pos, now, "manual", why, st=st)
    if nums is None:
        raise ValueError("その建玉はもう閉じています。")
    conn.execute("INSERT INTO n6_exits(ts, position_id, reason, note, position_json, estimate_json, pnl_usd) "
                 "VALUES (?,?,?,?,?,?,?)", (_iso(now), pos_id, reason, (note or "").strip()[:300] or None,
                                            json.dumps(snapshot, ensure_ascii=False, default=str),
                                            json.dumps(est, ensure_ascii=False, default=str), nums["pnl_usd"]))
    conn.commit()
    return {"position_id": pos_id, "reason": reason, "reason_ja": REASONS_JA[reason], **nums,
            "valued_at": st.get("last_ts")}


def request_entry(conn: sqlite3.Connection, config: Config, opp_key: str, size: float, amount: float | None,
                  hedge: str, now: datetime | None = None) -> dict[str, Any]:
    """自分で選ぶ練習の申し込み。次の15分ごとの回で、そのときの値段と見込みで入る。"""
    now = now or datetime.now(UTC)
    if size not in config.n6.owner_portfolios:
        raise ValueError(f"練習の総額は {' / '.join(f'${x:,.0f}' for x in config.n6.owner_portfolios)} のどれかにしてください。")
    if amount is not None and amount not in config.n6.amounts_usd:
        raise ValueError(f"入れる額は {' / '.join(f'${x:,.0f}' for x in config.n6.amounts_usd)} のどれかにしてください。")
    if hedge not in ("auto", "yes", "no"):
        raise ValueError("保険は auto / yes / no のどれかにしてください。")
    pid = pf_id("owner", size)
    if conn.execute("SELECT 1 FROM n6_requests WHERE portfolio_id=? AND opp_key=? AND status='waiting'",
                    (pid, opp_key)).fetchone():
        raise ValueError("同じ申し込みが、もう待っています。")
    cur = conn.execute("INSERT INTO n6_requests(ts, portfolio_id, opp_key, amount_usd, hedge, status) VALUES (?,?,?,?,?,?)",
                       (_iso(now), pid, opp_key, amount, hedge, "waiting"))
    conn.commit()
    return {"id": int(cur.lastrowid), "portfolio_id": pid, "status": "waiting",
            "message_ja": "申し込みました。次の15分ごとの見回りで、そのときの値段と見込みで入ります（仮想のお金）。"}


def resume(conn: sqlite3.Connection, pid: str, now: datetime | None = None) -> dict[str, Any]:
    """損の線「すべて止める」で止まった練習を再開する（オーナーだけ）。損の線は再開した時点から数え直す。"""
    now = now or datetime.now(UTC)
    pf = conn.execute("SELECT * FROM n6_portfolios WHERE id=?", (pid,)).fetchone()
    if pf is None:
        raise LookupError("その練習はありません。")
    if pf["status"] != "stopped":
        raise ValueError("その練習は止まっていません。")
    st = _pf_state(pf)
    st["resumed_at"] = _iso(now)
    st["loss_active"] = []
    st["no_new"] = False
    conn.execute("UPDATE n6_portfolios SET status='running', state_json=? WHERE id=?", (json.dumps(st, ensure_ascii=False), pid))
    event(conn, now, pid, None, "resume", "練習を再開した（損の線は再開した時点から数え直す）")
    conn.commit()
    return {"id": pid, "status": "running"}


def make_runner(config: Config):
    """feeds の n6_practice から呼ぶ（新しい版のデータベースに書く）。"""
    def run(fconn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        return tick(config, now)
    return run
