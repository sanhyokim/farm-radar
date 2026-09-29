"""円のレート（1ドル = 何円）。台帳に円建ての値段を書くために使う（SPEC 12.4章）。

出どころ: frankfurter（欧州中央銀行の参照レート。無料・キー不要。2026-09-29 オーナー決定）。
frankfurter.app は api.frankfurter.dev/v1 に転送されるので、転送先を直接使う（同じサービス）。
- 日付を指定すると、その日にレートがなければ（土日・祝日）直前の営業日のレートと、その日付が返る（2026-09-29 確認:
  2026-09-27（日）を聞くと date=2026-09-25 が返った）。台帳にはそのレートの日付も書く（オーナー指示）。
- 1日1回だけ聞き、fx_rates 表にためる。その日のレートがまだ出ていない（欧州の夕方に出る）ときは、
  直前の営業日の値を使い、6時間後にもう一度聞く。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .external.http import JsonGetter

log = logging.getLogger(__name__)

BASE_URL = "https://api.frankfurter.dev/v1"
SOURCE = "frankfurter(ECB)"
RETRY_HOURS = 6


@dataclass(frozen=True)
class FxRate:
    jpy_per_usd: float
    rate_date: str          # レートの日付（営業日）


class Frankfurter:
    def __init__(self, getter: JsonGetter | None = None):
        self.http = getter or JsonGetter(BASE_URL, max_retries=2, backoff_seconds=2.0, timeout=10.0)

    def usd_jpy(self, day: str) -> FxRate:
        data = self.http.get(day, {"base": "USD", "symbols": "JPY"})
        return FxRate(float(data["rates"]["JPY"]), str(data["date"]))


def rate_for(conn: sqlite3.Connection, when: datetime, source: Frankfurter | None = None,
             now: datetime | None = None) -> FxRate | None:
    """when の日に使う円のレート。取れなければ None（あとで埋める）。"""
    day = when.astimezone(UTC).strftime("%Y-%m-%d")
    now = now or datetime.now(UTC)
    row = conn.execute("SELECT * FROM fx_rates WHERE day=?", (day,)).fetchone()
    if row is not None:
        fresh = row["rate_date"] == day or now.strftime("%Y-%m-%d") != day or \
            now - datetime.fromisoformat(row["fetched_at"]) < timedelta(hours=RETRY_HOURS)
        if fresh:
            return FxRate(row["jpy_per_usd"], row["rate_date"])
    try:
        got = (source or Frankfurter()).usd_jpy(day)
    except Exception as exc:
        log.warning("fx rate fetch failed", extra={"data": {"day": day, "error": str(exc)}})
        return FxRate(row["jpy_per_usd"], row["rate_date"]) if row is not None else None
    conn.execute("INSERT OR REPLACE INTO fx_rates(day, jpy_per_usd, rate_date, source, fetched_at) VALUES (?,?,?,?,?)",
                 (day, got.jpy_per_usd, got.rate_date, SOURCE, now.isoformat(timespec="seconds")))
    conn.commit()
    return got


def fill_ledger_jpy(conn: sqlite3.Connection, source: Frankfurter | None = None, limit: int = 500) -> int:
    """円の値段がまだ入っていない台帳の行を埋める（レートが取れなかったときの後追い）。"""
    rows = conn.execute("SELECT id, ts, price_usd FROM ledger WHERE fx_rate IS NULL ORDER BY ts LIMIT ?",
                        (limit,)).fetchall()
    n = 0
    for r in rows:
        fx = rate_for(conn, datetime.fromisoformat(r["ts"]), source)
        if fx is None:
            break
        conn.execute("UPDATE ledger SET fx_rate=?, fx_date=?, price_jpy=? WHERE id=?",
                     (fx.jpy_per_usd, fx.rate_date,
                      r["price_usd"] * fx.jpy_per_usd if r["price_usd"] is not None else None, r["id"]))
        n += 1
    conn.commit()
    return n
