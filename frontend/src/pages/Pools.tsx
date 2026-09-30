import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useApi, type PoolDetail as Detail, type PoolRow } from "../api";
import { VERDICT, bigUsd, faintPct, jstDay, plainPct, untilText, usd, verdictOf, type Verdict } from "../format";
import { Icon } from "../icons";
import { Dot, Loading, PageHead, Pill, Segmented, Spark, Term, VerdictPill, usePulse, useWide } from "../ui";
import { conclusion, KeyReason, StatTiles, warnItems, WhyList, BreakdownBars, ActionCard } from "./PoolDetail";
import { FlipNotice, PoolLine, countVerdicts } from "./parts";

// プール（SPEC 7.3章。2026-09-30 オーナー依頼 29〜31）
// スマホ: 判定で絞る → 一覧。パソコン: 並べ替えられる表。行を押すと右に詳しい画面（ページは移らない）
type Scores = { counts: Record<string, number>; scores: PoolRow[] };
type SortKey = "venue" | "pair" | "verdict" | "net" | "hedge" | "slip" | "tvl" | "flip";
type Filter = "all" | Verdict;

const LEGACY: Record<string, Verdict> = { green: "good", yellow: "watch", red: "danger" };
const RANK: Record<Verdict, number> = { good: 3, watch: 2, danger: 1, none: 0 };
const SORTS: { key: SortKey; label: string; desc: boolean }[] = [
  { key: "net", label: "純日利が高い順", desc: true },
  { key: "tvl", label: "プールが大きい順", desc: true },
  { key: "slip", label: "両替のずれが小さい順", desc: false },
];
const MOBILE_FIRST = 30;

function sortValue(p: PoolRow, k: SortKey): number | string {
  switch (k) {
    case "venue": return p.venue_name ?? p.venue_id;
    case "pair": return p.pair;
    case "verdict": return RANK[verdictOf(p)];
    case "net": return p.net_daily_pct ?? -Infinity;
    case "hedge": return p.hedge_info?.has ? 1 : 0;
    case "slip": return p.slippage_pct ?? Infinity;
    case "tvl": return p.tvl_usd ?? -Infinity;
    case "flip": return p.epoch_flip?.at ?? "9999";
  }
}

