export const usd = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const signedUsd = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${usd(v, digits)}`;

export const pct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}%`;

export const ratioPct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);

export const bigUsd = (v: number | null | undefined) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : a.toFixed(0);
  return `${v < 0 ? "−" : ""}$${s}`;
};

export const tone = (v: number | null | undefined) =>
  v == null ? "text-slate-400" : v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-300";

export const jst = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

export const jstHour = (iso: string) =>
  new Date(iso).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "numeric" });

export const SIGNAL = {
  green: { emoji: "🟢", ja: "攻め候補", cls: "text-emerald-400" },
  yellow: { emoji: "🟡", ja: "様子見", cls: "text-amber-300" },
  red: { emoji: "🔴", ja: "見送り", cls: "text-rose-400" },
} as const;

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
