import { Link, useSearchParams } from "react-router-dom";
import { useApi, type PoolRow, type Signal } from "../api";
import { SIGNAL, bigUsd, pct, rangeText } from "../format";
import { HedgeBadge } from "./Home";
import { Loading, Term } from "../ui";

type Scores = { counts: Record<Signal, number>; scores: PoolRow[] };

export default function Pools() {
  const { data, error } = useApi<Scores>("/api/scores");
  const [params, setParams] = useSearchParams();
  const filter = (params.get("signal") as Signal | null) ?? null;
  if (!data) return <Loading error={error} />;
  const rows = data.scores.filter((r) => !filter || r.signal === filter);
  const chip = (s: Signal | null, label: string) => (
    <button
      key={label}
      onClick={() => setParams(s ? { signal: s } : {})}
      className={`rounded-full px-3 py-1 text-sm ${filter === s ? "bg-sky-500/20 text-sky-200 ring-1 ring-sky-500/40" : "bg-slate-800 text-slate-300"}`}
    >
      {label}
    </button>
  );
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">プール</h1>
      <div className="flex flex-wrap gap-2">
        {chip(null, `すべて ${data.scores.length}`)}
        {(["green", "yellow", "red"] as const).map((s) => chip(s, `${SIGNAL[s].emoji} ${data.counts[s] ?? 0}`))}
      </div>
      <p className="text-xs text-slate-400">純日利の高い順。数字は<Term k="総資産あたり日利" />。値段は最適レンジの<Term k="範囲（レンジ）">範囲</Term>、印は<Term k="保険（ヘッジ）">保険</Term>の有無。</p>
      <ul className="divide-y divide-slate-800 rounded-2xl bg-slate-900 ring-1 ring-slate-800">
        {rows.map((r) => (
          <li key={r.pool_id}>
            <Link to={`/pools/${encodeURIComponent(r.pool_id)}`} className="flex items-center gap-3 px-3 py-2.5">
              <span className="text-lg">{SIGNAL[r.signal].emoji}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-slate-100">{r.pair}</span>
                <span className="block text-xs text-slate-400">
                  {r.best_r != null ? `±${r.best_r}%` : "判定できず"} ・ TVL {bigUsd(r.tvl_usd)}
                </span>
                {r.range_prices && <span className="num block text-xs text-slate-300">{rangeText(r.range_prices)}</span>}
                <span className="mt-0.5 block"><HedgeBadge h={r.hedge_info} /></span>
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
