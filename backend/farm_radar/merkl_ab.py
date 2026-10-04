"""Merkl の分母の検証（2026-10-04 指示書「Merkl B『幅の中だけ』を実測で検証できるようにする」）。

同じ区切り（Merkl の配った合計が変わってから次に変わるまで。約2時間）・同じ預け方で、次の3つを並べる。
- A: 自分の量 ÷ プールの預け方すべての合計（全員が分母）
- B: 自分の量 ÷ その時に値段が幅の中にある預け方の合計（幅の中だけが分母）
- 実際: その区切りに、その預け方がもらった額 ÷ キャンペーン全体で配った額

Merkl の資料（docs.merkl.xyz の concentrated-liquidity-mechanisms）の式:
  手数料の重み ×（預け方の手数料 ÷ プールの手数料）＋ token0 の重み ×（預け方の token0 ÷ プールの token0）
  ＋ token1 の重み ×（預け方の token1 ÷ プールの token1）。「ふつうは幅の中にある預け方だけがもらう」。
資料に書いていないのは、「プールの token0・token1」に幅の外の預け方を入れるか（A）入れないか（B）。それを確かめる。
- 手数料の分: 手数料は幅の中の預け方だけに入るので、A と B は同じ（自分の流動性 ÷ 今の値段のところの流動性）。
- token0・token1 の分: A と B で分母が違う。ここで比べる。

預け方は、チェーンの記録（feeds/pool_history.py が読んだ、預け方を足した・減らした記録）から組み立て直す。
その時の量を使う（今の量を過去に当てはめない）。区切りの途中で自分の預け方が変わったもの（足す・減らす・閉じる・
作る・幅を変える = 別の預け方になる）は使わず、理由を数える。プールのほかの人の預け方が途中で変わるのは、
1時間ごとのプールの状態のときの預け方で数える（今の答え合わせ merkl_check と同じ）。

本番の見込み（探す）は A のまま。ここは検証だけ。5% の上限・置き直しの式は変えない。
読むのは feeds の保存だけ（読み取りだけ）。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime
from typing import Any

from .feeds.pool_history import position_key
from .merkl_check import _amount_at, amounts, boundaries

L_CHECK = 0.01           # 組み立て直した「幅の中の流動性」と、チェーンで読んだ流動性のずれがこれ以下なら合っている
EDGE_S = 60              # 区切りの境目の前後この秒数の中で自分の預け方が変わったら、時刻を確かめきれないので使わない
IN_RANGE_FULL = 0.99     # 区切りのあいだ、ずっと幅の中にいた組だけを比べる（1時間ごとの記録では、出入りした時間が粗い）
WITHIN = 0.30            # 「±30% に入った」の幅
OWN_USD = (100, 500, 1_000, 3_000)
OUT_LABEL = {"in": "幅の中だけ", "out": "幅の外にも配る", "unknown": "判断できない"}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def out_class(st: dict[str, Any]) -> str:
    """キャンペーンの設定 isOutOfRangeIncentivized: false = 幅の中だけ / true = 幅の外にも配る / ない = 判断できない。"""
    v = st.get("isOutOfRangeIncentivized")
    if v is False:
        return "in"
    if v is True:
        return "out"
    return "unknown"


def restricted(st: dict[str, Any]) -> str | None:
    """除外する人（blacklist）・対象の人（whitelist）の決まり。あれば使えない理由を返す。

    Merkl はその人の預け方を分母から外す（作業場所の試験用の値で確かめた: 除外が1件ある ETH/ClawBank は、全部の預け方で
    見込みがそろって実際の 0.3 倍だった）。決まりは預け方の持ち主（番号の持ち主）で書かれていて、持ち主はこの記録
    （預け方を足した・減らした記録）からは分からないので、推測で外さずに使わない。"""
    if "blacklist" not in st and "whitelist" not in st:
        return "除外する人の設定が記録にない（判断できない）"
    if st.get("blacklist") or st.get("whitelist"):
        return "除外する人（または対象の人）の決まりがある（その人の預け方を分母から外せない）"
    return None


def weights(st: dict[str, Any]) -> dict[str, float]:
    return {"fee": float(st.get("weightFees") or 0) / 10000, "token0": float(st.get("weightToken0") or 0) / 10000,
            "token1": float(st.get("weightToken1") or 0) / 10000}


def main_weight(w: dict[str, float]) -> str:
    """キャンペーンの一番大きい重み（手数料が中心 / コインが中心）。"""
    return "手数料が中心" if w["fee"] >= w["token0"] + w["token1"] else "コインが中心"


class Book:
    """1つのプールの預け方（記録の始まりまでの合計 + それ以後の1件ずつの記録）。時刻を進めながら、その時の預け方を出す。"""

    def __init__(self, conn: sqlite3.Connection, chain_id: int, pool_id: str):
        prog = conn.execute("SELECT * FROM pool_liq_progress WHERE chain_id=? AND pool_id=?", (chain_id, pool_id)).fetchone()
        self.ok = prog is not None and prog["done_ts"] is not None and prog["keep_from_block"] is not None
        self.keep_from_ts = int(prog["keep_from_ts"]) if prog else None
        self.done_ts = int(prog["done_ts"]) if prog and prog["done_ts"] is not None else None
        self.meta: dict[str, tuple[str, int, int, str]] = {}     # 印 → (預けた契約, 下, 上, salt)
        self.base: dict[str, int] = {}
        for r in conn.execute("SELECT pos_key, owner, tick_lower, tick_upper, salt, liquidity FROM pool_liq_base "
                              "WHERE chain_id=? AND pool_id=?", (chain_id, pool_id)):
            self.meta[r[0]] = (r[1], int(r[2]), int(r[3]), r[4])
            self.base[r[0]] = int(r[5])
        self.events: list[tuple[int, str, int]] = []
        self.own_events: dict[str, list[tuple[int, int]]] = {}    # 印 → [(時刻, 増減)]
        for r in conn.execute("SELECT ts, pos_key, owner, tick_lower, tick_upper, salt, delta FROM pool_liq_events "
                              "WHERE chain_id=? AND pool_id=? ORDER BY block, log_index", (chain_id, pool_id)):
            self.meta.setdefault(r[1], (r[2], int(r[3]), int(r[4]), r[5]))
            self.events.append((int(r[0]), r[1], int(r[6])))
            self.own_events.setdefault(r[1], []).append((int(r[0]), int(r[6])))
        self.by_salt: dict[tuple[str, int], list[str]] = {}
        for k, (owner, _lo, _hi, salt) in self.meta.items():
            self.by_salt.setdefault((owner, int(salt, 16)), []).append(k)
        self._live = dict(self.base)
        self._i = 0
        self._t = -1

    def at(self, ts: int) -> dict[str, int]:
        """時刻 ts の預け方（印 → 流動性。0 より大きいものだけ）。ts は増える順に呼ぶ。"""
        if ts < self._t:
            self._live, self._i = dict(self.base), 0
        self._t = ts
        while self._i < len(self.events) and self.events[self._i][0] <= ts:
            _, k, d = self.events[self._i]
            self._live[k] = self._live.get(k, 0) + d
            if self._live[k] == 0:
                del self._live[k]
            self._i += 1
        return self._live

    def cuts(self, ts: int, w: int) -> list[dict[str, int]]:
        """時刻 ts の前後 w 秒の中の、記録1件ごとの区切りでの預け方（ブロックの時刻は比例で出していて、
        プールの状態を読んだ時刻ともずれるので、どの区切りがチェーンで読んだ値と合うかを確かめるため）。"""
        live = dict(self.at(ts - w))
        out = [dict(live)]
        i = self._i
        while i < len(self.events) and self.events[i][0] <= ts + w:
            _, k, d = self.events[i]
            live[k] = live.get(k, 0) + d
            if live[k] == 0:
                del live[k]
            out.append(dict(live))
            i += 1
        return out

    def liquidity(self, key: str, ts: int) -> int:
        """1つの預け方の、時刻 ts の量。"""
        return self.base.get(key, 0) + sum(d for t, d in self.own_events.get(key, []) if t <= ts)

    def resolve(self, reason: str, pool_id: str, pm: str | None) -> tuple[str | None, str | None]:
        """Merkl の預け方の名前 → 預け方の印。名前の末尾は、公式の PositionManager の番号か、v4 の預け方の印（0x… 32バイト）。
        チェーンの記録に同じものがあるときだけ使う（推測で結びつけない）。戻り値 = (印, 使えない理由)。"""
        prefix = f"uniswap_v4_{pool_id.lower()}_"
        if not reason.lower().startswith(prefix):
            return None, "名前の形が違う"
        tail = reason[len(prefix):]
        if tail.isdigit():
            if not pm:
                return None, "公式の PositionManager の住所が登録にない"
            keys = self.by_salt.get((pm, int(tail)), [])
            if len(keys) != 1:
                return None, "チェーンの記録に見つからない（記録の始まりより前に閉じた預け方など）" if not keys \
                    else "同じ番号の預け方が2つ以上ある"
            return keys[0], None
        if tail.lower().startswith("0x") and len(tail) == 66:
            key = tail.lower()
            if key not in self.meta:
                return None, "チェーンの記録に見つからない（記録の始まりより前に閉じた預け方など）"
            o, lo, hi, salt = self.meta[key]
            if position_key(o, lo, hi, salt) != key:
                return None, "預け方の印が記録と合わない"
            return key, None
        return None, "名前の形が違う"


def _in_l(book: Book, live: dict[str, int], tick: int) -> int:
    return sum(liq for k, liq in live.items() if liq > 0 and book.meta[k][1] <= tick < book.meta[k][2])


def _denoms(book: Book, s: sqlite3.Row) -> dict[str, Any]:
    """プールの状態1つのときの分母（A: 預け方すべて / B: 幅の中だけ）。最小単位の token0・token1 と、token1 に直した額。

    状態を読んだ時刻の前後 EDGE_S 秒の中で預け方が変わっていたら、記録1件ごとの区切りのうち、
    幅の中の流動性がチェーンで読んだ値（その時の流動性）に一番近いものを使う（合わなければ「合わない」として使わない）。"""
    tick, sq = int(s["tick"]), int(s["sqrt_price_x96"]) / 2 ** 96
    l_act = int(s["liquidity"])
    best, best_gap = None, None
    for live in book.cuts(_ts(s["checked_at"]), EDGE_S):
        gap = abs(_in_l(book, live, tick) - l_act) / l_act if l_act > 0 else 1.0
        if best_gap is None or gap < best_gap:
            best, best_gap = live, gap
    price = sq * sq                                  # token0 の最小単位1つあたりの token1 の最小単位
    all0 = all1 = in0 = in1 = 0.0
    in_l, n_all, n_in = 0, 0, 0
    for k, liq in (best or {}).items():
        if liq <= 0:
            continue
        _, lo, hi, _ = book.meta[k]
        a0, a1 = amounts(float(liq), sq, lo, hi)
        all0, all1, n_all = all0 + a0, all1 + a1, n_all + 1
        if lo <= tick < hi:
            in0, in1, in_l, n_in = in0 + a0, in1 + a1, in_l + liq, n_in + 1
    all_v, in_v = all0 * price + all1, in0 * price + in1
    return {"tick": tick, "sq": sq, "price": price, "l_act": l_act, "in_l": in_l,
            "l_gap": best_gap, "l_ok": best_gap is not None and best_gap <= L_CHECK,
            "all0": all0, "all1": all1, "in0": in0, "in1": in1, "all_v": all_v, "in_v": in_v,
            "ratio_v": in_v / all_v if all_v > 0 else None, "n_all": n_all, "n_in": n_in}


def _q(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    s = sorted(xs)
    i = q * (len(s) - 1)
    lo = int(i)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A と B の誤差（見込み ÷ 実際 − 1）のまん中・25%・75%、±30% に入った割合、キャンペーン数、組の数、記録の日数。"""
    out: dict[str, Any] = {"pairs": len(rows), "campaigns": len({r["campaign_id"] for r in rows})}
    if rows:
        t0 = min(_ts(r["start"]) for r in rows)
        t1 = max(_ts(r["end"]) for r in rows)
        out["days"] = (t1 - t0) / 86400
    else:
        out["days"] = 0.0
    for k in ("a", "b"):
        e = [r[f"err_{k}"] for r in rows]
        out[k] = {"median": _q(e, 0.5), "p25": _q(e, 0.25), "p75": _q(e, 0.75),
                  "within": sum(1 for x in e if abs(x) <= WITHIN) / len(e) if e else None}
    return out


