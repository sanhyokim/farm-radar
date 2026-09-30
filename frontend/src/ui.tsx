import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { GLOSSARY, findTerm } from "./glossary";
import { VERDICT, hm, jstDate, type Verdict } from "./format";
import { Icon, type IconName } from "./icons";
import { useApi, type Pulse } from "./api";

// --- 画面の幅（スマホ / パソコン。オーナー依頼 29: 同じURLで幅に合わせる） -------------------------------
export function useMedia(query: string): boolean {
  const get = () => typeof window !== "undefined" && window.matchMedia(query).matches;
  const [on, setOn] = useState(get);
  useEffect(() => {
    const m = window.matchMedia(query);
    const f = () => setOn(m.matches);
    f();
    m.addEventListener("change", f);
    return () => m.removeEventListener("change", f);
  }, [query]);
  return on;
}
export const useWide = () => useMedia("(min-width: 1024px)");

// --- 収集の状態と計算の時刻（左のメニューと見出しで使う） --------------------------------------------
const PulseCtx = createContext<Pulse | null>(null);
export function PulseProvider({ children }: { children: ReactNode }) {
  const { data } = useApi<Pulse>("/api/pulse", 60_000);
  return <PulseCtx.Provider value={data}>{children}</PulseCtx.Provider>;
}
export const usePulse = () => useContext(PulseCtx);

/** 画面の見出し（「9月30日(水) · 13:05 に計算」と題名） */
export function PageHead({ title, right, at, back }: { title: ReactNode; right?: ReactNode; at?: string | null; back?: { to: string; label: string } }) {
  const pulse = usePulse();
  const when = at ?? pulse?.scored_at;
  return (
    <header className="flex items-end justify-between gap-2 pt-2">
      <div className="flex min-w-0 flex-col gap-1">
        {back && (
          <Link to={back.to} className="cap flex items-center gap-1 text-sec"><Icon name="left" size={16} /><span>{back.label}</span></Link>
        )}
        <div className="cap">{jstDate()}{when && ` · ${hm(when)} に計算`}</div>
        <h1 className="t20 truncate">{title}</h1>
      </div>
      {right}
    </header>
  );
}

// --- 用語（点線の下線。タップで説明と「学ぶ」へのリンク。オーナー依頼 16） ------------------------------
const TermCtx = createContext<(t: string) => void>(() => {});

export const termId = (term: string) => `term-${GLOSSARY.findIndex((g) => g.term === term)}`;

export function TermProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<string | null>(null);
  const entry = open ? findTerm(open) : null;
  return (
    <TermCtx.Provider value={setOpen}>
      {children}
      {entry && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 lg:items-center" onClick={() => setOpen(null)}>
          <div role="dialog" aria-label={entry.term}
            className="glass w-full max-w-lg rounded-t-3xl p-6 pb-10 lg:rounded-3xl lg:pb-6" onClick={(e) => e.stopPropagation()}>
            <div className="t20 mb-2">{entry.term}</div>
            <p className="sec">{entry.text}</p>
            <div className="mt-6 flex flex-wrap gap-2">
              <Link to={`/learn#${termId(entry.term)}`} onClick={() => setOpen(null)} className="btn btn-quiet">
                <Icon name="learn" size={16} /><span>学ぶで詳しく見る</span>
              </Link>
              <button className="btn" onClick={() => setOpen(null)}>閉じる</button>
            </div>
          </div>
        </div>
      )}
    </TermCtx.Provider>
  );
}

/** 専門用語。タップすると説明が出る（SPEC 7章）。k は用語集の見出し */
export function Term({ k, children }: { k: string; children?: ReactNode }) {
  const open = useContext(TermCtx);
  return (
    <button type="button" className="term" aria-label={`${k} の説明`}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); open(k); }}>
      {children ?? k}
    </button>
  );
}

