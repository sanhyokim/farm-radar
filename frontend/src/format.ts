import type { PoolRow, Signal } from "./api";

// 0 に近い数字は符号を付けない（「−0.00%」を出さない。オーナー依頼 14）
const nearZero = (v: number, digits: number) => Math.abs(v) < 0.5 * 10 ** -digits;

export const usd = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v < 0 && !nearZero(v, digits) ? "−" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const signedUsd = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v > 0 && !nearZero(v, digits) ? "+" : ""}${usd(v, digits)}`;

export const pct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : nearZero(v, digits) ? `${(0).toFixed(digits)}%` : `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}%`;

/** 符号なしの % （純日利の大きな数字など。マイナスだけ「−」） */
export const plainPct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : nearZero(v, digits) ? `${(0).toFixed(digits)}%` : `${v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}%`;

export const ratioPct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);

export const bigUsd = (v: number | null | undefined) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : a.toFixed(0);
  return `${v < 0 ? "−" : ""}$${s}`;
};

/** 数字の色: 数字は白。データがない・0 に近いときだけ灰色（緑と赤は判定の点だけに使う。オーナー依頼 14） */
export const tone = (v: number | null | undefined, digits = 2) =>
  v == null || nearZero(v, digits) ? "text-cap" : "text-ink";

/** 純日利が 0 に近い（±0.05% 未満。$1,000 で1日 50セント未満）ときは灰色にする（オーナー依頼 14） */
export const faintPct = (v: number | null | undefined) => v == null || Math.abs(v) < 0.05;

export const jst = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

export const jstHour = (iso: string) =>
  new Date(iso).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "numeric" });

/** 「13:05」（日本時間） */
export const hm = (iso: string | null | undefined) =>
  !iso ? "—" : new Date(iso).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit" });

/** 「9月30日(水)」（日本時間） */
export const jstDate = (iso: string | number | Date = Date.now()) => {
  const d = new Date(iso);
  const md = d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "long", day: "numeric" });
  const wd = d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", weekday: "short" });
  return `${md}(${wd})`;
};

// --- 判定の呼び方（オーナー依頼 7・14: 緑=良い、黄=様子見、赤=危険、灰=データ不足） -------------------------
export type Verdict = "good" | "watch" | "danger" | "none";

export const VERDICT: Record<Verdict, { label: string; dot: string; pill: string }> = {
  good: { label: "良い", dot: "dot-g", pill: "pill-g" },
  watch: { label: "様子見", dot: "dot-y", pill: "pill-y" },
  danger: { label: "危険", dot: "dot-r", pill: "pill-r" },
  none: { label: "データ不足", dot: "dot-n", pill: "pill-n" },
};

const FROM_SIGNAL: Record<Signal, Verdict> = { green: "good", yellow: "watch", red: "danger" };

/** 判定（計算できなかったプールは「データ不足」の灰色） */
export const verdictOf = (p: Pick<PoolRow, "signal" | "net_daily_pct">): Verdict =>
  p.net_daily_pct == null ? "none" : FROM_SIGNAL[p.signal];

export const signalLabel = (s: Signal) => VERDICT[FROM_SIGNAL[s]].label;

/** 大きなドルの額を日本語の単位で（$1.2万、$3.4億）。見通しの大きな数字用 */
export const jpUsd = (v: number | null | undefined) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  const s = a >= 1e12 ? `${(a / 1e12).toLocaleString("en-US", { maximumFractionDigits: 1 })}兆` :
    a >= 1e8 ? `${(a / 1e8).toFixed(1)}億` : a >= 1e4 ? `${(a / 1e4).toFixed(1)}万` :
    a.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return `${v < 0 ? "−" : ""}$${s}`;
};

/** 値段（桁が大きく違うので、大きさに合わせて小数の桁を決める） */
export const price = (v: number) =>
  v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) :
  v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(4) : v.toPrecision(3);

/** 最適レンジの値段の範囲（例: NVDA $176.40〜$194.94） */
export const rangeText = (r: { kind: string; symbol: string; quote?: string; low: number; high: number;
  usd_low?: number; usd_high?: number } | null | undefined) =>
  !r ? "" : r.kind === "usd" ? `${r.symbol} $${price(r.low)}〜$${price(r.high)}`
    : r.usd_low != null && r.usd_high != null
      ? `${r.symbol} $${price(r.usd_low)}〜$${price(r.usd_high)} 相当（${r.quote ?? ""} が今の値段のままなら）`
      : `1 ${r.symbol} = ${price(r.low)}〜${price(r.high)} ${r.quote ?? ""}`;

/** 次の時刻までの残り（「あと1日9時間」「あと35分」）。過ぎていれば「まもなく」 */
export const untilText = (iso: string, now: number = Date.now()) => {
  const m = Math.floor((new Date(iso).getTime() - now) / 60000);
  if (m <= 0) return "まもなく";
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
  if (d > 0) return `あと${d}日${h}時間`;
  if (h > 0) return `あと${h}時間${m % 60}分`;
  return `あと${m}分`;
};

/** 「10/1(木) 9:00」の形（日本時間） */
export const jstDay = (iso: string) => {
  const d = new Date(iso);
  const md = d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "numeric", day: "numeric" });
  const wd = d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", weekday: "short" });
  const hm = d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", hour: "numeric", minute: "2-digit" });
  return `${md}(${wd}) ${hm}`;
};

export function hoursJa(h: number): string {
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}分`;
  if (h < 48) return `${h < 10 ? h.toFixed(1) : Math.round(h)}時間`;
  return `${(h / 24).toFixed(1)}日`;
}
