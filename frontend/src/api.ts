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
  /** ボーナスの見込みが仮定つきの推定である会場の一言（Alandale:「推定（1週間を7日で均等に配ると仮定）」。2026-09-30 オーナー追加） */
  reward_estimate_note?: string | null;
  /** 報酬トークンそのものを預けるプールの警告文（2026-09-30 オーナー追加） */
  reward_held?: string | null;
  /** 配布の終了日が分かる会場・プールだけ（「配布終了まであと○日」。2026-09-30 オーナー追加） */
  emission_end?: EmissionEnd | null;
  // 画面の見直し（2026-09-30 オーナー依頼 13・23・24。表示だけ）
  ts?: string;
  key_reason?: string | null;
  slippage_pct?: number | null;
  checks?: Check[];
  reason_parts?: { formula: string | null; main: string | null; notes: string[] };
  /** 直近48時間の純日利（ホームのカードの小さな線グラフ） */
  spark?: number[];
}

export interface Check { state: "ok" | "warn" | "bad" | "info"; text: string }

/** 今日やること（オーナー依頼 17）。危険 → 注意 → お知らせ の順 */
export interface Todo { level: "danger" | "attention" | "info"; title: string; action: string; to: string | null }

export interface HomePaper {
  enabled: boolean; stopped: boolean; text: string;
  positions: { id: number; pair: string; venue_id: string; value: number; change_usd: number; today_usd: number;
    reward_24h_usd: number; status: string; tone: "ok" | "attention"; hours: number; spark: number[]; started_red: boolean }[];
  total: { capital: number; value: number; change_usd: number; today_usd: number };
  hourly?: { bars: Bar[]; best: Bar | null; worst: Bar | null };
  /** 評価額の合計の推移（1時間ごと） */
  spark?: number[];
}

/** 評価の期間に閉じた建玉（2026-10-01 オーナー決定 C: 閉じた建玉はそこで記録を終え、評価は残りで続ける） */
export interface EvalClosed { id: number; pair: string; closed_at: string; reason: string | null; reason_ja: string | null }
/** 評価の建玉が全部閉じて中断したとき（合否は出さない） */
export interface EvalInterrupted { at: string; last_pair: string | null; reason_ja: string | null; message: string }

export interface EvalLight {
  state: "running" | "stopped" | "finished" | "interrupted"; started_at: string; ends_at: string; day: number; days: number;
  left_hours: number; coverage_pct: number | null; min_coverage_pct: number; coverage_ok: boolean;
  ok_days: number; need_days: number; marks: { day: number; state: "ok" | "ng" | "running" | "none"; flip: boolean }[];
  closed_positions?: EvalClosed[]; interrupted?: EvalInterrupted | null;
}

/** 左のメニューと見出し用の軽い状態 */
export interface Pulse { mode: string; stale: boolean; last_ok_at: string | null; scored_at: string | null; snapshot_minutes: number; now: string;
  /** 練習中のプール */
  practicing: string[];
  /** 次の木曜の切り替え */
  next_flip: string | null;
  /** false = 並べて動かす新しい版で、チェーンを読んでいない（一覧の保存だけ。SPEC 13.4） */
  chain_reads?: boolean }

/** 一覧の保存（N2a。SPEC 13.4） */
export interface FeedSource {
  id: string; label: string; cadence: string; cadence_ja: string;
  status: "ok" | "error" | "rate_limited" | "running" | "stuck" | "none"; status_ja: string; late: boolean;
  last_run_at: string | null; last_ok_at: string | null; items: number | null; new_today: number;
  gone_last: number | null; error: string | null;
}
export interface FeedNew {
  source: string; label: string; key: string; name: string | null; chain: string | null; first_seen: string;
  info: { protocol?: string; apr?: number | null; tvl?: number | null; daily_rewards?: number | null; status?: string;
    type?: string; action?: string; start?: number | null; end?: number | null; date?: string | null; url?: string;
    category?: string; chains?: string[] };
}
export interface FeedsStatus {
  enabled: boolean; text: string; started_at?: string | null; problem?: boolean; chain_reads?: boolean;
  sources: FeedSource[]; new: FeedNew[]; rate_limited_24h?: number; disk_bytes?: number;
  aero: { start: { jst: string; utc: string; source: string };
    articles: { slug: string; title: string; date: string | null; url: string; new: boolean; first_seen: string }[];
    addresses?: { name: string; first_seen: string; changed_at: string | null; url: string | null; new: boolean }[] };
}

