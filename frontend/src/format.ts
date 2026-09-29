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