def check_campaign(conn: sqlite3.Connection, c: sqlite3.Row, books: dict[tuple[int, str], Book],
                   denoms: dict[tuple[int, str, str], dict[str, Any]], pm: str | None) -> dict[str, Any]:
    st = json.loads(c["settings_json"] or "{}")
    cid, chain = c["campaign_id"], int(c["chain_id"])
    pool = str(st.get("poolId") or "").lower()
    w = weights(st)
    wsum = sum(w.values()) or 1.0
    wt = w["token0"] + w["token1"]
    oc = out_class(st)
    res: dict[str, Any] = {"campaign_id": cid, "chain_id": chain, "pool_id": pool,
                           "pair": f"{st.get('symbolCurrency0')}/{st.get('symbolCurrency1')}", "weights": w,
                           "main_weight": main_weight(w), "out_class": oc, "rows": [], "intervals": [],
                           "skipped": {}, "unusable": {}, "positions": 0}
    skipped: dict[str, int] = res["skipped"]

    def skip(why: str, n: int = 1) -> None:
        skipped[why] = skipped.get(why, 0) + n

    if oc != "in":
        skip("幅の外にも配る設定なので B を当てはめない" if oc == "out" else "幅の外の扱いが設定にない（判断できない）")
        return res
    why_r = restricted(st)
    if why_r:
        skip(why_r)
        res["restricted"] = why_r
        return res
    key = (chain, pool)
    if key not in books:
        books[key] = Book(conn, chain, pool)
    book = books[key]
    if not book.ok:
        skip("プールの預け方の歴史をまだ読み終えていない")
        return res

    reasons = [r[0] for r in conn.execute("SELECT DISTINCT reason FROM merkl_reward_snaps WHERE campaign_id=?", (cid,))]
    own: dict[str, str] = {}
    for reason in reasons:
        k, why = book.resolve(reason, pool, pm)
        if k is None:
            res["unusable"][why or "?"] = res["unusable"].get(why or "?", 0) + 1
        else:
            own[reason] = k
    res["positions"] = len(reasons)
    totals, why_not = boundaries(conn, cid)
    if why_not:
        skip(why_not)
    for (a_ts, a_sum), (b_ts, b_sum) in zip(totals, totals[1:]):
        total_d = b_sum - a_sum
        if total_d <= 0:
            continue
        ta, tb = _ts(a_ts), _ts(b_ts)
        a, b = {"ts": a_ts}, {"ts": b_ts}
        if book.keep_from_ts is None or ta < book.keep_from_ts:
            skip("区切りがチェーンの記録の始まりより前")
            continue
        if book.done_ts is None or book.done_ts < tb + EDGE_S:
            skip("チェーンの記録を区切りの終わりまで読めていない")
            continue
        states = conn.execute("SELECT checked_at, tick, liquidity, sqrt_price_x96 FROM pool_state_snaps WHERE chain_id=? "
                              "AND pool_id=? AND checked_at>? AND checked_at<=? AND liquidity IS NOT NULL "
                              "AND sqrt_price_x96 IS NOT NULL AND tick IS NOT NULL ORDER BY checked_at",
                              (chain, pool, a["ts"], b["ts"])).fetchall()
        if not states:
            skip("プールの状態がない")
            continue
        ds = []
        for s in states:
            dk = (chain, pool, s["checked_at"])
            if dk not in denoms:
                denoms[dk] = _denoms(book, s)
            ds.append(denoms[dk])
        l_ok = all(d["l_ok"] for d in ds)
        ratios = [d["ratio_v"] for d in ds if d["ratio_v"] is not None]
        res["intervals"].append({"start": a["ts"], "end": b["ts"], "l_ok": l_ok,
                                 "a_den_v": statistics.fmean(d["all_v"] for d in ds),
                                 "b_den_v": statistics.fmean(d["in_v"] for d in ds),
                                 "ratio": statistics.fmean(ratios) if ratios else None,
                                 "n_all": round(statistics.fmean(d["n_all"] for d in ds), 1),
                                 "n_in": round(statistics.fmean(d["n_in"] for d in ds), 1)})
        if not l_ok:
            skip("組み立て直した流動性がチェーンで読んだ値と合わない区切り")
            continue
        for reason, k in own.items():
            before, after = _amount_at(conn, cid, reason, a["ts"]), _amount_at(conn, cid, reason, b["ts"])
            if after is None:
                continue
            got = after - (before or 0)
            evs = [t for t, _ in book.own_events.get(k, []) if ta - EDGE_S < t <= tb + EDGE_S]
            if evs:
                skip("区切りの間に預け方が変わった（足す・減らす・閉じる・作る）"
                     if any(ta < t <= tb for t in evs) else "区切りの境目の近くで預け方が変わった（時刻を確かめきれない）")
                continue
            liq = book.liquidity(k, ta)
            if liq <= 0:
                skip("区切りの始めに預け方がない（閉じていた・まだない）")
                continue
            _, lo, hi, _ = book.meta[k]
            fee, t0a, t0b, t1a, t1b, inside = [], [], [], [], [], 0
            for d in ds:
                if not (lo <= d["tick"] < hi):
                    for xs in (fee, t0a, t0b, t1a, t1b):
                        xs.append(0.0)
                    continue
                inside += 1
                a0, a1 = amounts(float(liq), d["sq"], lo, hi)
                fee.append(liq / d["l_act"])
                t0a.append(a0 / d["all0"] if d["all0"] > 0 else 0.0)
                t0b.append(a0 / d["in0"] if d["in0"] > 0 else 0.0)
                t1a.append(a1 / d["all1"] if d["all1"] > 0 else 0.0)
                t1b.append(a1 / d["in1"] if d["in1"] > 0 else 0.0)
            m = {n: statistics.fmean(xs) for n, xs in (("fee", fee), ("t0a", t0a), ("t0b", t0b), ("t1a", t1a), ("t1b", t1b))}
            pa = (w["fee"] * m["fee"] + w["token0"] * m["t0a"] + w["token1"] * m["t1a"]) / wsum
            pb = (w["fee"] * m["fee"] + w["token0"] * m["t0b"] + w["token1"] * m["t1b"]) / wsum
            actual = got / total_d
            row = {"campaign_id": cid, "start": a["ts"], "end": b["ts"], "position": k, "actual": actual,
                   "pred_a": pa, "pred_b": pb, "in_range_share": inside / len(ds), **m,
                   # A の見込みのうち、重みの種類ごとの分（手数料・token0・token1）
                   "c_fee": w["fee"] * m["fee"] / wsum, "c_t0a": w["token0"] * m["t0a"] / wsum,
                   "c_t1a": w["token1"] * m["t1a"] / wsum}
            # 逆算した分母の割合（今の答え合わせと同じ考え方を、チェーンで数えた A で）: 手数料の分は資料どおりと置き、
            # 残りをコインの分とする。B どおりなら「コインの分の A ÷ B」に、A どおりなら 1 に近くなる
            tok_a = (w["token0"] * m["t0a"] + w["token1"] * m["t1a"]) / wt if wt > 0 else None
            tok_b = (w["token0"] * m["t0b"] + w["token1"] * m["t1b"]) / wt if wt > 0 else None
            tok_actual = (actual * wsum - w["fee"] * m["fee"]) / wt if wt > 0 else None
            row["share_backcalc"] = tok_a / tok_actual if tok_a and tok_actual and tok_actual > 0 else None
            row["share_chain"] = tok_a / tok_b if tok_a and tok_b else None
            if row["in_range_share"] < IN_RANGE_FULL:
                skip("区切りの途中で幅を出入りした（1時間ごとの記録では時間の割合が粗い）" if inside
                     else "区切りのあいだ幅の外にいた")
                res["rows"].append({**row, "used": False})
                continue
            if actual <= 0:
                skip("ずっと幅の中なのに実際が 0（配られなかった）")
                res["rows"].append({**row, "used": False})
                continue
            row.update({"used": True, "err_a": pa / actual - 1, "err_b": pb / actual - 1})
            res["rows"].append(row)
    return res


