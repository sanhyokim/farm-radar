"""1つの仮想の建玉の計算（N6。100% 仮想。お金は動かさない）。

建玉の額 = プールに置く分 + 保険の預け金（Lighter）+ ガス代の予備（2026-10-07 指示書 15）。
15分ごとに、保存した値段で次を進める:
  - プールの値打ち（幅に配るプールは v3 の式。幅なしのプールは x·y=k。預ける型はコインの量 × 値段）
  - ボーナス（Merkl の今の1日の額 × 自分の取り分 × 幅の中にいたか。取り分の分母は N6 の決まり（A / B）。影で A だけも）
  - 置き直し（新: 境目から幅の 15% 外に 30 分。影で旧: 境目を出て 15 分）
  - 保険（売りの損益・資金調達料（実際の向きのまま）・取引の手数料・維持の額。強制決済になったら段階1）
  - 預け金が半分を切ったら「足したとしたら」を記録（建玉は変えない）
状態は dict（n6_positions.state_json）。関数は状態を書き換え、起きたことを返す。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from ..execution.hedge_guard import MATCH_TOLERANCE, edge_zone
from ..execution.paper import lp_amounts
from ..feeds.store import JST as _JST
from ..scoring import model as m
from .market import Market, is_cl_campaign, settings_of

MAX_STEP_S = 2 * 3600          # これより長く空いたら（パソコンが止まっていたなど）、その間のボーナス・資金調達料は数えない（欠損）
GAP_STEP_S = 15 * 60           # 空いたときは、この時間分だけ数える
RANGE_GEOS = ("cl", "synthetic")


class SimError(ValueError):
    """仮想の建玉を作れない（値段が分からないなど。理由はオーナー向けの日本語）。"""


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


# --- 値段 -----------------------------------------------------------------------------------------

def prices(st: dict[str, Any], mk: Market) -> tuple[list[float], float, str]:
    """(コインごとのドル, 2つのコインの比率 P = token0 1個あたりの token1, 値段の出どころ)。分からなければ前の値。"""
    toks = st["tokens"]
    cid = st.get("chain_id")
    u_prev = list(st.get("u") or [1.0] * len(toks))
    srcs = []
    if st["geo"] == "cl":
        ps = mk.pool_state(cid, st.get("pool_id"))
        if ps is not None and ps.get("price"):
            P = float(ps["price"])
            if toks[0]["stable"]:
                u = [1.0, 1.0 / P]
            elif toks[1]["stable"]:
                u = [P, 1.0]
            else:
                p0 = mk.price(cid, toks[0]["addr"], toks[0]["sym"])
                p1 = mk.price(cid, toks[1]["addr"], toks[1]["sym"]) if p0 is None else None
                if p0 is not None:
                    u = [p0.usd, p0.usd / P]
                    srcs.append(p0.source)
                elif p1 is not None:
                    u = [P * p1.usd, p1.usd]
                    srcs.append(p1.source)
                else:
                    u = u_prev
                    srcs.append("前の値")
            return u, P, "+".join(["pool", *srcs])
        return u_prev, float(st.get("P") or 1.0), "前の値（プールの記録なし）"
    u = []
    for i, t in enumerate(toks):
        p = mk.price(cid, t["addr"], t["sym"])
        if p is None:
            u.append(u_prev[i])
            srcs.append("前の値")
        else:
            u.append(p.usd)
            srcs.append(p.source)
    P = u[0] / u[1] if len(u) > 1 and u[1] > 0 else 1.0
    return u, P, "+".join(srcs)


def amounts(st: dict[str, Any], u: list[float], P: float, lp: dict[str, Any] | None = None) -> list[float]:
    """今のプールの中身（コインごとの量）。lp は幅の置き方（無ければ本体の幅）。"""
    geo = st["geo"]
    if geo in RANGE_GEOS:
        lp = lp or st
        d0, d1 = st["dec"]
        x, y = lp_amounts(lp["L"], P, lp["lower"], lp["upper"], d0, d1)
        return [x, y]
    if geo == "full":
        k = st["x0"] * st["y0"]
        return [math.sqrt(k * u[1] / u[0]), math.sqrt(k * u[0] / u[1])] if u[0] > 0 and u[1] > 0 else [st["x0"], st["y0"]]
    return [st["units"]]


def pool_value(st: dict[str, Any], u: list[float], P: float, lp: dict[str, Any] | None = None) -> float:
    return sum(a * p for a, p in zip(amounts(st, u, P, lp), u))


def _sides(st: dict[str, Any], u: list[float]) -> tuple[m.TokenSide, m.TokenSide]:
    d0, d1 = st["dec"]
    return m.TokenSide(usd=u[0], decimals=d0, sigma_usd=0.0), m.TokenSide(usd=u[1], decimals=d1, sigma_usd=0.0)


def place(st: dict[str, Any], usd: float, u: list[float], P: float, r: float) -> dict[str, float]:
    """幅 ±r に usd ドル置いたときの幅と流動性。"""
    t0, t1 = _sides(st, u)
    return {"lower": P * (1 - r), "upper": P * (1 + r), "L": m.liquidity_for_usd(usd, P, r, t0, t1)}


# --- 始める ----------------------------------------------------------------------------------------

def open_state(*, mk: Market, op: Any, variant: Any, r: float | None, hedge_markets: dict[str, dict[str, Any]],
               now: datetime, est: dict[str, Any]) -> dict[str, Any]:
    """建玉の最初の状態。variant は探すと同じ式の見込み（N6 の新しい決まり・控えめ）。"""
    info = mk.info(op.base.key)
    syms = list(info.get("tokens") or [])
    addrs = list(info.get("token_addrs") or []) + [None] * 6
    kind = op.kind
    cid = op.base.evm_chain_id
    s = mk.s
    toks = []
    for sym, addr in zip(syms, addrs):
        stat = mk.data.token(mk.data.coin(cid, addr)) or mk.data.token_by_symbol(cid, sym)
        stable = mk.data.is_stable(stat, sym)
        if kind == "hold" and stable is None and stat is None:
            continue
        toks.append({"sym": sym, "addr": (addr or "").lower() or None, "stable": bool(stable)})
    if kind == "hold":
        toks = toks[:1]
    else:
        toks = toks[:2]
    camps = mk.campaigns(op.base.key)
    pool_id, dec, geo = None, [0, 0], {"pool_range": "synthetic", "pool_full": "full", "hold": "hold"}[kind]
    fee = s.pool_fee_pct / 100
    if kind == "pool_range":
        for c in camps:
            stt = settings_of(c)
            if is_cl_campaign(c) and mk.pool_state(cid, stt.get("poolId")) is not None:
                ps = mk.pool_state(cid, stt.get("poolId"))
                if ps.get("official") == 1 and str(ps.get("hooks") or "").lower() in ("0x" + "00" * 20,):
                    pool_id = str(stt["poolId"]).lower()
                    dec = [int(stt["decimalsCurrency0"]), int(stt["decimalsCurrency1"])]
                    geo = "cl"
                    # Merkl の設定の並び（currency0/1）を正にする（一覧の並びと逆のことがある）
                    c0, c1 = str(stt.get("currency0") or "").lower(), str(stt.get("currency1") or "").lower()
                    by = {t["addr"]: t for t in toks}
                    if c0 in by and c1 in by:
                        toks = [by[c0], by[c1]]
                    lpf = ps.get("lp_fee")
                    if lpf is not None and 0 < lpf < 1_000_000:
                        fee = lpf / 1_000_000
                    break
    gas = s.gas_usd_per_tx.get(op.base.chain or "", max(s.gas_usd_per_tx.values(), default=0.2))
    split = dict(variant.split)
    st: dict[str, Any] = {
        "geo": geo, "kind": kind, "chain_id": cid, "chain": op.base.chain, "pool_id": pool_id, "tokens": toks,
        "dec": dec, "fee": fee, "gas": gas, "swap_ratio": mk.config.scoring.swap_ratio, "split": split,
        "costs": {k: 0.0 for k in ("entry_swap", "entry_gas", "entry_taker", "reb_swap", "reb_gas", "hedge_taker",
                                   "exit_swap", "exit_gas", "exit_taker", "exit_bonus")},
        "reb": {"count": 0, "out_since": None, "est_per_day": variant.rebalances_per_day},
        "bonus": {"by": {}, "sold": 0.0, "sale_cost": 0.0, "hold_units": {}, "acc_usd": 0.0, "acc_usd_a": 0.0,
                  "last_sale_day": None, "opened_jst": _iso(now.astimezone(_JST))},
        "fees_usd": 0.0, "in_range_s": 0.0, "total_s": 0.0, "gap_s": 0.0, "est": est, "denoms": {},
        "below_target_n": 0, "notes": [],
    }
    if not toks:
        raise SimError("コインが分からない")
    u, P, src = prices(st, mk)
    if "前の値" in src or any(x <= 0 for x in u) or P <= 0:
        raise SimError("コインの値段が分からない（保存した値段の記録がない）")
    st.update(u=u, P=P, price_src=src)
    pool = split["pool"]
    volatile = any(not t["stable"] for t in toks)
    swap_part = pool * 0.5 if kind.startswith("pool") else (pool if volatile else 0.0)
    st["costs"]["entry_swap"] = swap_part * fee
    st["costs"]["entry_gas"] = 2 * gas
    p0 = pool - st["costs"]["entry_swap"]
    st["P0"] = p0
    if geo in RANGE_GEOS:
        st["r"] = r
        st.update(place(st, p0, u, P, r))
        st["old"] = {"lower": st["lower"], "upper": st["upper"], "L": st["L"], "out_since": None, "count": 0,
                     "cost": 0.0, "in_range_s": 0.0, "bonus_usd": 0.0, "pool_value": p0, "est_per_day": None}
    elif geo == "full":
        st["x0"], st["y0"] = p0 / 2 / u[0], p0 / 2 / u[1]
    else:
        st["units"] = p0 / u[0]
    st["pool_value"] = pool_value(st, u, P)
    legs = []
    amts = amounts(st, u, P)
    for i, t in enumerate(toks):
        mkt = hedge_markets.get(str(t["sym"] or "").upper())
        if mkt is None or t["stable"]:
            continue
        legs.append({"idx": i, "sym": t["sym"], "market": mkt, "size": amts[i], "px": u[i]})
    taker = sum(lg["size"] * u[lg["idx"]] * (lg["market"].get("taker_pct") or 0.0) / 100 for lg in legs)
    st["costs"]["entry_taker"] = taker
    mm0 = sum(lg["size"] * u[lg["idx"]] * (lg["market"].get("mmf") or mk.config.guard.hedge_mmf_fallback) for lg in legs)
    st["hedge"] = {"legs": legs, "pnl": 0.0, "funding": 0.0, "margin0": split.get("hedge_margin", 0.0),
                   "buffer0": split.get("hedge_margin", 0.0) - taker - mm0, "zone": None, "zone_since": None,
                   "adjusted_at": None, "matched_out": False, "adjusts": 0, "liquidated": None, "topups": [],
                   "topup_active": False}
    st["last_ts"] = _iso(now)
    v = value(st)
    st["peak"], st["max_dd"] = v["value"], 0.0
    return st



# --- 値打ち --------------------------------------------------------------------------------------------

def hedge_equity(st: dict[str, Any]) -> float:
    h = st["hedge"]
    return h["margin0"] + h["pnl"] - h["funding"] - st["costs"]["entry_taker"] - st["costs"]["hedge_taker"]


def maintenance(st: dict[str, Any], mmf_fallback: float = 0.05) -> float:
    u = st["u"]
    return sum(lg["size"] * u[lg["idx"]] * (lg["market"].get("mmf") or mmf_fallback) for lg in st["hedge"]["legs"])


def bonus_value(st: dict[str, Any]) -> float:
    b = st["bonus"]
    return b["sold"] + sum(x["units"] * (x.get("px") or 0.0) for x in b["by"].values())


def value(st: dict[str, Any]) -> dict[str, float]:
    """今の値打ちと内訳（出る費用の前）。値打ち − 建玉の額 = 損益。"""
    c = st["costs"]
    reserve = st["split"].get("reserve", 0.0) - c["entry_gas"] - c["reb_gas"]
    eq = hedge_equity(st)
    bonus = bonus_value(st)
    total = st["pool_value"] + reserve + eq + bonus + st["fees_usd"]
    return {"value": total, "pool": st["pool_value"], "reserve": reserve, "hedge_equity": eq, "bonus": bonus,
            "fees": st["fees_usd"],
            "price_move": st["pool_value"] + c["reb_swap"] + c["entry_swap"] - st["split"]["pool"],
            "hedge_pnl": st["hedge"]["pnl"], "funding": st["hedge"]["funding"],
            "rebalance_cost": c["reb_swap"] + c["reb_gas"], "hedge_cost": c["hedge_taker"],
            "entry_cost": c["entry_swap"] + c["entry_gas"] + c["entry_taker"]}


def exit_cost(st: dict[str, Any]) -> dict[str, float]:
    """今出たときの費用（両替・ガス代2回・保険を閉じる手数料・残りのボーナスを売る費用）。"""
    volatile = any(not t["stable"] for t in st["tokens"])
    swap_part = st["pool_value"] * 0.5 if st["kind"].startswith("pool") else (st["pool_value"] if volatile else 0.0)
    u = st["u"]
    taker = sum(lg["size"] * u[lg["idx"]] * (lg["market"].get("taker_pct") or 0.0) / 100 for lg in st["hedge"]["legs"])
    unsold = [x for x in st["bonus"]["by"].values() if x["units"] > 0 and x.get("px")]
    bonus = sum(x["units"] * x["px"] * st["fee"] for x in unsold) + (st["gas"] if unsold else 0.0)
    return {"swap": swap_part * st["fee"], "gas": 2 * st["gas"], "taker": taker, "bonus": bonus,
            "total": swap_part * st["fee"] + 2 * st["gas"] + taker + bonus}


# --- ボーナス --------------------------------------------------------------------------------------

def campaign_day(c: dict[str, Any], v: float, tvl: float, lshare: float | None, b_ratio: float | None,
                 in_range: bool, range_geo: bool) -> float:
    """1つのキャンペーンから、今の1日の額（ドル）。探すの _variant と同じ式（幅の中にいたかは実際の値段で）。"""
    from .. import opportunities as opps
    if (c.get("reward_type") or "TOKEN").upper() != "TOKEN" or v <= 0:
        return 0.0
    st = settings_of(c)
    if lshare is not None and is_cl_campaign(c) and (c.get("distribution_type") or "").upper() == "DUTCH_AUCTION":
        d = c.get("daily_rewards") if c.get("daily_rewards") is not None else opps.budget_daily(c)
        w_fee = float(st.get("weightFees") or 0) / 10000
        w_tok = float(st.get("weightToken0") or 0) / 10000 + float(st.get("weightToken1") or 0) / 10000
        if w_fee + w_tok <= 0:
            w_fee = 1.0
        denom = max(tvl, 0.0) * b_ratio if b_ratio is not None else max(tvl, 0.0)
        dshare = v / (denom + v)
        inc = (d or 0.0) * (w_fee * lshare + w_tok * dshare) / (w_fee + w_tok)
    else:
        inc, _ = opps.campaign_income(c, v, tvl)
    if range_geo and st.get("isOutOfRangeIncentivized") is not True and not in_range:
        inc = 0.0
    return inc


def _lshare(st: dict[str, Any], mk: Market, lp: dict[str, Any], in_range: bool) -> float | None:
    if st["geo"] != "cl":
        return None
    if not in_range:
        return 0.0
    ps = mk.pool_state(st["chain_id"], st["pool_id"])
    if ps is None or ps.get("liquidity") is None:
        return None
    active = float(ps["liquidity"])
    return lp["L"] / (active + lp["L"]) if lp["L"] > 0 else 0.0


def accrue_bonus(st: dict[str, Any], mk: Market, key: str, days: float, in_range: bool,
                 old_in_range: bool | None) -> dict[str, float]:
    """ボーナスを進める。本体は N6 の分母（B の条件がそろえば B）、影は A だけ（と旧の幅）。1日の額を返す。"""
    camps = mk.campaigns(key)
    tvl = (mk.tvl.get(key) or (None, None))[0] or 0.0
    range_geo = st["geo"] in RANGE_GEOS
    v = st["pool_value"]
    lshare = _lshare(st, mk, st, in_range)
    day_new = day_a = day_old = 0.0
    b = st["bonus"]
    for c in camps:
        ratio = st["denoms"].get(str(c.get("campaign_id")))
        inc = campaign_day(c, v, tvl, lshare, ratio, in_range, range_geo)
        inc_a = campaign_day(c, v, tvl, lshare, None, in_range, range_geo) if ratio is not None else inc
        day_new += inc
        day_a += inc_a
        px = mk.reward_price(c)
        if inc > 0 and px:
            rk = f"{c.get('distribution_chain_id')}:{str(c.get('reward_address') or '').lower()}"
            x = b["by"].setdefault(rk, {"sym": c.get("reward_symbol"), "units": 0.0, "px": px})
            x["units"] += inc * days / px
        if range_geo and old_in_range is not None:
            old = st["old"]
            old_ls = _lshare(st, mk, old, old_in_range)
            day_old += campaign_day(c, old["pool_value"], tvl, old_ls, None, old_in_range, range_geo)
    # 値段は、売っていないボーナスのコインすべてで今の値に直す
    for rk, x in b["by"].items():
        chain, _, addr = rk.partition(":")
        p = mk.reward_px.get((int(chain), addr)) if chain.isdigit() else None
        if p is not None:
            x["px"] = p[0]
    b["acc_usd"] += day_new * days
    b["acc_usd_a"] += day_a * days
    if range_geo and old_in_range is not None:
        st["old"]["bonus_usd"] += day_old * days
    return {"new": day_new, "a": day_a, "old": day_old}


def sell_bonus(st: dict[str, Any], now: datetime, sale_hour_jst: int) -> list[dict[str, Any]]:
    """1日1回（日本時間 sale_hour_jst 時を過ぎた最初の回）、たまったボーナスを売ったとしたら（実際には売らない）。
    費用 = 両替の手数料（プールの段）+ ガス代1回。持ち続けたときとの差を出すため、売った量も覚えておく。"""
    jst = now.astimezone(_JST)
    day = jst.strftime("%Y-%m-%d")
    b = st["bonus"]
    if b.get("last_sale_day") is None and b.get("opened_jst"):
        opened = datetime.fromisoformat(b["opened_jst"])
        if opened.strftime("%Y-%m-%d") == day and opened.hour >= sale_hour_jst:
            b["last_sale_day"] = day          # 売る時刻のあとに始めた日は、次の日から
    if b.get("last_sale_day") == day or jst.hour < sale_hour_jst:
        return []
    b["last_sale_day"] = day
    out = []
    for rk, x in b["by"].items():
        if x["units"] <= 0 or not x.get("px"):
            continue
        usd = x["units"] * x["px"]
        cost = usd * st["fee"] + st["gas"]
        out.append({"day": day, "symbol": x.get("sym") or rk, "key": rk, "units": x["units"], "price": x["px"],
                    "usd": usd, "cost": cost})
        b["sold"] += usd - cost
        b["sale_cost"] += cost
        b["sales_n"] = b.get("sales_n", 0) + 1
        b["hold_units"][rk] = b["hold_units"].get(rk, 0.0) + x["units"]
        x["units"] = 0.0
    return out


def hold_difference(st: dict[str, Any]) -> dict[str, float]:
    """売らずに持ち続けたとしたら（今の値段）と、1日1回売った額の差。プラス = 持っていた方がよかった。"""
    b = st["bonus"]
    held = sum(units * (b["by"].get(rk, {}).get("px") or 0.0) for rk, units in b["hold_units"].items())
    sold_gross = b["sold"] + b["sale_cost"]
    return {"held_usd": held, "sold_net_usd": b["sold"], "sold_usd": sold_gross, "diff_usd": held - b["sold"]}


# --- 15分ごとに進める -------------------------------------------------------------------------------

def _outside(P: float, lp: dict[str, Any], buffer: float) -> bool:
    edge = buffer * (lp["upper"] - lp["lower"])
    return not (lp["lower"] - edge <= P <= lp["upper"] + edge)


def step(st: dict[str, Any], mk: Market, key: str, now: datetime, rules_new: Any, rules_old: Any,
         guard: Any) -> list[dict[str, Any]]:
    """1回分進める。起きたこと（置き直し・保険を直した・強制決済・足したとしたら）を返す。"""
    events: list[dict[str, Any]] = []
    last = datetime.fromisoformat(st["last_ts"])
    dt = max(0.0, (now - last).total_seconds())
    if dt <= 0:
        return events
    dt_eff = dt if dt <= MAX_STEP_S else GAP_STEP_S
    if dt > MAX_STEP_S:
        st["gap_s"] += dt - dt_eff
    days = dt_eff / 86400
    u_prev = list(st["u"])
    u, P, src = prices(st, mk)
    st.update(u=u, P=P, price_src=src)
    geo = st["geo"]
    range_geo = geo in RANGE_GEOS
    st["pool_value"] = pool_value(st, u, P)
    in_range = (st["lower"] <= P <= st["upper"]) if range_geo else True
    old_in = None
    if range_geo:
        old = st["old"]
        old["pool_value"] = pool_value(st, u, P, old)
        old_in = old["lower"] <= P <= old["upper"]
    # 保険の損益（売り）と資金調達料
    h = st["hedge"]
    for lg in h["legs"]:
        px = u[lg["idx"]]
        h["pnl"] += -lg["size"] * (px - lg.get("px", u_prev[lg["idx"]]))
        lg["px"] = px
        rate, _ = mk.funding_daily(lg["market"])
        if rate is not None:
            h["funding"] += lg["size"] * px * rate * days
    # ボーナス・元の利回り
    day = accrue_bonus(st, mk, key, days, in_range, old_in)
    st["last_bonus_day"] = day
    if geo == "hold":
        native = mk.info(key).get("native_apr")
        try:
            st["fees_usd"] += st["pool_value"] * float(native) / 100 * days / 365 if native else 0.0
        except (TypeError, ValueError):
            pass
    st["total_s"] += dt_eff
    if in_range:
        st["in_range_s"] += dt_eff
    if range_geo and old_in:
        st["old"]["in_range_s"] += dt_eff
    # 置き直し（新）と影の置き直し（旧）
    if range_geo:
        events += _rebalance(st, P, u, now, rules_new, guard)
        _shadow_rebalance(st, P, u, now, rules_old)
    elif geo == "full" and h["legs"] and not h.get("liquidated"):
        events += _match_full(st, u, P, now, guard)
    # 保険: 幅から出たとき（保険の決まりは変えない: 上に出たら閉じる・下に出たら増やす・10%・30分・1時間あける）
    if range_geo and h["legs"] and not h.get("liquidated"):
        events += _hedge_guard(st, P, u, now, guard)
    if h["legs"] and not h.get("liquidated"):
        events += _margin_checks(st, now, guard)
    v = value(st)["value"]
    st["peak"] = max(st.get("peak", v), v)
    st["max_dd"] = max(st.get("max_dd", 0.0), st["peak"] - v)
    st["last_ts"] = _iso(now)
    return events


def _rematch(st: dict[str, Any], u: list[float], P: float) -> float:
    """保険の売りの量を今の中身に合わせる。取引の手数料（ドル）を返す。"""
    amts = amounts(st, u, P)
    fee = 0.0
    for lg in st["hedge"]["legs"]:
        new = amts[lg["idx"]]
        fee += abs(new - lg["size"]) * u[lg["idx"]] * (lg["market"].get("taker_pct") or 0.0) / 100
        lg["size"] = new
    st["costs"]["hedge_taker"] += fee
    return fee


def _rebalance(st: dict[str, Any], P: float, u: list[float], now: datetime, rules: Any, guard: Any) -> list[dict[str, Any]]:
    reb = st["reb"]
    if not _outside(P, st, rules.edge_buffer_frac):
        reb["out_since"] = None
        return []
    if reb["out_since"] is None:
        reb["out_since"] = _iso(now)
    if now - datetime.fromisoformat(reb["out_since"]) < timedelta(minutes=rules.edge_wait_minutes):
        return []
    v = st["pool_value"]
    swap = v * st["swap_ratio"] * st["fee"]
    st["costs"]["reb_swap"] += swap
    st["costs"]["reb_gas"] += 2 * st["gas"]
    before = (st["lower"], st["upper"])
    st.update(place(st, v - swap, u, P, st["r"]))
    st["pool_value"] = pool_value(st, u, P)
    fee = _rematch(st, u, P) if st["hedge"]["legs"] and not st["hedge"].get("liquidated") else 0.0
    h = st["hedge"]
    h["zone"], h["zone_since"], h["matched_out"] = None, None, False
    reb["count"] += 1
    reb["out_since"] = None
    return [{"action": "rebalance", "rule": "edge", "message_ja":
             f"置き直した（幅の境目から幅の {rules.edge_buffer_frac * 100:g}% 外に {rules.edge_wait_minutes:g} 分いた）。"
             f"費用 ${swap + 2 * st['gas'] + fee:,.2f}",
             "data": {"before": before, "after": (st["lower"], st["upper"]), "price": P, "swap_usd": swap,
                      "gas_usd": 2 * st["gas"], "hedge_fee_usd": fee}}]


def _shadow_rebalance(st: dict[str, Any], P: float, u: list[float], now: datetime, rules: Any) -> None:
    """前の決まり（境目を出て 15 分）なら、の影の幅（プールの側だけ。2週間の見直しのため）。"""
    old = st["old"]
    if not _outside(P, old, rules.edge_buffer_frac):
        old["out_since"] = None
        return
    if old["out_since"] is None:
        old["out_since"] = _iso(now)
    if now - datetime.fromisoformat(old["out_since"]) < timedelta(minutes=rules.edge_wait_minutes):
        return
    v = old["pool_value"]
    swap = v * st["swap_ratio"] * st["fee"]
    old["cost"] += swap + 2 * st["gas"]
    old.update(place(st, v - swap, u, P, st["r"]))
    old["pool_value"] = pool_value(st, u, P, old)
    old["count"] += 1
    old["out_since"] = None


def _hedge_guard(st: dict[str, Any], P: float, u: list[float], now: datetime, guard: Any) -> list[dict[str, Any]]:
    h = st["hedge"]
    zone = edge_zone(P, st["lower"], st["upper"], st["r"], guard.hedge_edge_buffer_frac)
    if h.get("zone") != zone:
        h["zone"], h["zone_since"] = zone, _iso(now)
    if zone not in ("above", "below", "inside") or not h.get("zone_since"):
        return []
    if zone == "inside" and not h.get("matched_out"):
        return []
    if now - datetime.fromisoformat(h["zone_since"]) < timedelta(minutes=guard.hedge_edge_wait_minutes):
        return []
    if h.get("adjusted_at") and now - datetime.fromisoformat(h["adjusted_at"]) < timedelta(minutes=guard.hedge_cooldown_minutes):
        return []
    want = amounts(st, u, P)
    if not any(abs(want[lg["idx"]] - lg["size"]) > MATCH_TOLERANCE * max(lg["size"], want[lg["idx"]], 1e-18)
               for lg in h["legs"]):
        return []
    before = [lg["size"] for lg in h["legs"]]
    fee = _rematch(st, u, P)
    h["adjusted_at"] = _iso(now)
    h["matched_out"] = zone != "inside"
    h["adjusts"] += 1
    what = {"above": "値段が幅の上に出たので、保険の売りを閉じた（中身がステーブルだけになった）",
            "below": "値段が幅の下に出たので、保険の売りを増やした（中身が値動きするコインだけになった）",
            "inside": "幅の中に戻ったので、保険の売りを中身に合わせた"}[zone]
    return [{"action": "hedge", "rule": f"hedge_{zone}", "message_ja": f"{what}。手数料 ${fee:,.2f}",
             "data": {"before": before, "after": [lg["size"] for lg in h["legs"]], "price": P}}]


def _match_full(st: dict[str, Any], u: list[float], P: float, now: datetime, guard: Any) -> list[dict[str, Any]]:
    """幅のないプール: 中身が少しずつ変わるので、ずれが5%を超えたら（1時間あけて）保険を合わせる。"""
    h = st["hedge"]
    if h.get("adjusted_at") and now - datetime.fromisoformat(h["adjusted_at"]) < timedelta(minutes=guard.hedge_cooldown_minutes):
        return []
    want = amounts(st, u, P)
    if not any(abs(want[lg["idx"]] - lg["size"]) > MATCH_TOLERANCE * max(lg["size"], want[lg["idx"]], 1e-18)
               for lg in h["legs"]):
        return []
    fee = _rematch(st, u, P)
    h["adjusted_at"] = _iso(now)
    h["adjusts"] += 1
    return [{"action": "hedge", "rule": "hedge_match", "message_ja": f"中身が5%以上変わったので、保険の売りを合わせた。手数料 ${fee:,.2f}",
             "data": {"price": P}}]


def _margin_checks(st: dict[str, Any], now: datetime, guard: Any) -> list[dict[str, Any]]:
    """強制決済（担保 < 維持の額）と「足したとしたら」（余裕がはじめの半分を切った。建玉は変えない）。"""
    h = st["hedge"]
    eq = hedge_equity(st)
    mm = maintenance(st, guard.hedge_mmf_fallback)
    if eq < mm:
        notional = sum(lg["size"] * st["u"][lg["idx"]] for lg in h["legs"])
        h["liquidated"] = _iso(now)
        for lg in h["legs"]:
            lg["size"] = 0.0
        # 強制決済のあとに残る担保（0 未満にはならない）。手数料は分からないので数えない
        lost = min(0.0, eq)
        h["pnl"] -= lost
        return [{"action": "liquidation", "rule": "liquidation", "stage": 1,
                 "message_ja": f"保険（Lighter の売り）が強制的に閉じられた: 担保 ${eq:,.2f} が維持に要る額 ${mm:,.2f} を下回った"
                               f"（売りの額 ${notional:,.2f}）", "data": {"equity": eq, "maintenance": mm}}]
    buffer = eq - mm
    b0 = h.get("buffer0") or 0.0
    if b0 > 0 and buffer < guard.hedge_topup_buffer_frac * b0:
        if not h.get("topup_active"):
            add = b0 - buffer
            rec = {"ts": _iso(now), "add_usd": add, "pool_side_usd": st["pool_value"], "cost_usd": 2 * st["gas"],
                   "buffer_usd": buffer, "buffer0_usd": b0}
            h["topups"].append(rec)
            h["topup_active"] = True
            return [{"action": "topup", "rule": "margin_half",
                     "message_ja": f"保険の余裕がはじめの半分を切った（${buffer:,.2f} / はじめ ${b0:,.2f}）。足したとしたら "
                                   f"${add:,.2f}（記録だけ。建玉は変えない）", "data": rec}]
    elif h.get("topup_active") and buffer >= guard.hedge_topup_buffer_frac * b0:
        h["topup_active"] = False
    return []


# --- 閉じる -----------------------------------------------------------------------------------------

def close_numbers(st: dict[str, Any]) -> dict[str, float]:
    """閉じたときの数字（指示書 22 の項目）。出る費用を引いたあと。"""
    v = value(st)
    ex = exit_cost(st)
    c = st["costs"]
    c["exit_swap"], c["exit_gas"], c["exit_taker"], c["exit_bonus"] = ex["swap"], ex["gas"], ex["taker"], ex["bonus"]
    final = v["value"] - ex["total"]
    sales_gas = st["gas"] * st["bonus"].get("sales_n", 0)
    return {"value_usd": final, "bonus_usd": v["bonus"] - ex["bonus"], "fees_usd": v["fees"],
            "price_move_usd": v["price_move"], "rebalance_cost_usd": v["rebalance_cost"], "hedge_usd": v["hedge_pnl"],
            "hedge_cost_usd": v["hedge_cost"], "funding_usd": v["funding"],
            "gas_usd": c["entry_gas"] + c["reb_gas"] + ex["gas"] + sales_gas,
            "entry_exit_cost_usd": v["entry_cost"] + ex["swap"] + ex["gas"] + ex["taker"],
            "max_drawdown_usd": st.get("max_dd", 0.0), "rebalances": st["reb"]["count"]}