/** 機会の一覧（N2b。SPEC 13.4）。金額はドル、1日あたり */
export interface OppVariant {
  hedge: boolean; amount: number; split: { pool: number; hedge_margin: number; reserve: number };
  income: number; points: boolean; gamma: number; rebalance: number; hedge_cost: number; haircut: number;
  direction: number; net: number; move_cost: number; stay_days: number; net_after_move: number; apr_pct: number;
  payback_days: number | null; in_range_ratio: number | null; range_pct: number | null;
  liquidity_share?: number | null;   // 幅に配るプールで、チェーンの記録の流動性から出した取り分（N3）
  sigma_pct?: number | null;         // 計算に使った1日の値動き（%。N4b）
  jumps_per_day?: number | null;     // 幅を飛び越える飛び（市場が閉まっていたあとなど）の1日あたりの回数（N4b）
}
export interface OppFlag { code: string; level: "exclude" | "warn" | "info"; text: string }
export type OppCase = { no_hedge: OppVariant | null; hedge: OppVariant | null };
export interface Opportunity {
  source: "chain" | "merkl"; chain: string | null; chain_name: string | null; venue: string; venue_name: string | null;
  key: string; name: string | null; tokens: string[]; tvl_usd: number | null; bonus_usd_per_day: number | null;
  bonus_token: string | null; shown_apr_pct: number | null; ends_at: string | null; days_left: number | null;
  observed_at: string | null; url: string | null;
  kind: "pool_range" | "pool_full" | "hold" | "other"; computable: boolean; reason: string | null;
  flags: OppFlag[]; excluded: boolean; unprotected: string[]; cap_usd: number | null; new_pool: boolean;
  calc: Record<string, { normal: OppCase; cautious: OppCase }>;
  best: OppVariant | null; above_target: boolean | null; over_cap: boolean;
  recommended?: boolean | null;   // 練習のおすすめ（狙い以上で、会場の見分けが確か。2026-10-02 23:15 JST）
  safety: Safety; venue_safety: Safety | null;
}
/** 危なさの点数（N4a。N2c の「安全度」を置きかえた。点が多いほど危ない。重みと区切りは仮） */
export interface SafetyPart { key: string; label: string; points: number; note: string }
export interface VenueIdentity {
  status: "verified" | "unchecked" | "hooked" | "unregistered" | "no_address" | "mismatch";
  venue_id: string | null; name: string | null; method: string | null; reason: string; source_url: string | null;
}
export interface Recommend { usd: number | null; pct: number | null; small_capital: boolean; note: string }
export interface Safety {
  level: "low" | "mid" | "high" | "very_high"; label: string; score: number; provisional: boolean;
  parts: SafetyPart[]; reasons: string[]; identity: VenueIdentity | null;
  uncertain_match?: boolean;   // 会場を契約の住所で見分けられていない
  recommend?: Recommend;       // 推奨金額（本番で1か所に置いてよい額の目安）
}
export interface OppCampaign {
  campaign_id: string; distribution_type: string | null; distribution_method: string | null; reward_symbol: string | null;
  reward_type: string | null; daily_rewards: number | null; apr: number | null; start_ts: number | null; end_ts: number | null;
  restricted: number | null; distribution_chain_id: number | null;
}
export interface OppDetailResp {
  computed_at: string; target_apr_pct: number; amount: number; amounts: number[]; item: Opportunity;
  campaigns: OppCampaign[]; practice: { available: boolean; pool_id: string | null; note: string | null };
}
/** 守る（N2c）: 置いている額と上限・損失ライン */
export interface GuardVenue {
  venue_id: string; name: string; chain: string | null; chain_name: string | null; practice: boolean; safety: Safety | null;
  placed_usd: number; positions: number; cap_usd: number | null; left_usd: number | null; used_frac: number | null;
}
export interface Guard {
  limits: { position_usd: number | null; total_usd: number | null; per_venue_share: number | null; venue_cap_usd: number | null; trades_per_day: number | null };
  placed_usd: number; total_left_usd: number | null; positions: number;
  venues: GuardVenue[];
  chains: { chain: string; name: string; placed_usd: number; venues: number; cap_usd: number | null; left_usd: number | null }[];
  pnl: { open_change_usd: number; today_usd: number };
  loss_line: { pct: number; line_usd: number | null; today_usd: number; state: "none" | "ok" | "near" | "hit"; near_frac: number; used_frac: number };
  loss_lines: LossLines; stages: StageRow[]; signals: RiskEvent[];
  hedges: { position_id: number; pair: string; margin_usd: number; status: HedgeMargin | null }[];
  notes: string[];
}
/** 損の線（N4b。3つの期間 × 3段階。数字は仮） */
export type LossLevel = "caution" | "no_new" | "stop";
export interface LossBreakdown { income: number; pool: number; bonus: number; hedge: number; costs: number; net: number }
export interface LossPeriod {
  period: "day" | "week" | "since_start"; label: string; since: string | null; base_usd: number; net_usd: number; pct: number | null;
  level: LossLevel | null; level_ja: string | null; lines: Record<LossLevel, { pct: number; usd: number | null; label: string }>;
  breakdown: LossBreakdown | null; main_cause: keyof LossBreakdown | null;
}
export interface LossLines { periods: LossPeriod[]; level: LossLevel | null; level_ja: string | null; period: string | null; provisional: boolean; note: string }
/** 早く出る4段階（N4b） */
export interface StageRow { stage: 1 | 2 | 3 | 4; label: string; auto: boolean; waits_for_gas: boolean; rules: string[] }
/** 保険の強制決済までの余裕（N4b） */
export interface HedgeMargin {
  rise_pct: number; margin_usd: number; hedge_pnl_usd: number; equity_usd: number; notional_usd: number; maintenance_usd: number;
  buffer_usd: number; buffer_initial_usd: number; buffer_frac: number | null; alert_frac: number; to_liquidation_pct: number;
  state: "ok" | "alert" | "liquidated"; mmf_from_lighter: boolean; add_to_restore_usd: number;
}
export interface HedgeTest {
  status: HedgeMargin; would_ja: string; message_ja: string | null; note: string;
  options: { add: { usd: number; note: string }; exit: { close_cost_usd: number | null } };
}
export interface OpportunitiesResp {
  computed_at: string; target_apr_pct: number; amount: number; amounts: number[];
  counts: { total: number; computed: number; listed: number; above_target: number; recommended?: number; uncertain_venue?: number; danger?: Partial<Record<Safety["level"], number>>; venue_verified?: number; excluded: number; not_computable: number };
  not_computable: [string, number][];
  settings: { max_pool_share: number; cautious_tvl_multiple: number; min_tvl_usd: number; stay_days: number;
    merkl_range_pct: number; hedge_withstand_rise_pct: number };
  items: Opportunity[];
}
export interface AppSettings { target_apr_pct: number; default_target_apr_pct: number; updated_at: string | null; min: number; max: number }

