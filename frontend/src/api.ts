import { useEffect, useState } from "react";

export type Signal = "green" | "yellow" | "red";

export interface Warn { code: string; level: "major" | "minor"; message_ja: string }

export interface Breakdown {
  income: number; direction: number; gamma: number; hedge: number; haircut: number; other: number;
  net: number; hedge_gap: number; core: number; luck_ratio: number; lucky: boolean;
  days?: number; since?: string;
}

export interface PoolRow {
  pool_id: string; pair: string; venue_id: string; venue_name?: string | null; signal: Signal;
  net_daily_pct: number | null; net_daily_pct_lp: number | null; best_r: number | null;
  reason_ja: string; is_stock_pair?: number; has_perp: number | null; tvl_usd: number | null;
  warnings?: Warn[]; mode?: string | null;
  hedge_info?: HedgeInfo; range_prices?: RangePrices | null;
  /** 次の切り替え（木曜 9:00 JST）と「来週ボーナスがなくなることがある」注意（2026-09-30 オーナー追加） */
  epoch_flip?: EpochFlip | null;
}

/** 運営が手で足したボーナス（プール全体・今のエポック）。判定には入れない（2026-09-30 オーナー条件4） */
export interface ManualBonus {
  symbol: string | null;
  amount: number;
  usd: number | null;
  epoch_total: number | null;
  regular: number | null;
  usd_day_spread: number | null;
  your_extra_usd_day: number | null;
  epoch_start: string | null;
}

export interface EpochFlip { at: string; note_ja: string; why_ja?: string | null }

/** 保険あり/なしと、使うヘッジ先の名前（2026-09-29 オーナー追加） */
export interface HedgeInfo { has: boolean; venues: string[]; tokens: Record<string, string | null>; label: string }

/** 最適レンジの実際の値段の範囲 */
export interface RangePrices { kind: "usd" | "ratio"; symbol: string; quote?: string; now: number; low: number; high: number;
  usd_now?: number; usd_low?: number; usd_high?: number }

/** 両替のずれと、費用に含まれる額 */
export interface SwapCosts {
  trade_usd: number; slippage_pct: number; source: string | null; fee_pct: number; swap_usd: number;
  swap_fee: number; slippage: number; gas: number; hedge_fee: number; open_total: number; rebalance_total: number;
}

export interface Home {
  mode: string; summary: string; counts: Record<Signal, number>; judge_basis: string;
  scored_at: string | null; greens: PoolRow[]; near: PoolRow[];
  market: {
    us_open: boolean; gas_usd_per_tx: number | null;
    us_day?: { ny_date: string; trading_day: boolean; holiday: string | null; weekend: boolean; early_close: string | null;
      calendar_covered: boolean };
    reward_tokens: { venue_id: string; symbol: string; price_usd: number | null; change_24h: number | null }[];
  };
  collection: { venue_id: string; name?: string; observe?: boolean; last_ok_at: string | null; stale: boolean;
    gaps_7d: { start_slot: string; end_slot: string }[] }[];
}

export interface Point { ts: string; v: number | null }

