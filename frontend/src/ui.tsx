import { createContext, useContext, useState, type ReactNode } from "react";
import { findTerm } from "./glossary";
import { SIGNAL } from "./format";
import type { Signal } from "./api";

// --- 用語の「?」 -------------------------------------------------------------------------
const TermCtx = createContext<(t: string) => void>(() => {});

export function TermProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<string | null>(null);
  const entry = open ? findTerm(open) : null;
  return (
    <TermCtx.Provider value={setOpen}>
      {children}
      {entry && (
        <div className="fixed inset-0 z-50 flex items-end bg-black/60" onClick={() => setOpen(null)}>
          <div className="w-full rounded-t-2xl bg-slate-800 p-5 pb-10 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 text-lg font-bold text-slate-100">{entry.term}</div>
            <p className="leading-relaxed text-slate-200">{entry.text}</p>
            <button className="mt-4 w-full rounded-lg bg-slate-700 py-2 text-slate-100" onClick={() => setOpen(null)}>
              閉じる
            </button>
          </div>
        </div>
      )}
    </TermCtx.Provider>
  );
}

/** 専門用語に「?」を付ける（SPEC 7章: タップで用語集の説明）。 */
export function Term({ k, children }: { k: string; children?: ReactNode }) {
  const open = useContext(TermCtx);
  return (
    <span className="inline-flex items-center gap-0.5">
      {children ?? k}
      <button
        aria-label={`${k} の説明`}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); open(k); }}
        className="ml-0.5 inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-500 text-[10px] leading-none text-slate-400"
      >
        ?
      </button>
    </span>
  );
}

// --- 部品 ---------------------------------------------------------------------------------
export function Card({ title, children, className = "", right }: { title?: ReactNode; children: ReactNode; className?: string; right?: ReactNode }) {
  return (
    <section className={`rounded-2xl bg-slate-900 p-4 ring-1 ring-slate-800 ${className}`}>
      {(title || right) && (
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-300">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function SignalBadge({ s }: { s: Signal }) {
  const x = SIGNAL[s];
  return <span className={`whitespace-nowrap text-sm font-semibold ${x.cls}`}>{x.emoji} {x.ja}</span>;
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "amber" | "rose" | "emerald" | "sky" }) {
  const c = {
    slate: "bg-slate-800 text-slate-300", amber: "bg-amber-500/15 text-amber-300",
    rose: "bg-rose-500/15 text-rose-300", emerald: "bg-emerald-500/15 text-emerald-300", sky: "bg-sky-500/15 text-sky-300",
  }[tone];
  return <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs ${c}`}>{children}</span>;
}

export function Loading({ error }: { error?: string | null }) {
  return (
    <div className="p-6 text-center text-slate-400">
      {error ? <span className="text-rose-300">読み込めませんでした: {error}</span> : "読み込み中…"}
    </div>
  );
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="mt-2 text-xs leading-relaxed text-slate-400">{children}</p>;
}