export interface EmissionEnd { at: string; days_left: number; soon: boolean; ended: boolean; source?: string | null }

/** 「予定とメモ」の1件（docs/plans.yaml。期限の7日前から soon。2026-09-30 オーナー追加） */
export interface PlanItem {
  key: string; group: string; title: string; text: string; source?: string | null;
  due: string | null; days_left: number | null; state: "soon" | "later" | "past" | null;
}

/** 調べたが実装していない会場（docs/plans.yaml。2026-09-30 オーナー追加） */
export interface SkippedVenue { key: string; name: string; chain?: string | null; reason: string; decided?: string | null; source?: string | null }

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
    /** この版ではチェーンを読まない（並べて動かす新しい版。SPEC 13.4） */
    off?: boolean;
    gaps_7d: { start_slot: string; end_slot: string }[] }[];
  /** 期限が7日以内の予定（2026-09-30 オーナー追加） */
  plans_soon?: PlanItem[];
  todo: Todo[]; paper: HomePaper; evaluation: EvalLight | null; snapshot_minutes: number;
  /** false = 並べて動かす新しい版で、チェーンを読んでいない */
  chain_reads?: boolean;
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
  venue?: { id: string; name: string | null; practice: boolean; practice_note: string; reward_estimate_note?: string | null };
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
  id: number; pool_id: string; pair: string; venue_id: string; status: "open" | "closed"; reference?: boolean;
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
  /** 今日（日本時間の0時から）の損益と、評価額の小さな線グラフ（2026-09-30 画面の見直し） */
  today_usd: number; spark: number[];
  /** ボーナスが減ったときの比べ方の最新の結果（2026-09-30 オーナー決定③。記録だけ） */
  bonus_drop: BonusDrop | null;
  swap?: { slippage_pct_now: number | null; rebalance_slippage_total: number; rebalance_slippage_next: number | null;
    open: { swap_usd: number; slippage_pct: number; slippage: number; swap_fee?: number; gas?: number; hedge_fee?: number;
      total?: number; estimated?: boolean } | null }; close_cost_usd: number | null;
}

export interface BonusDrop {
  at: string; event: number; best: "stay" | "fees" | "exit"; best_ja: string; ratio: number; next_flip: string;
  options: Record<string, { usd: number; per_day: number; income_day?: number; close_cost?: number; switch_cost?: number }>;
}

export interface Watch {
  contracts: { address: string; label: string; checked_at: string | null; ok: string[]; unconfirmed: string[];
    changed_at: string | null }[];
  usdg: { ts: string; price: number; source: string } | null;
}

