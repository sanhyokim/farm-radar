"""N5c: Merkl の「実際に配った額」と、探すの見込み（コイン0・コイン1の分母 = 預けられたお金の全部）の答え合わせ。

docs/n5-plan-2026-10-03.md の 2-3（判断② A: これからの記録で答え合わせ）。読むのは feeds の保存だけ（読み取りだけ）。

1回の区切り = Merkl のキャンペーン全体の「配った累計」が変わってから、次に変わるまで（約2時間）。
累計 = 確定した額（amount）＋まだ確定していない額（pending）。全体の累計は、預け方ごとの額と同じ時点で数える
（boundaries。2026-10-04 に直した。前は確定した額だけと /rewards/total を比べていて、時点がずれていた）。
- 実際: その区切りに、預け方 i がもらった額 d_i ÷ キャンペーン全体で配った額 D。
- 見込み（今の探すの式。13.1 の追加の決定 8 の A）:
  手数料の重み × (預け方の流動性 ÷ 今の値段のところの流動性) ＋ コインの重み × (預け方のドルの額 ÷ プールの預かり額)。
  幅の外にいる時間は 0（キャンペーンの設定 isOutOfRangeIncentivized が false のとき。Merkl の資料「In-Range positions only」）。
  区切りの中の1時間ごとのプールの状態（pool_state_snaps）で平均する。
- 比 = 実際 ÷ 見込み。1 に近ければ今の式どおり。1 より大きければ、今の式は控えめ（もらえる額を小さく見ている）。
  手数料の分は、Merkl の資料で分母が「今の値段のところの流動性」とはっきりしているので、比のずれはほぼコインの分の分母の違い。
- 分母の割合 = 見込みのコインの分 ÷ 実際のコインの分（実際から手数料の分の見込みを引いた残り）。
  「全員が分母」なら 1 に近く、「幅の中にある預け方だけが分母」なら、預かり額のうち幅の中にある割合に近くなる。
  全部の預け方を数えなくても、どちらの数え方に近いかがわかる（docs/n5-plan-2026-10-03.md 2-3 の 4〜5）。

使わない区切り: 区切りの前と後で預け方の量（流動性）が変わったもの（足した・減らした）、プールの状態が無いもの、
値段が分からないもの。合わなかったものは合わなかったまま数える（資料にない仕組みを推測で埋めない。絶対ルール4）。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import datetime
from typing import Any

from .feeds.positions import parse_reason

LIQ_SAME = 0.001             # 区切りの前後の流動性の違いがこれ以下なら「変わっていない」


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def amounts(liq: float, sqrt_p: float, tick_lower: int, tick_upper: int) -> tuple[float, float]:
    """流動性 liq の預け方の中身（最小単位の token0, token1）。sqrt_p = sqrtPriceX96 / 2^96。"""
    sa, sb = 1.0001 ** (tick_lower / 2), 1.0001 ** (tick_upper / 2)
    if sqrt_p <= sa:
        return liq * (sb - sa) / (sa * sb), 0.0
    if sqrt_p >= sb:
        return 0.0, liq * (sb - sa)
    return liq * (sb - sqrt_p) / (sqrt_p * sb), liq * (sqrt_p - sa)


def _price_usd(conn: sqlite3.Connection, coin: str | None, t0: int, t1: int) -> float | None:
    if not coin:
        return None
    r = conn.execute("SELECT AVG(price) FROM token_prices WHERE coin=? AND ts>=? AND ts<=?",
                     (coin, t0 - 3600, t1 + 3600)).fetchone()
    if r and r[0]:
        return float(r[0])
    r = conn.execute("SELECT price FROM token_prices WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                     (coin, t1)).fetchone()
    return float(r[0]) if r else None


def _amount_at(conn: sqlite3.Connection, cid: str, reason: str, ts: str) -> int | None:
    """その時刻までにもらった累計（受け取る人ごとの最後の値の合計。預け方を人に渡すと受け取る人が変わるため）。

    累計 = 確定した額（amount）＋まだ確定していない額（pending）。2026-10-04 に直した: 前は amount だけで、
    キャンペーン全体の額（amount と pending の合計）と時点がずれていた。確定（配る木の更新）が区切りの中にあると、
    実際が約2倍に見えていた（作業場所の試験用の値: 07:57〜12:07 で、預け方の増え方の合計 ÷ 全体の増え方 = 1.9）。"""
    rows = conn.execute("SELECT s.amount_raw, s.pending_raw FROM merkl_reward_snaps s JOIN (SELECT recipient, MAX(ts) AS ts "
                        "FROM merkl_reward_snaps WHERE campaign_id=? AND reason=? AND ts<=? GROUP BY recipient) m "
                        "ON s.recipient=m.recipient AND s.ts=m.ts WHERE s.campaign_id=? AND s.reason=?",
                        (cid, reason, ts, cid, reason)).fetchall()
    vals = [int(r[0]) + int(r[1] or 0) for r in rows if r[0] is not None]
    return sum(vals) if vals else None


def boundaries(conn: sqlite3.Connection, cid: str, page_rows: int = 100) -> tuple[list[tuple[str, int]], str | None]:
    """区切りの境目: キャンペーン全体の累計（全部の預け方の amount ＋ pending）を、預け方ごとの額と同じ時点で数えられた時刻。

    - 全部のページを読んだ記録（merkl_reward_sums の complete=1）があれば、その時刻と合計。
    - それより前（1ページ = 先頭100行だけを読んでいたころ）は、キャンペーンの行が100行に届いていなければ全部の行が
      読めているので、記録した行から合計を出す。届いていれば、全体の増え方が数えられないので使わない（理由を返す）。
    戻り値 = ([(時刻, 合計)], 使えない期間の理由)。"""
    sums = []
    if _table(conn, "merkl_reward_sums"):
        sums = conn.execute("SELECT ts, sum_raw, complete FROM merkl_reward_sums WHERE campaign_id=? ORDER BY ts",
                            (cid,)).fetchall()
    first = sums[0][0] if sums else None
    out: list[tuple[str, int]] = []
    why = None
    cond, args = ("AND ts < ?", (cid, first)) if first else ("", (cid,))
    n_rows = conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT recipient, reason FROM merkl_reward_snaps "
                          f"WHERE campaign_id=? {cond})", args).fetchone()[0]
    times = [r[0] for r in conn.execute(f"SELECT DISTINCT ts FROM merkl_reward_snaps WHERE campaign_id=? {cond} "
                                        f"ORDER BY ts", args)]
    if times and n_rows >= page_rows:
        why = "配った額を先頭100行までしか記録していなかった期間（全体の増え方を数えられない）"
    elif times:
        for t in times:
            # 大きな整数（18桁を超える）は SQLite の足し算だと小数になるので、Python で足す
            rows = conn.execute("SELECT s.amount_raw, s.pending_raw FROM merkl_reward_snaps s JOIN (SELECT recipient, reason, "
                                "MAX(ts) AS ts FROM merkl_reward_snaps WHERE campaign_id=? AND ts<=? GROUP BY recipient, reason) m "
                                "ON s.recipient=m.recipient AND s.reason=m.reason AND s.ts=m.ts WHERE s.campaign_id=?",
                                (cid, t, cid)).fetchall()
            out.append((t, sum(int(r[0] or 0) + int(r[1] or 0) for r in rows)))
    out += [(r[0], int(r[1])) for r in sums if int(r[2]) == 1]
    return out, why


def _position(conn: sqlite3.Connection, chain_id: int, token_id: int, ts: str, before: bool) -> sqlite3.Row | None:
    op, order = ("<=", "DESC") if before else (">=", "ASC")
    return conn.execute(f"SELECT * FROM merkl_position_snaps WHERE chain_id=? AND token_id=? AND ts {op} ? "
                        f"AND liquidity IS NOT NULL AND error IS NULL ORDER BY ts {order} LIMIT 1",
                        (chain_id, token_id, ts)).fetchone()


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def check_campaign(conn: sqlite3.Connection, c: sqlite3.Row, coins_key: str | None) -> dict[str, Any]:
    """1つのキャンペーンの区切りごと・預け方ごとの「実際 ÷ 見込み」。"""
    st = json.loads(c["settings_json"] or "{}")
    pool = str(st.get("poolId") or "").lower()
    w_fee = float(st.get("weightFees") or 0) / 10000
    w_tok = (float(st.get("weightToken0") or 0) + float(st.get("weightToken1") or 0)) / 10000
    out_ok = bool(st.get("isOutOfRangeIncentivized"))
    d0, d1 = int(st.get("decimalsCurrency0") or 18), int(st.get("decimalsCurrency1") or 18)
    coin1 = f"{coins_key}:{str(st.get('currency1') or '').lower()}" if coins_key and st.get("currency1") else None
    totals, why_not = boundaries(conn, c["campaign_id"])
    rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    # 幅と量を読んだ預け方だけ（読むのは配った額の多い順に PER_CAMPAIGN まで。feeds/positions.py）
    measured = {int(r[0]) for r in conn.execute("SELECT DISTINCT token_id FROM merkl_position_snaps WHERE chain_id=? "
                                                "AND pool_id=?", (c["chain_id"], pool))}

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    if why_not:
        skip(why_not)

    reasons: list[tuple[str, int]] = []
    for (reason,) in conn.execute("SELECT DISTINCT reason FROM merkl_reward_snaps WHERE campaign_id=? "
                                  "AND reason LIKE 'UNISWAP\\_V4\\_%' ESCAPE '\\'", (c["campaign_id"],)):
        parsed = parse_reason(reason)
        if parsed is None:
            # 末尾が番号でなく 32バイトの印（0x…）の預け方がある（PositionManager を使わない預け方）。
            # 番号として読もうとして /api/trial/records 全体が 500 になっていた（2026-10-04）。使わずに1つ1回数える
            skip("預け方の番号の形でない（PositionManager を使わない預け方）")
        elif parsed[1] in measured:
            reasons.append((reason, parsed[1]))

    for (a_ts, a_sum), (b_ts, b_sum) in zip(totals, totals[1:]):
        total_d = b_sum - a_sum
        if total_d <= 0:
            continue
        ta, tb = _ts(a_ts), _ts(b_ts)
        a, b = {"ts": a_ts}, {"ts": b_ts}
        states = conn.execute("SELECT tick, liquidity, sqrt_price_x96 FROM pool_state_snaps WHERE chain_id=? AND pool_id=? "
                              "AND checked_at>? AND checked_at<=? AND liquidity IS NOT NULL AND sqrt_price_x96 IS NOT NULL",
                              (c["chain_id"], pool, a["ts"], b["ts"])).fetchall()
        if not states:
            skip("プールの状態がない")
            continue
        opp = conn.execute("SELECT AVG(tvl) FROM merkl_opportunity_snaps WHERE opportunity_id=? AND ts>? AND ts<=?",
                           (c["opportunity_id"], a["ts"], b["ts"])).fetchone()
        tvl = float(opp[0]) if opp and opp[0] else None
        p1 = _price_usd(conn, coin1, ta, tb)
        for reason, token_id in reasons:
            before, after = _amount_at(conn, c["campaign_id"], reason, a["ts"]), _amount_at(conn, c["campaign_id"], reason, b["ts"])
            if after is None:
                continue
            got = after - (before or 0)
            # 区切りの前（始まり以前）と後（終わり以後）の両方で読めた預け方だけ。片方しかないと、間に量が変わったか分からない
            # （前は、読み始める前の区切りにも、あとで読んだ量を当てはめていた）
            pa = _position(conn, c["chain_id"], token_id, a["ts"], True)
            pb = _position(conn, c["chain_id"], token_id, b["ts"], False)
            if pa is None or pb is None or pa["tick_lower"] is None:
                skip("預け方の幅と量がない")
                continue
            la, lb = float(pa["liquidity"]), float(pb["liquidity"])
            if la <= 0 or abs(la - lb) / la > LIQ_SAME:
                skip("区切りの間に預け方の量が変わった")
                continue
            fee_parts, tok_parts, inside = [], [], 0
            for s in states:
                tick, l_act, sq = int(s["tick"]), float(s["liquidity"]), int(s["sqrt_price_x96"]) / 2 ** 96
                in_range = pa["tick_lower"] <= tick < pa["tick_upper"]
                inside += in_range
                if not in_range and not out_ok:
                    fee_parts.append(0.0)
                    tok_parts.append(0.0)
                    continue
                fee_parts.append(la / l_act if in_range and l_act > 0 else 0.0)
                if tvl and p1:
                    a0, a1 = amounts(la, sq, pa["tick_lower"], pa["tick_upper"])
                    price = sq * sq * 10 ** (d0 - d1)               # token0 1個あたりの token1
                    usd = (a0 / 10 ** d0 * price + a1 / 10 ** d1) * p1
                    tok_parts.append(usd / tvl)
                else:
                    tok_parts.append(math.nan)
            if any(math.isnan(x) for x in tok_parts):
                skip("値段か預かり額が分からない")
                continue
            w = w_fee + w_tok or 1.0
            fee_m, tok_m = statistics.fmean(fee_parts), statistics.fmean(tok_parts)
            pred = (w_fee * fee_m + w_tok * tok_m) / w
            actual = got / total_d
            # コインの分の分母が、預かり額の何割に見えるか（手数料の分は資料どおりと置いて、残りをコインの分とする）。
            # 1 に近い = 全員が分母（今の式）。1 より小さい = もっと少ない額が分母（例: 幅の中にある預け方だけ）
            tok_actual = (actual * w - w_fee * fee_m) / w_tok if w_tok > 0 else 0.0
            rows.append({"start": a["ts"], "end": b["ts"], "token_id": token_id, "actual": actual, "predicted": pred,
                         "ratio": actual / pred if pred > 0 else None, "in_range_share": inside / len(states),
                         "denominator_share": tok_m / tok_actual if tok_actual > 0 else None})
    full = [r for r in rows if r["ratio"] is not None and r["in_range_share"] >= 0.99]
    ratios = [r["ratio"] for r in full]
    shares = [r["denominator_share"] for r in full if r["denominator_share"] is not None]
    return {"campaign_id": c["campaign_id"], "chain_id": c["chain_id"], "pool_id": pool,
            "pair": f"{st.get('symbolCurrency0')}/{st.get('symbolCurrency1')}",
            "weights": {"fee": w_fee, "token": w_tok}, "out_of_range_paid": out_ok,
            "intervals": len({(r['start'], r['end']) for r in rows}), "pairs": len(rows),
            "pairs_in_range": len(ratios), "ratio_median": statistics.median(ratios) if ratios else None,
            # 1つの区切りで、預け方1つがもらう割合（見込み・実際のまん中。N5d の画面）
            "predicted_median": statistics.median([r["predicted"] for r in full]) if full else None,
            "actual_median": statistics.median([r["actual"] for r in full]) if full else None,
            "first": min((r["start"] for r in rows), default=None), "last": max((r["end"] for r in rows), default=None),
            "ratio_p25": _q(ratios, 0.25), "ratio_p75": _q(ratios, 0.75),
            "denominator_share_median": statistics.median(shares) if shares else None,
            "out_of_range_paid_share": _out_paid(rows), "skipped": skipped}


def _q(xs: list[float], q: float) -> float | None:
    if len(xs) < 4:
        return None
    return statistics.quantiles(xs, n=4)[0 if q == 0.25 else 2]


def _out_paid(rows: list[dict[str, Any]]) -> float | None:
    """ずっと幅の外にいた預け方の組のうち、なにかもらった割合（0 なら「幅の外には配らない」のとおり）。"""
    out = [r for r in rows if r["in_range_share"] == 0]
    return sum(1 for r in out if r["actual"] > 0) / len(out) if out else None


def check(conn: sqlite3.Connection, coins_keys: dict[int, str]) -> dict[str, Any]:
    """幅に配る v4 のキャンペーンすべての答え合わせ。coins_keys = チェーン番号 → DefiLlama の coins の名前。"""
    need = ("merkl_reward_snaps", "merkl_position_snaps", "pool_state_snaps", "merkl_campaigns")
    if not all(_table(conn, t) for t in need):
        return {"campaigns": [], "errors": [], "ratio_median": None, "pairs_in_range": 0}
    out, errors = [], []
    skipped: dict[str, int] = {}
    for c in conn.execute("SELECT * FROM merkl_campaigns WHERE type='UNISWAP_V4' AND settings_json LIKE '%weightFees%' "
                          "AND campaign_id IN (SELECT DISTINCT campaign_id FROM merkl_reward_snaps)").fetchall():
        if c["chain_id"] not in coins_keys:
            continue
        try:
            res = check_campaign(conn, c, coins_keys.get(c["chain_id"]))
        except Exception as exc:  # noqa: BLE001  1つのキャンペーンの不良で、全体（/api/trial/records）を止めない
            errors.append({"campaign_id": c["campaign_id"], "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        for k, v in res["skipped"].items():
            skipped[k] = skipped.get(k, 0) + v
        if res["pairs"]:
            out.append(res)
    all_ratios = [m for m in (x["ratio_median"] for x in out) if m is not None]
    all_shares = [m for m in (x["denominator_share_median"] for x in out) if m is not None]
    return {"campaigns": sorted(out, key=lambda x: -x["pairs_in_range"]), "errors": errors,
            "ratio_median": statistics.median(all_ratios) if all_ratios else None,
            "denominator_share_median": statistics.median(all_shares) if all_shares else None,
            "pairs_in_range": sum(x["pairs_in_range"] for x in out), "pairs": sum(x["pairs"] for x in out),
            "skipped": skipped}