export default function Pools() {
  const wide = useWide();
  const pulse = usePulse();
  const { data, error } = useApi<Scores>("/api/scores?slim=1");
  const [params, setParams] = useSearchParams();
  const [showAll, setShowAll] = useState(false);
  const raw = params.get("v") ?? params.get("signal");
  const filter: Filter = raw ? (LEGACY[raw] ?? (raw as Filter)) : "all";
  const venue = params.get("venue");
  const hedgeOnly = params.get("hedge") === "1";
  const sort = (params.get("sort") as SortKey | null) ?? "net";
  const desc = params.get("dir") ? params.get("dir") === "desc" : (SORTS.find((s) => s.key === sort)?.desc ?? true);
  const sel = params.get("sel");
  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    p.delete("signal");
    Object.entries(next).forEach(([k, v]) => (v == null ? p.delete(k) : p.set(k, v)));
    setParams(p, { replace: true });
  };
  const head = <PageHead title="プール" right={<span className="cap">{data ? `${data.scores.length}件` : ""}</span>} />;
  if (!data) return <>{head}<Loading error={error} /></>;

  const venues = [...new Map(data.scores.map((r) => [r.venue_id, r.venue_name ?? r.venue_id])).entries()];
  const base = data.scores.filter((r) => (!venue || r.venue_id === venue) && (!hedgeOnly || r.hedge_info?.has));
  const counts = countVerdicts(base);
  const rows = base.filter((r) => filter === "all" || verdictOf(r) === filter).sort((a, b) => {
    const x = sortValue(a, sort), y = sortValue(b, sort);
    const c = typeof x === "string" ? x.localeCompare(y as string, "ja") : (x as number) - (y as number);
    return desc ? -c : c;
  });
  const practicing = new Set(pulse?.practicing ?? []);
  const flip = base.map((r) => r.epoch_flip).filter(Boolean).sort((a, b) => a!.at.localeCompare(b!.at))[0] ?? null;
  const flipVenues = [...new Set(base.filter((r) => r.epoch_flip).map((r) => r.venue_name ?? r.venue_id))];

  const seg = (
    <Segmented<Filter> value={filter} onChange={(k) => set({ v: k === "all" ? null : k })}
      options={[
        { key: "all", label: `すべて ${base.length}` },
        ...(["good", "watch", "danger", "none"] as Verdict[])
          .filter((k) => k !== "none" || counts.none > 0)
          .map((k) => ({ key: k as Filter, label: `${VERDICT[k].label} ${counts[k]}`, dot: k })),
      ]} />
  );
  const venueChip = venues.length > 1 && (
    <label className="chip relative">
      <Icon name="filter" size={16} color="var(--sec)" />
      <span>会場: {venue ? venues.find(([id]) => id === venue)?.[1] : "すべて"}</span><Icon name="down" size={16} />
      <select aria-label="会場で絞る" className="absolute inset-0 cursor-pointer opacity-0" value={venue ?? ""}
        onChange={(e) => set({ venue: e.target.value || null })}>
        <option value="">すべて</option>
        {venues.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select>
    </label>
  );
  const hedgeChip = (
    <button type="button" className="chip" aria-pressed={hedgeOnly} onClick={() => set({ hedge: hedgeOnly ? null : "1" })}>
      <Icon name="shield" size={16} color="var(--sec)" /><span>保険あり</span>
      <span className="flex h-5 w-8 items-center rounded-full px-0.5" style={{ background: hedgeOnly ? "var(--text)" : "rgba(255,255,255,0.12)", justifyContent: hedgeOnly ? "flex-end" : "flex-start" }}>
        <span className="h-4 w-4 rounded-full" style={{ background: hedgeOnly ? "var(--bg)" : "var(--sec)" }} />
      </span>
    </button>
  );
  const lead = leadText(rows, filter);

  if (wide) {
    return (
      <div style={{ marginRight: sel ? 416 : 0 }} className="flex flex-col gap-6">
        {head}
        <div className="flex flex-wrap items-center gap-2"><div className="w-auto">{seg}</div>{venueChip}{hedgeChip}</div>
        {flip && (
          <div className="cap flex items-start gap-2 text-sec">
            <Icon name="clock" size={16} color="var(--y)" />
            <span>{jstDay(flip.at)} にボーナスが<Term k="エポック">切り替わります</Term>（{flipVenues.join("・")}・{untilText(flip.at)}）。来週はボーナスがなくなることもあります</span>
          </div>
        )}
        <PoolTable rows={rows} sort={sort} desc={desc} sel={sel} practicing={practicing} narrow={!!sel}
          onSort={(k) => set({ sort: k, dir: k === sort ? (desc ? "asc" : "desc") : (k === "pair" || k === "venue" || k === "slip" || k === "flip" ? "asc" : "desc") })}
          onSelect={(id) => set({ sel: id === sel ? null : id })} />
        {sel && <Aside id={sel} onClose={() => set({ sel: null })} />}
      </div>
    );
  }

  const shown = showAll ? rows : rows.slice(0, MOBILE_FIRST);
  return (
    <>
      {head}
      {seg}
      <div className="flex flex-wrap gap-2">
        {venueChip}{hedgeChip}
        <label className="chip relative">
          <Icon name="sort" size={16} color="var(--sec)" /><span>{SORTS.find((s) => s.key === sort)?.label ?? "並べ替え"}</span>
          <select aria-label="並べ替え" className="absolute inset-0 cursor-pointer opacity-0" value={sort}
            onChange={(e) => set({ sort: e.target.value, dir: null })}>
            {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
      </div>
      {lead && <p className="sec">{lead}</p>}
      {flip && <FlipNotice f={flip} text={`${flipVenues.join("・")}のプール。来週はボーナスがなくなることもあります`} />}
      <section className="card flex flex-col overflow-hidden">
        {shown.length === 0 && <p className="cap p-6">ここに出すプールはありません。</p>}
        {shown.map((r, i) => (
          <div key={r.pool_id} className={i ? "border-t border-white/[0.06]" : ""}>
            <PoolLine p={r} practicing={practicing.has(r.pool_id)} />
          </div>
        ))}
        {rows.length > shown.length && (
          <button type="button" className="more" onClick={() => setShowAll(true)}>
            <span>残りの{rows.length - shown.length}件を見る</span><Icon name="down" />
          </button>
        )}
      </section>
      <p className="cap">数字は<Term k="総資産あたり日利" />。小さな線は直近48時間の純日利の動きです。</p>
    </>
  );
}

function leadText(rows: PoolRow[], filter: Filter): string | null {
  if (filter !== "all" || rows.length === 0) return null;
  const good = rows.filter((r) => verdictOf(r) === "good");
  if (good.length > 0) return `良いプールが${good.length}つあります。`;
  const top = rows[0];
  return `良い（緑）のプールは今はありません。いちばん上は${VERDICT[verdictOf(top)].label}の ${top.pair} です。`;
}

const COLS = (narrow: boolean) => narrow
  ? "72px minmax(0,1.6fr) 96px 80px 56px 88px 72px"
  : "88px minmax(0,1.6fr) 104px 88px 64px 96px 80px 136px";

function PoolTable({ rows, sort, desc, sel, practicing, narrow, onSort, onSelect }: {
  rows: PoolRow[]; sort: SortKey; desc: boolean; sel: string | null; practicing: Set<string>; narrow: boolean;
  onSort: (k: SortKey) => void; onSelect: (id: string) => void;
}) {
  const [limit, setLimit] = useState(60);
  const th = (k: SortKey, label: string, right = false, extra = "") => {
    const on = sort === k;
    return (
      <button type="button" aria-sort={on ? (desc ? "descending" : "ascending") : undefined} onClick={() => onSort(k)}
        className={`flex min-h-11 items-center gap-1 text-[12px] leading-4 font-semibold ${right ? "justify-end" : ""} ${extra}`}
        style={{ color: on ? "var(--text)" : "var(--cap)" }}>
        <span className="whitespace-nowrap">{label}</span>
        <Icon name={on ? (desc ? "sortDown" : "sortUp") : "sort"} size={16} />
      </button>
    );
  };
  return (
    <section className="card flex flex-col p-2">
      <div className="grid items-center gap-2 border-b border-white/[0.06] px-4" style={{ gridTemplateColumns: COLS(narrow) }}>
        {th("venue", "会場")}{th("pair", "ペア")}{th("verdict", "判定")}{th("net", "純日利", true)}
        {th("hedge", "保険", false, "pl-4")}{th("slip", "両替のずれ", true)}{th("tvl", "大きさ", true)}{!narrow && th("flip", "次の切り替え", true)}
      </div>
      {rows.slice(0, limit).map((p) => {
        const v = verdictOf(p);
        const on = p.pool_id === sel;
        return (
          <button key={p.pool_id} type="button" onClick={() => onSelect(p.pool_id)} aria-pressed={on}
            className={`grid min-h-14 items-center gap-2 rounded-2xl px-4 text-left ${on ? "bg-inset" : "hover:bg-white/[0.03]"}`}
            style={{ gridTemplateColumns: COLS(narrow) }}>
            <span className="cap truncate text-sec">{p.venue_name ?? p.venue_id}</span>
            <span className="flex min-w-0 flex-col">
              <span className="bold truncate">{p.pair}</span>
              {(practicing.has(p.pool_id) || p.reward_estimate_note) && <span className="cap">{practicing.has(p.pool_id) ? "練習中" : "ボーナスは推定"}</span>}
            </span>
            <span className="cap flex items-center gap-2 text-sec"><Dot v={v} small />{VERDICT[v].label}</span>
            <span className={`bold num text-right ${v === "none" || faintPct(p.net_daily_pct) ? "text-cap" : ""}`}>{v === "none" ? "—" : plainPct(p.net_daily_pct)}</span>
            <span className="cap pl-4 text-sec">{p.hedge_info?.has ? "あり" : "なし"}</span>
            <span className="cap num text-right text-sec">{p.slippage_pct == null ? "—" : `${p.slippage_pct.toFixed(2)}%`}</span>
            <span className="cap num text-right text-sec">{bigUsd(p.tvl_usd)}</span>
            {!narrow && <span className="cap num text-right text-sec">{p.epoch_flip ? jstDay(p.epoch_flip.at) : "—"}</span>}
          </button>
        );
      })}
      {rows.length === 0 && <p className="cap p-4">ここに出すプールはありません。</p>}
      {rows.length > limit && (
        <button type="button" className="more px-4" onClick={() => setLimit(rows.length)}><span>残りの{rows.length - limit}件を見る</span><Icon name="down" /></button>
      )}
    </section>
  );
}

/** 右に浮かぶ詳しい画面（オーナー依頼 31）。全画面で開くボタンつき */
function Aside({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error } = useApi<Detail>(`/api/pools/${encodeURIComponent(id)}`);
  const [why, setWhy] = useState(false);
  const [bd, setBd] = useState(false);
  return (
    <aside aria-label="選んだプールの詳しい画面"
      className="glass fixed top-4 right-4 bottom-4 z-40 flex w-[400px] flex-col gap-4 overflow-y-auto rounded-3xl p-6">
      {!data ? <Loading error={error} rows={1} /> : (() => {
        const s = data.score;
        const v = verdictOf(s);
        const w = warnItems(data);
        const nd = w.filter((x) => x.level === "danger").length;
        const hist = data.history.map((h) => h.net_daily_pct).filter((x): x is number => x != null);
        return (
          <>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0"><h2 className="t20 truncate">{s.pair}</h2><div className="cap">{data.venue?.name ?? s.venue_id}</div></div>
              <div className="flex gap-2">
                <Link to={`/pools/${encodeURIComponent(s.pool_id)}`} aria-label="全画面で開く" className="iconbtn"><Icon name="expand" /></Link>
                <button type="button" aria-label="閉じる" className="iconbtn" onClick={onClose}><Icon name="x" /></button>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2"><VerdictPill v={v} />
              <span className="cap">{conclusion(v, s.net_daily_pct, (s.warnings ?? []).some((x) => x.level === "major"))}</span></div>
            <div className="flex flex-col gap-2">
              <span className="label">純日利（費用と<Term k="ガンマ損失">ガンマ</Term>を引いたあと）</span>
              <span className={`hero num ${v === "none" || faintPct(s.net_daily_pct) ? "text-cap" : ""}`}>{v === "none" ? "—" : plainPct(s.net_daily_pct)}</span>
              {data.daily.breakdown && <span className="sec">{usd(data.capital.total, 0)} なら 1日 約 {usd(data.daily.breakdown.net)}</span>}
            </div>
            {hist.length > 1 && <Spark values={hist} height={40} />}
            <KeyReason d={data} inset />
            <StatTiles d={data} inset />
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={nd ? "r" : "n"}>危険 {nd}</Pill><Pill tone={w.length - nd ? "y" : "n"}>注意 {w.length - nd}</Pill>
              {w.length > 0 && <Link to={`/pools/${encodeURIComponent(s.pool_id)}`} className="cap text-sec underline decoration-dotted underline-offset-4">中身は全画面で</Link>}
            </div>
            <div className="-mx-6 flex flex-col">
              <button type="button" className="more" aria-expanded={why} onClick={() => setWhy(!why)}><span>なぜこの判定か</span><Icon name={why ? "up" : "down"} /></button>
              {why && <div className="px-6 pb-6"><WhyList checks={s.checks ?? []} /></div>}
              {data.daily.breakdown && (
                <>
                  <button type="button" className="more" aria-expanded={bd} onClick={() => setBd(!bd)}><span>1日の見込み（{usd(data.capital.total, 0)}あたり）</span><Icon name={bd ? "up" : "down"} /></button>
                  {bd && <div className="px-6 pb-6"><BreakdownBars b={data.daily.breakdown} capital={data.capital.total} /></div>}
                </>
              )}
            </div>
            <ActionCard d={data} />
          </>
        );
      })()}
    </aside>
  );
}
