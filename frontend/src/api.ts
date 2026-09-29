import { useEffect, useState } from "react";

export type Signal = "green" | "yellow" | "red";

export interface Warn { code: string; level: "major" | "minor"; message_ja: string }

export interface Breakdown {
  income: number; direction: number; gamma: number; hedge: number; haircut: number; other: number;
  net: number; hedge_gap: number; core: number; luck_ratio: number; lucky: boolean;
  days?: number; since?: string;
}

export interface PoolRow {
  pool_id: string; pair: string; venue_id: string; signal: Signal;
  net_daily_pct: number | null; net_daily_pct_lp: number | null; best_r: number | null;
  reason_ja: string; is_stock_pair?: number; has_perp: number | null; tvl_usd: number | null;
  warnings?: Warn[]; mode?: string | null;
}

export interface Home {
  mode: string; summary: string; counts: Record<Signal, number>; judge_basis: string;
  scored_at: string | null; greens: PoolRow[]; near: PoolRow[];
  market: {
    us_open: boolean; gas_usd_per_tx: number | null;
    reward_tokens: { venue_id: string; symbol: string; price_usd: number | null; change_24h: number | null }[];
  };
  collection: { venue_id: string; last_ok_at: string | null; stale: boolean; gaps_7d: { start_slot: string; end_slot: string }[] }[];
}

export interface Point { ts: string; v: number | null }

export interface Venue {
  venue_id: string; name: string; audited: boolean | null; launch_date: string | null; age_days: number | null;
  warnings: { code: string; level: string; title_ja?: string; key?: string }[];
  unverified_contracts: string[];
  reward_token: { symbol: string | null; price_usd: number | null; change_24h: number | null; change_7d: number | null; sparkline: Point[] };
  tvl: Point[];
  conditions: { code: string; state: "ok" | "warn" | "bad" | "unknown"; text: string }[];
}

export interface RangeRow {
  r_pct: number; net: number; net_pct: number; income: number; mode: string;
  gamma: number; rebalance: number; hedge: number; haircut: number; direction_risk: number;
  in_range_ratio: number; in_range_ratio_hold: number; rebalances_per_day: number;
  pool_inrange_usd?: number | null; staked_inrange_usd?: number | null; share_staked?: number | null;
}

export interface HistoryPoint {
  ts: string; signal: Signal; net_daily_pct: number | null; net_usd: number | null;
  reward_usd_day: number | null; staked_inrange_usd: number | null; reward_token_usd: number | null; us_open: boolean;
}

export interface Bar { ts: string; jst: string; net_usd: number | null; ma24_usd: number | null; us_open: boolean }

export interface PoolDetail {
  score: PoolRow & {
    ts: string; in_range_ratio: number | null; in_range_ratio_hold: number | null; sigma_pair: number | null;
    vol_source: string | null; is_stock_pair: number; address: string;
    details: { inputs?: Record<string, any>; ranges?: RangeRow[] };
  };
  price: { price: number; tick: number; ts: string } | null;
  capital: { total: number; lp: number; margin: number; reserve: number };
  daily: {
    breakdown: Breakdown | null; labels: Record<string, string>;
    net_daily_pct: number | null; net_daily_pct_lp: number | null; judge_basis: string;
    apy_display: number | null; apy_net: number | null; apy_note: string; realized_note: string;
  };
  today: Breakdown | null;
  since_start: Breakdown | null;
  hourly: { bars: Bar[]; best: Bar | null; worst: Bar | null };
  assets: {
    estimated: boolean; capital: number; value: number; change_usd: number; change_pct: number;
    series: { ts: string; jst: string; value: number }[];
    parts: { lp: number; wallet: number; margin: number; unclaimed: number };
  };
  history: HistoryPoint[];
}

export function useApi<T>(path: string): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let alive = true;
    setError(null);
    fetch(path)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
        return r.json();
      })
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e.message ?? e)));
    return () => { alive = false; };
  }, [path, n]);
  return { data, error, reload: () => setN((x) => x + 1) };
}