export interface Venue {
  venue_id: string; name: string; audited: boolean | null; launch_date: string | null; age_days: number | null;
  practice?: boolean;   // false なら観察だけ（練習と評価に入れない。M6）
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

export interface Reports {
  reports: { day: string; ts: string; body_ja: string; sent_at: string | null }[];
  learning: { ts: string; pool_id: string | null; title: string; body_ja: string; topic: string }[];
  telegram: { configured: boolean; last_alert_sent_at: string | null };
  schedule_jst: string;
}

export interface PoolDetail {
  score: PoolRow & {
    ts: string; in_range_ratio: number | null; in_range_ratio_hold: number | null; sigma_pair: number | null;
    vol_source: string | null; is_stock_pair: number; address: string;
    details: { inputs?: Record<string, any> & { manual_bonus?: ManualBonus | null }; ranges?: RangeRow[] };
  };
  venue?: { id: string; name: string | null; practice: boolean; practice_note: string };
  price: { price: number; tick: number; ts: string } | null;
  capital: { total: number; lp: number; margin: number; reserve: number };
  daily: {
    breakdown: Breakdown | null; labels: Record<string, string>;
    net_daily_pct: number | null; net_daily_pct_lp: number | null; judge_basis: string;
    apy_display: number | null; apy_net: number | null; apy_note: string; realized_note: string;
  };
  swap?: SwapCosts | null;
  sell_now: {
    hours: number; best_r: number; net_daily_pct: number; net_usd: number; income: number; haircut: number;
    mode: string; hold_net_daily_pct: number | null; hold_haircut: number | null; diff_pct: number | null; note: string;
  } | null;
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

export interface PaperCard {
  id: number; pool_id: string; pair: string; venue_id: string; status: "open" | "closed";
  opened_at: string; closed_at: string | null; close_reason: string | null; last_ts: string;
  capital: number; c_lp: number; mode: string; r_pct: number; lower: number; upper: number; price: number;
  price_open: number; in_range: boolean; to_lower_pct: number; to_upper_pct: number;
  amounts: Record<string, number>; value: number; change_usd: number; change_pct: number; reward_24h_usd: number;
  predicted_daily_pct: number | null; actual_daily_pct: number | null; days: number; open_cost_usd: number;
  started_red: boolean; signal_open: Signal; red_label: string | null;
  hedges: { symbol: string; perp: string; size: number; entry: number }[]; estimated_rows: number;
  // M5b
  close_reason_ja: string | null; reward_hours: number; hours: number;
  actual_daily_pct_with_cost: number | null; actual_state: "short" | "ok"; actual_min_hours: number;
  compare_enabled: boolean; compare_min_hours: number;
  payback_total_hours: number | null; payback_left_hours: number | null;
  rebalances: number; cautions: string[]; skipped: string[];
  rebalance_cost: number;
  swap?: { slippage_pct_now: number | null; rebalance_slippage_total: number; rebalance_slippage_next: number | null;
    open: { swap_usd: number; slippage_pct: number; slippage: number; swap_fee?: number; gas?: number; hedge_fee?: number;
      total?: number; estimated?: boolean } | null }; close_cost_usd: number | null;
}

export interface Watch {
  contracts: { address: string; label: string; checked_at: string | null; ok: string[]; unconfirmed: string[];
    changed_at: string | null }[];
  usdg: { ts: string; price: number; source: string } | null;
}

export interface RiskEvent {
  id: number; ts: string; position_id: number | null; level: "caution" | "rebalance" | "exit" | "emergency" | "info";
  level_ja: string; kind: string; message: string; action: string; action_ja: string;
}

export interface Paper {
  mode: string; enabled: boolean; stopped: boolean; capital: number;
  limits: { position_usd?: number; total_usd?: number; per_venue_share?: number; trades_per_day?: number };
  venue_cap_usd: number | null; open_total_usd: number; how_to_enable: string;
  open: PaperCard[]; closed: PaperCard[];
  stopped_reason: string | null; stopped_since: string | null; events: RiskEvent[];
  risk: { level: RiskEvent["level"]; level_ja: string; rule: string; action: string }[];
  watch: Watch;
  timeline: TimelineItem[]; outlook: Outlook | null; ledger_months: string[];
}

export interface TimelineItem {
  key: string; ts: string; type: "review" | "event" | "open" | "close"; level: string;
  title: string; body: string; position_id?: number | null;
}

export interface Evaluation {
  state: "not_started" | "running" | "stopped" | "finished"; mode: string; evaluation_days: number;
  started_at?: string; ends_at?: string; elapsed_hours?: number; left_hours?: number;
  coverage?: { venue_id: string; expected: number; ok: number; ratio: number | null; missing_hours: number }[];
  positions?: number; observed_hours?: number; estimated_hours?: number;
  compare?: { key: string; label: string; predicted: number | null; actual: number | null }[];
  predicted_net_day?: number | null; actual_net_day?: number | null; gap_pct?: number | null;
  hold_net_day?: number | null; sell_net_day?: number | null; closer?: "hold" | "sell" | null;
  events?: { level: string; level_ja: string; n: number }[]; note?: string;
  predicted_sell_net_day?: number | null; disclaimer?: string;
  criteria?: {
    min_coverage_pct: number; coverage_pct: number | null; coverage_ok: boolean; day_gap_pct: number;
    day_gap_capital_pct: number; pass_days_pct: number; days: number; done_days: number;
    hold: EvalVerdict; sell: EvalVerdict;
  };
  days?: { day: number; start: string; done: boolean; hours: number; capital: number; predicted: number | null;
    predicted_sell: number | null; hold: number | null; sell: number | null; hold_ok: boolean; sell_ok: boolean }[];
}

export interface EvalVerdict { ok_days: number; need_days: number; result: "running" | "stopped" | "pass" | "fail" }

export interface Outlook {
  value_now: number; capital: number; daily_usd: number | null; daily_pct: number | null;
  daily_low_usd: number | null; daily_low_pct: number | null;
  rows: { label: string; days: number; value: number; low: number }[];
  hours: number; short: boolean; min_hours: number; conservative_pct: number; note: string;
}

export interface PaperCalendar {
  month: string; first_weekday: number; days_in_month: number; today: string;
  days: { day: string; net: number; income: number; estimated: boolean }[];
  total: number; prev: string | null; next: string | null;
}

export interface PaperDetail extends PaperCard {
  total: Breakdown; realized: number; unrealized: number; today: Breakdown | null; landing: number | null;
  since_start: Breakdown | null; hourly: { bars: Bar[]; best: Bar | null; worst: Bar | null };
  series: { ts: string; jst: string; value: number }[];
  compare: {
    rows: { key: keyof Breakdown; label: string; predicted: number; actual: number }[];
    predicted_net: number; actual_net: number; score_ts: string; short: boolean; note: string;
    enabled: boolean; min_hours: number;
  } | null;
  sell_now: { hours: number; hold_haircut: number; sell_haircut: number; hold_net: number; sell_net: number;
    predicted_hold_pct: number | null; predicted_sell_pct: number | null };
  ledger: { ts: string; kind: string; token: string; amount: number; price_usd: number | null; value_usd: number | null;
    price_jpy: number | null; fx_rate: number | null; fx_date: string | null; note: string }[];
  labels: Record<string, string>; apy_note: string; apy_display: number; apy_net: number;
  red_note: string | null; notes: string[]; events: RiskEvent[];
  outlook: Outlook | null; timeline: TimelineItem[];
}

/** POST して JSON を返す。失敗したらサーバーの日本語の理由を投げる。 */
export async function postApi<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
  return r.json();
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

/** ヘッジ先と担保の状態（SPEC 5.2.1章。読み取りのみ） */
export interface HedgeStatus {
  state: "ok" | "short" | "none" | "error" | "waiting"; state_ja: string; collateral_usd?: number | null;
  available_usd?: number | null; paper?: boolean; note?: string; read_at?: string;
  positions?: { symbol: string; size: number; value_usd: number | null }[];
}
export interface Hedges {
  mode: string; need_usd: number;
  venues: { hedge_id: string; name: string; address: string | null; markets: number; fees_at: string | null;
    max_taker_pct: number | null; need_usd: number; status: HedgeStatus; real: HedgeStatus | null }[];
}

/** 候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1） */
export type DiscoveryDecision = "study" | "hold" | "skip";
export interface HedgeMatch { ok: boolean; stable: boolean; tokens: string[]; venues: string[] }
export interface DiscoveryPool {
  pool: string; symbol: string; meta?: string | null; tvl_usd: number; apy_reward: number | null; apy_base: number | null;
  days: number | null; outlier?: boolean; hedge: HedgeMatch;
  project?: string; venue_name?: string; chain?: string;
  reward_token_info?: { symbol: string | null; change_7d_pct: number | null }[];
}
export interface DiscoveryVenue {
  key: string; source: "defillama" | "geckoterminal"; name: string; chain: string; url?: string | null;
  tvl_usd?: number; change_7d_pct?: number | null; multi_chain?: boolean; listed_at?: string | null; age_days?: number | null;
  audit_links?: string[]; reward_apr_median?: number | null; reward_pools_tvl_usd?: number | null;
  weekly_reward_usd?: number | null; weekly_ratio_pct?: number | null; reward_pools_n?: number;
  top_pools?: DiscoveryPool[]; hedge_pools?: string[];
  reward_token_info?: { symbol: string | null; price: number | null; change_7d_pct: number | null }[];
  reward_change_7d_pct?: number | null;
  new?: boolean; first_seen?: string; decision: DiscoveryDecision | null; decided_at?: string | null; not_in_latest?: boolean;
  // GeckoTerminal の新しい DEX
  network?: string; dex_id?: string; watched?: boolean; tvl_top_usd?: number;
  pools?: { name: string; tvl_usd: number | null; volume_24h_usd: number | null; created_at: string | null; hedge: HedgeMatch }[];
}
export interface Discovery {
  schedule_ja: string; next_run: string; last_ok_at: string | null; refresh_block: string | null;
  last_run: { started_at: string; finished_at: string | null; status: string; error: string | null; trigger: string } | null;
  chains: string[]; criteria_ja: string;
  venues: DiscoveryVenue[]; new_dexes: DiscoveryVenue[]; gt_dex_count: number; new_pools: DiscoveryPool[];
  decided_elsewhere: DiscoveryVenue[];
}