// --- 部品 ---------------------------------------------------------------------------------
export function Card({ title, children, className = "", right, pad = "p-6" }: {
  title?: ReactNode; children: ReactNode; className?: string; right?: ReactNode; pad?: string;
}) {
  return (
    <section className={`card flex flex-col gap-4 ${pad} ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between gap-2">
          <h2 className="label min-w-0">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

type PillTone = "g" | "y" | "r" | "n";
export function Pill({ tone = "n", icon, children }: { tone?: PillTone; icon?: IconName; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{icon && <Icon name={icon} size={12} />}{children}</span>;
}

/** 前の形の印（色の名前）を、新しい印に読み替える */
export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "amber" | "rose" | "emerald" | "sky" }) {
  const t: PillTone = tone === "amber" ? "y" : tone === "rose" ? "r" : tone === "emerald" ? "g" : "n";
  return <Pill tone={t}>{children}</Pill>;
}

export function Dot({ v, small = false }: { v: Verdict; small?: boolean }) {
  return <span className={`dot ${VERDICT[v].dot} ${small ? "dot-sm" : ""}`} aria-hidden="true" />;
}

export function VerdictPill({ v }: { v: Verdict }) {
  return <span className={`pill ${VERDICT[v].pill}`}><Dot v={v} small />{VERDICT[v].label}</span>;
}

/** 読み込み中は灰色の形を出す（オーナー依頼 11）。読めなかったときは理由 */
export function Loading({ error, rows = 3 }: { error?: string | null; rows?: number }) {
  if (error) {
    return (
      <div className="card flex items-start gap-4 p-6">
        <Icon name="alert" color="var(--y)" />
        <div><div>読み込めませんでした</div><div className="cap">{error}。アプリが動いているか確かめて、画面を読み直してください。</div></div>
      </div>
    );
  }
  return (
    <div aria-busy="true" className="flex flex-col gap-4">
      {Array.from({ length: rows }, (_, i) => (
        <section key={i} className="card flex flex-col gap-4 p-6">
          <Sk w="96px" h={16} /><Sk w="70%" h={32} /><Sk w="100%" h={56} r={16} />
        </section>
      ))}
    </div>
  );
}

export function Sk({ w, h, r = 8 }: { w: string; h: number; r?: number }) {
  return <div className="sk" style={{ width: w, height: h, borderRadius: r }} />;
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="cap">{children}</p>;
}

/** 詳しく見る（たたんでおく。オーナー依頼 12） */
export function Fold({ title, children, open: initial = false, id }: { title: ReactNode; children: ReactNode; open?: boolean; id?: string }) {
  const [open, setOpen] = useState(initial);
  return (
    <div id={id}>
      <button type="button" className="more" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="min-w-0">{title}</span><Icon name={open ? "up" : "down"} />
      </button>
      {open && <div className="flex flex-col gap-4 px-6 pb-6">{children}</div>}
    </div>
  );
}

/** たたんだ項目をまとめる1枚のカード */
export function Folds({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card folds flex flex-col overflow-hidden ${className}`}>{children}</section>;
}

/** アイコン + 1行 + 小さな説明（今日やること・危険と注意など） */
export function Item({ icon, color, title, sub, to }: { icon: IconName; color?: string; title: ReactNode; sub?: ReactNode; to?: string | null }) {
  const body = (
    <>
      <span className="flex pt-0.5" style={{ color: color ?? "var(--sec)" }}><Icon name={icon} /></span>
      <div className="flex min-w-0 flex-1 flex-col"><div>{title}</div>{sub && <div className="cap">{sub}</div>}</div>
      {to && <span className="flex pt-0.5 text-sec"><Icon name="right" size={16} /></span>}
    </>
  );
  return to
    ? <Link to={to} className="inset flex items-start gap-4 p-4">{body}</Link>
    : <div className="inset flex items-start gap-4 p-4">{body}</div>;
}

/** 小さな折れ線（色は文字の濃淡だけ） */
export function Spark({ values, height, area = true, stroke = "var(--sec)" }: { values: number[]; height: number; area?: boolean; stroke?: string }) {
  if (values.length < 2) return <div style={{ height }} className="flex items-end"><div className="h-px w-full bg-white/10" /></div>;
  const w = 100;
  const lo = Math.min(...values), hi = Math.max(...values);
  const rng = hi - lo || 1;
  const pts = values.map((v, i) => [(w * i) / (values.length - 1), 2 + (height - 4) * (1 - (v - lo) / rng)]);
  const line = pts.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const d = `M0,${height} ${pts.map(([x, y]) => `L${x.toFixed(2)},${y.toFixed(2)}`).join(" ")} L${w},${height} Z`;
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" aria-hidden="true" className="block">
      {area && <path d={d} fill="rgba(255,255,255,0.05)" stroke="none" />}
      <polyline points={line} fill="none" stroke={stroke} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round"
        vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** 丸いメーター（評価の何日目か） */
export function Ring({ frac, size, stroke = 8, big, small }: { frac: number; size: number; stroke?: number; big: ReactNode; small: ReactNode }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const m = size / 2;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={m} cy={m} r={r} fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth={stroke} />
        <circle cx={m} cy={m} r={r} fill="none" stroke="var(--text)" strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${(c * Math.max(0, Math.min(1, frac))).toFixed(1)} ${c.toFixed(1)}`} transform={`rotate(-90 ${m} ${m})`} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="bold">{big}</span><span className="cap">{small}</span>
      </div>
    </div>
  );
}

/** 切り替えのボタン（すべて / 良い / 様子見 / 危険 など） */
export function Segmented<K extends string>({ options, value, onChange }: {
  options: { key: K; label: ReactNode; dot?: Verdict }[]; value: K; onChange: (k: K) => void;
}) {
  return (
    <div className="scroll-x card rounded-full p-2" style={{ borderRadius: 999 }}>
      <div className="grid gap-0" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(max-content, 1fr))` }}>
        {options.map((o) => (
          <button key={o.key} type="button" className="seg" aria-pressed={value === o.key} onClick={() => onChange(o.key)}>
            {o.dot && <Dot v={o.dot} small />}{o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 表の中の1行（左: 名前、右: 数字） */
export function Line({ k, v, note, strong = false }: { k: ReactNode; v: ReactNode; note?: ReactNode; strong?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <span className="min-w-0 text-sec">{k}{note && <span className="cap block">{note}</span>}</span>
      <span className={`num shrink-0 text-right ${strong ? "bold" : ""}`}>{v}</span>
    </div>
  );
}