def _signature(conn: sqlite3.Connection) -> tuple[Any, ...]:
    """保存した記録が変わったか（変わっていなければ前の計算を使う）。"""
    return tuple(conn.execute(
        "SELECT (SELECT MAX(ts) FROM merkl_reward_snaps), (SELECT COUNT(*) FROM merkl_reward_snaps), "
        "(SELECT MAX(checked_at) FROM pool_state_snaps), (SELECT SUM(events) FROM pool_liq_progress), "
        "(SELECT MAX(done_ts) FROM pool_liq_progress)").fetchone())


_CACHE: dict[str, Any] = {}


def check(conn: sqlite3.Connection, pms: dict[int, str], back_shares: dict[str, float | None] | None = None
          ) -> dict[str, Any]:
    """幅に配る v4 のキャンペーンすべての A / B / 実際。pms = チェーン番号 → 公式の PositionManager（小文字）。
    back_shares = 今の答え合わせ（merkl_check）の、キャンペーンごとの逆算した分母の割合（まん中）。"""
    need = ("merkl_reward_snaps", "pool_state_snaps", "merkl_campaigns", "pool_liq_progress")
    if not all(_table(conn, t) for t in need):
        return {"present": False, "campaigns": [], "overall": stats([]), "by_weight": {}, "skipped": {}, "unusable": {}}
    sig = (_signature(conn), tuple(sorted(pms.items())))
    if _CACHE.get("sig") == sig:
        res = _CACHE["res"]
    else:
        books: dict[tuple[int, str], Book] = {}
        denoms: dict[tuple[int, str, str], dict[str, Any]] = {}
        camps, errors = [], []
        for c in conn.execute("SELECT * FROM merkl_campaigns WHERE type='UNISWAP_V4' AND settings_json LIKE '%weightFees%' "
                              "AND campaign_id IN (SELECT DISTINCT campaign_id FROM merkl_reward_snaps)").fetchall():
            if int(c["chain_id"]) not in pms:
                continue
            try:
                r = check_campaign(conn, c, books, denoms, pms.get(int(c["chain_id"])))
            except Exception as exc:  # noqa: BLE001  1つのキャンペーンの不良で、全体を止めない
                errors.append({"campaign_id": c["campaign_id"], "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            r["opportunity_id"] = c["opportunity_id"]
            camps.append(r)
        res = {"camps": camps, "errors": errors}
        _CACHE.update(sig=sig, res=res)
    return summarize(conn, res["camps"], res["errors"], back_shares or {})


def _tvl(conn: sqlite3.Connection, opp: str | None) -> float | None:
    if not opp or not _table(conn, "merkl_opportunity_snaps"):
        return None
    r = conn.execute("SELECT tvl FROM merkl_opportunity_snaps WHERE opportunity_id=? AND tvl IS NOT NULL "
                     "ORDER BY ts DESC LIMIT 1", (opp,)).fetchone()
    return float(r[0]) if r and r[0] else None


def summarize(conn: sqlite3.Connection, camps: list[dict[str, Any]], errors: list[dict[str, Any]],
              back_shares: dict[str, float | None]) -> dict[str, Any]:
    used = [r for c in camps for r in c["rows"] if r.get("used")]
    skipped: dict[str, int] = {}
    unusable: dict[str, int] = {}
    out_classes: dict[str, int] = {}
    per: list[dict[str, Any]] = []
    for c in camps:
        for k, v in c["skipped"].items():
            skipped[k] = skipped.get(k, 0) + v
        for k, v in c["unusable"].items():
            unusable[k] = unusable.get(k, 0) + v
        out_classes[c["out_class"]] = out_classes.get(c["out_class"], 0) + 1
        rows = [r for r in c["rows"] if r.get("used")]
        ivs = c["intervals"]
        ratios = [i["ratio"] for i in ivs if i["ratio"] is not None and i["l_ok"]]
        bc = [r["share_backcalc"] for r in rows if r.get("share_backcalc") is not None]
        sc = [r["share_chain"] for r in rows if r.get("share_chain") is not None]
        ratio = statistics.median(ratios) if ratios else None
        tvl = _tvl(conn, c.get("opportunity_id"))
        per.append({"campaign_id": c["campaign_id"], "pair": c["pair"], "chain_id": c["chain_id"],
                    "out_class": c["out_class"], "out_label": OUT_LABEL[c["out_class"]], "weights": c["weights"],
                    "main_weight": c["main_weight"], "intervals": len(ivs),
                    "intervals_l_ok": sum(1 for i in ivs if i["l_ok"]), "pairs": len(rows),
                    "positions": c["positions"], "unusable_positions": sum(c["unusable"].values()),
                    "n_all": statistics.median([i["n_all"] for i in ivs]) if ivs else None,
                    "n_in": statistics.median([i["n_in"] for i in ivs]) if ivs else None,
                    "a_den_v": statistics.median([i["a_den_v"] for i in ivs]) if ivs else None,
                    "b_den_v": statistics.median([i["b_den_v"] for i in ivs]) if ivs else None,
                    "ratio": ratio, "share_backcalc": statistics.median(bc) if bc else None,
                    "share_chain": statistics.median(sc) if sc else None,
                    "share_backcalc_now": back_shares.get(c["campaign_id"]),
                    "err_a": _q([r["err_a"] for r in rows], 0.5), "err_b": _q([r["err_b"] for r in rows], 0.5),
                    "tvl": tvl, "skipped": c["skipped"], "unusable": c["unusable"]})
    by_weight = {name: stats([r for r in used if next(c for c in camps if c["campaign_id"] == r["campaign_id"])
                              ["main_weight"] == name]) for name in ("手数料が中心", "コインが中心")}
    # 重みの種類ごとに、B が A と違う値になったか（手数料の分は A と B が同じ。コインの分は分母が違う）
    parts = {"fee": {"label": "手数料", "b": "A と同じ（手数料は幅の中の預け方だけに入るので、分母はもともと幅の中だけ）",
                     "share_of_pred_a": _share(used, "c_fee")},
             "token0": {"label": "token0", "b": f"計算できた組 {sum(1 for r in used if r['t0b'] > 0)} 件",
                        "share_of_pred_a": _share(used, "c_t0a")},
             "token1": {"label": "token1", "b": f"計算できた組 {sum(1 for r in used if r['t1b'] > 0)} 件",
                        "share_of_pred_a": _share(used, "c_t1a")}}
    cap = []
    for p in per:
        if p["tvl"] and p["ratio"]:
            b_den = p["tvl"] * p["ratio"]
            cap.append({"pair": p["pair"], "tvl": p["tvl"], "ratio": p["ratio"], "b_den": b_den,
                        "a": {x: x / (p["tvl"] + x) for x in OWN_USD}, "b": {x: x / (b_den + x) for x in OWN_USD}})
    return {"present": True, "campaigns": sorted(per, key=lambda x: -x["pairs"]), "overall": stats(used),
            "by_weight": by_weight, "parts": parts, "skipped": skipped, "unusable": unusable,
            "out_classes": {OUT_LABEL[k]: v for k, v in out_classes.items()}, "cap": cap, "errors": errors,
            "own_usd": list(OWN_USD)}


def _share(rows: list[dict[str, Any]], key: str) -> float | None:
    """A の見込みのうち、その重みの分が占める割合（まん中）。"""
    xs = []
    for r in rows:
        c = r["pred_a"]
        if c > 0:
            xs.append(r[key] / c)
    return statistics.median(xs) if xs else None


__all__ = ["check", "check_campaign", "Book", "stats", "out_class", "weights", "OWN_USD"]
