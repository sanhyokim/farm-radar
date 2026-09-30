import { Link, useSearchParams } from "react-router-dom";
import { useApi, type PoolRow, type Signal } from "../api";
import { SIGNAL, bigUsd, jstDay, pct, rangeText, untilText } from "../format";
import { HedgeBadge } from "./Home";
import { Loading, Term } from "../ui";

type Scores = { counts: Record<Signal, number>; scores: PoolRow[] };

export default function Pools() {
  const { data, error } = useApi<Scores>("/api/scores");
  const [params, setParams] = useSearchParams();
  const filter = (params.get("signal") as Signal | null) ?? null;
  const venue = params.get("venue");
  if (!data) return <Loading error={error} />;
  // 会場が2つ以上あるときだけ、会場で絞り込むボタンを出す（M6）
  const venues = [...new Map(data.scores.map((r) => [r.venue_id, r.venue_name ?? r.venue_id])).entries()];
  const inVenue = data.scores.filter((r) => !venue || r.venue_id === venue);
  const rows = inVenue.filter((r) => !filter || r.signal === filter);
  const counts = { green: 0, yellow: 0, red: 0 } as Record<Signal, number>;
  inVenue.forEach((r) => { counts[r.signal] = (counts[r.signal] ?? 0) + 1; });
  const set = (next: { signal?: Signal | null; venue?: string | null }) => {
    const s = next.signal !== undefined ? next.signal : filter;
    const v = next.venue !== undefined ? next.venue : venue;
    setParams({ ...(s ? { signal: s } : {}), ...(v ? { venue: v } : {}) });
  };
  const chipClass = (on: boolean) =>
    `rounded-full px-3 py-1 text-sm ${on ? "bg-sky-500/20 text-sky-200 ring-1 ring-sky-500/40" : "bg-slate-800 text-slate-300"}`;
  const chip = (s: Signal | null, label: string) => (
    <button key={label} onClick={() => set({ signal: s })} className={chipClass(filter === s)}>
      {label}
    </button>
  );
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">プール</h1>
      {venues.length > 1 && (
        <div className="flex flex-wrap gap-2">
          <button onClick={() => set({ venue: null })} className={chipClass(!venue)}>全部の会場</button>
          {venues.map(([id, name]) => (
            <button key={id} onClick={() => set({ venue: id })} className={chipClass(venue === id)}>{name}</button>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {chip(null, `すべて ${inVenue.length}`)}
        {(["green", "yellow", "red"] as const).map((s) => chip(s, `${SIGNAL[s].emoji} ${counts[s] ?? 0}`))}
      </div>
      {data.scores[0]?.epoch_flip && (
        <p className="rounded-lg bg-amber-500/10 px-2 py-1 text-xs text-amber-100">
          ⏰ 次の切り替えは {jstDay(data.scores[0].epoch_flip.at)}（{untilText(data.scores[0].epoch_flip.at)}）。
          切り替えで、来週ボーナスが減ったり、なくなったりするプールがあります。
        </p>
      )}
      <p className="text-xs text-slate-400">純日利の高い順。数字は<Term k="総資産あたり日利" />。値段は最適レンジの<Term k="範囲（レンジ）">範囲</Term>、印は<Term k="保険（ヘッジ）">保険</Term>の有無。</p>
      <ul className="divide-y divide-slate-800 rounded-2xl bg-slate-900 ring-1 ring-slate-800">
        {rows.map((r) => (
          <li key={r.pool_id}>
            <Link to={`/pools/${encodeURIComponent(r.pool_id)}`} className="flex items-center gap-3 px-3 py-2.5">
              <span className="text-lg">{SIGNAL[r.signal].emoji}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-slate-100">{r.pair}</span>
                <span className="block text-xs text-slate-400">
                  {venues.length > 1 && `${r.venue_name ?? r.venue_id} ・ `}
                  {r.best_r != null ? `±${r.best_r}%` : "判定できず"} ・ TVL {bigUsd(r.tvl_usd)}
                </span>
                {r.range_prices && <span className="num block text-xs text-slate-300">{rangeText(r.range_prices)}</span>}
                <span className="mt-0.5 block"><HedgeBadge h={r.hedge_info} /></span>
                {r.reward_held && <span className="mt-0.5 block text-xs text-rose-300">⚠ {r.reward_held}</span>}
                {r.reward_estimate_note && <span className="mt-0.5 block text-xs text-slate-400">ボーナスは{r.reward_estimate_note}</span>}
              </span>
              <span className={`num shrink-0 text-right font-semibold ${r.net_daily_pct == null ? "text-slate-500" : r.net_daily_pct < 0 ? "text-rose-400" : "text-slate-50"}`}>
                {pct(r.net_daily_pct)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