export interface RiskEvent {
  id: number; ts: string; position_id: number | null; level: "caution" | "rebalance" | "exit" | "emergency" | "info";
  level_ja: string; kind: string; message: string; action: string; action_ja: string;
  stage?: number | null; stage_ja?: string | null; rule_ja?: string | null; breakdown?: LossBreakdown | null;
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
  /** 評価の間は新しい練習を始めない（2026-09-30 オーナー決定①） */
  evaluation_block: { until: string; message: string } | null;
  /** 参考の練習（合否に使わない。2026-10-01 案B） */
  reference?: { open: number; max: number; outside_limits: boolean; can_open: boolean; note: string };
}

export interface TimelineItem {
  key: string; ts: string; type: "review" | "event" | "open" | "close"; level: string;
  title: string; body: string; position_id?: number | null;
}

export interface Evaluation {
  state: "not_started" | "running" | "stopped" | "finished" | "interrupted"; mode: string; evaluation_days: number;
  started_at?: string; ends_at?: string; elapsed_hours?: number; left_hours?: number;
  closed_positions?: EvalClosed[]; open_positions?: number; interrupted?: EvalInterrupted | null;
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
    /** 合否の比べる相手（2026-10-01）: weekly = 切り替えのあとはその週の見込み */
    prediction?: "weekly" | "start";
    start_only?: { hold: EvalVerdict; sell: EvalVerdict } | null;
  };
  days?: { day: number; start: string; done: boolean; hours: number; capital: number; predicted: number | null;
    predicted_sell: number | null; hold: number | null; sell: number | null; hold_ok: boolean; sell_ok: boolean;
    reference?: number | null; reference_ok?: boolean | null; flip?: boolean;
    week_hours?: number; predicted_start?: number | null; start_ok?: boolean }[];
  weekly?: { enabled: boolean; used: { flip_at: string; score_ts: string }[]; note: string | null };
  reference_tracks?: { tracks: RefTrack[]; note: string };
  /** 参考: その時間の見込みとの比較（2026-09-30 オーナー決定②。合否には使わない） */
  reference?: { ok_days: number; need_days: number; differs_days: number[]; flip_days: number[]; net_day: number | null; note: string };
}

/** 参考の練習の「見込みと実際」（建玉ごと。合否に使わない。2026-10-01 案B） */
export interface RefTrack {
  id: number; pool_id: string; pair: string; status: "open" | "closed"; opened_at: string; closed_at: string | null;
  close_reason_ja: string | null; mode: string; capital: number; ends_at: string;
  done_days: number; ok_days: number; need_days: number; observed_hours: number; estimated_hours: number;
  predicted_net_day: number | null; actual_net_day: number | null;
  days: { day: number; done: boolean; hours: number; predicted: number | null; hold: number | null; ok: boolean }[];
}

export interface EvalVerdict { ok_days: number; need_days: number; result: "running" | "stopped" | "interrupted" | "pass" | "fail" }

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
  outlook: Outlook | null; timeline: TimelineItem[]; hedge_margin?: HedgeMargin | null;
}

/** POST して JSON を返す。失敗したらサーバーの日本語の理由を投げる。 */
export async function postApi<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
  return r.json();
}

/** PUT して JSON を返す（設定の変更）。失敗したらサーバーの日本語の理由を投げる。 */
export async function putApi<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
  return r.json();
}

/**
 * API を読む。画面に戻ってきたとき（タブを開き直す・スマホで表示し直す）と、refreshMs ごとに読み直す
 * （開きっぱなしの画面が古い数字のままにならないように。オーナー依頼 21）。読み直しの間も前の数字は出したまま。
 */
export function useApi<T>(path: string, refreshMs = 60_000): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const [shown, setShown] = useState(path);
  if (shown !== path) {        // 別のプールなどに移ったら、前の画面の数字は消す
    setShown(path);
    setData(null);
  }
  useEffect(() => {
    let alive = true;
    fetch(path)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
        return r.json();
      })
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e) => alive && setError(String(e.message ?? e)));
    return () => { alive = false; };
  }, [path, n]);
  useEffect(() => {
    const again = () => { if (document.visibilityState === "visible") setN((x) => x + 1); };
    document.addEventListener("visibilitychange", again);
    window.addEventListener("focus", again);
    const t = refreshMs > 0 ? window.setInterval(again, refreshMs) : undefined;
    return () => {
      document.removeEventListener("visibilitychange", again);
      window.removeEventListener("focus", again);
      if (t) window.clearInterval(t);
    };
  }, [refreshMs]);
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
