import { useState } from "react";
import { postApi, useApi, type Discovery, type DiscoveryDecision, type DiscoveryPool, type DiscoveryVenue } from "../api";
import { bigUsd, jst, pct } from "../format";
import { Badge, Card, Loading, Note, Term } from "../ui";

/** 候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1）。読み取りだけで、お金は動かさない */

const DECISION: Record<DiscoveryDecision, { label: string; tone: "emerald" | "amber" | "slate" }> = {
  study: { label: "調べる", tone: "emerald" },
  hold: { label: "保留", tone: "amber" },
  skip: { label: "見送り", tone: "slate" },
};
type Filter = "open" | DiscoveryDecision | "all";
const FILTERS: [Filter, string][] = [["open", "まだ決めていない"], ["study", "調べる"], ["hold", "保留"], ["skip", "見送り"], ["all", "すべて"]];

export function DiscoverySection() {
  const { data, error, reload } = useApi<Discovery>("/api/discovery");
  const [filter, setFilter] = useState<Filter>("open");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!data) return <Card title="新しい会場の候補（週1回）"><Loading error={error} /></Card>;

  const decide = async (key: string, status: DiscoveryDecision | null) => {
    setBusy(true);
    try {
      const r = await postApi<{ message: string }>("/api/discovery/decision", { key, status });
      setMsg(r.message);
      reload();
    } catch (e) {
      setMsg(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  const refresh = async () => {
    setBusy(true);
    try {
      const r = await postApi<{ message: string }>("/api/discovery/refresh");
      setMsg(r.message);
      window.setTimeout(reload, 90_000);
    } catch (e) {
      setMsg(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const all = [...data.venues, ...data.decided_elsewhere];
  const shown = all.filter((v) => (filter === "all" ? true : filter === "open" ? !v.decision : v.decision === filter));
  const count = (f: Filter) => all.filter((v) => (f === "all" ? true : f === "open" ? !v.decision : v.decision === f)).length;
  const run = data.last_run;

  return (
    <div className="space-y-3">
      <Card title={<Term k="候補の会場">新しい会場の候補（週1回）</Term>}
        right={<span className="text-xs text-slate-400">{data.last_ok_at ? `${jst(data.last_ok_at)} 更新` : "まだ集めていません"}</span>}>
        <p className="text-xs text-slate-400">
          {data.schedule_ja}に DefiLlama と GeckoTerminal から集めます（次は {jst(data.next_run)}）。見るチェーン: {data.chains.join("・")}。
        </p>
        <p className="mt-1 text-xs text-slate-400">{data.criteria_ja}</p>
        {run && run.status === "partial" && <p className="mt-1 text-xs text-amber-300">前回は一部のサイトを読めませんでした（読めた分だけ出しています）。</p>}
        {run && run.status === "error" && <p className="mt-1 text-xs text-rose-300">前回は読み取りに失敗しました。{run.error}</p>}
        <button onClick={refresh} disabled={busy || !!data.refresh_block}
          className="mt-2 w-full rounded-xl bg-sky-700 py-2 text-sm font-bold text-white disabled:bg-slate-700 disabled:text-slate-400">
          今すぐ更新
        </button>
        {data.refresh_block && <p className="mt-1 text-xs text-slate-400">{data.refresh_block}</p>}
        {msg && <p className="mt-2 text-sm text-slate-200">{msg}</p>}

        <div className="mt-3 flex flex-wrap gap-1.5">
          {FILTERS.map(([f, label]) => (
            <button key={f} onClick={() => setFilter(f)}
              className={`rounded-full px-3 py-1 text-xs ${filter === f ? "bg-sky-500/20 text-sky-200 ring-1 ring-sky-500/40" : "bg-slate-800 text-slate-300"}`}>
              {label} {count(f)}
            </button>
          ))}
        </div>
        <div className="mt-2 space-y-2">
          {shown.length === 0 && <p className="text-sm text-slate-400">ここに出す候補はありません。</p>}
          {shown.map((v) => <VenueItem key={v.key} v={v} busy={busy} onDecide={decide} />)}
        </div>
        <Note>
          「調べる」を押しても、自動で監視や練習は始まりません。アドレスと報酬の仕組みを出典つきで確かめてから、会場として足します。
          「調べる」にした会場は、チャットで知らせてください。
        </Note>
      </Card>

      <NewDexes data={data} busy={busy} onDecide={decide} />
      <NewPools pools={data.new_pools} />
    </div>
  );
}

function DecisionButtons({ v, busy, onDecide }: { v: DiscoveryVenue; busy: boolean; onDecide: (k: string, s: DiscoveryDecision | null) => void }) {
  return (
    <div className="mt-2 grid grid-cols-3 gap-1.5">
      {(Object.keys(DECISION) as DiscoveryDecision[]).map((s) => (
        <button key={s} disabled={busy} onClick={() => onDecide(v.key, v.decision === s ? null : s)}
          className={`rounded-lg py-1.5 text-xs font-bold disabled:opacity-50 ${v.decision === s ? "bg-sky-600 text-white" : "bg-slate-700 text-slate-200"}`}>
          {v.decision === s ? `✓ ${DECISION[s].label}` : DECISION[s].label}
        </button>
      ))}
    </div>
  );
}

function VenueItem({ v, busy, onDecide }: { v: DiscoveryVenue; busy: boolean; onDecide: (k: string, s: DiscoveryDecision | null) => void }) {
  const [open, setOpen] = useState(false);
  const tokens = (v.reward_token_info ?? []).filter((t) => t.symbol);
  return (
    <div className="rounded-lg bg-slate-800/40 p-2 text-sm">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-semibold text-slate-100">{v.name}</span>
        <Badge tone="sky">{v.chain}</Badge>
        {v.new && <Badge tone="emerald">新着</Badge>}
        {v.decision && <Badge tone={DECISION[v.decision].tone}>{DECISION[v.decision].label}</Badge>}
        {v.not_in_latest && <Badge>今週の一覧にはない</Badge>}
      </div>
      <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-slate-400">
        <div>
          <Term k="週のボーナスの目安">週のボーナス</Term>{" "}
          <span className="num text-slate-100">{v.weekly_reward_usd == null ? "なし" : bigUsd(v.weekly_reward_usd)}</span>
          {v.weekly_ratio_pct != null && <span className="num text-slate-300">（預かり額の {v.weekly_ratio_pct.toFixed(1)}%）</span>}
        </div>
        <div>
          <Term k="TVL">預かり額</Term> <span className="num text-slate-100">{bigUsd(v.tvl_usd)}</span>
          {v.change_7d_pct != null && <span className={`num ${v.change_7d_pct < 0 ? "text-rose-400" : "text-emerald-400"}`}> 7日{pct(v.change_7d_pct, 0)}</span>}
          {v.multi_chain && <span className="text-slate-500">（7日の変化は全チェーン）</span>}
        </div>
        <div>
          ボーナスのトークン{" "}
          {tokens.length === 0 ? <span className="text-slate-300">—</span> : tokens.slice(0, 3).map((t, i) => (
            <span key={i} className="num">
              <span className="text-slate-200">{t.symbol}</span>
              {t.change_7d_pct != null && <span className={t.change_7d_pct < 0 ? "text-rose-400" : "text-emerald-400"}> {pct(t.change_7d_pct, 0)}</span>}
              {i < Math.min(tokens.length, 3) - 1 && "・"}
            </span>
          ))}
          <span className="text-slate-500">（7日）</span>
        </div>
        <div>
          {v.age_days != null ? <>掲載 <span className="num text-slate-200">{v.age_days}日前</span></> : "掲載日 —"}
          {" ・ "}監査の記録 {v.audit_links && v.audit_links.length > 0 ? <span className="text-slate-200">あり</span> : <span className="text-slate-200">なし</span>}
        </div>
      </div>
      <div className="mt-1 text-xs text-slate-400">
        <Term k="保険（ヘッジ）">保険のきくペア</Term>:{" "}
        {v.hedge_pools && v.hedge_pools.length > 0 ? <span className="text-slate-200">{v.hedge_pools.join("、")}</span> : "見つかりません"}
        {v.hedge_pools && v.hedge_pools.length > 0 && <span className="text-slate-500">（記号が同じだけで、同じトークンかは未確認）</span>}
      </div>
      {(v.top_pools?.length ?? 0) > 0 && (
        <button onClick={() => setOpen(!open)} className="mt-1 text-xs text-sky-300">
          {open ? "▲ プールを閉じる" : `▼ ボーナスの高いプール（${v.reward_pools_n}件中 ${v.top_pools!.length}件）`}
        </button>
      )}
      {open && v.top_pools && <PoolTable pools={v.top_pools} />}
      <div className="mt-1 flex gap-3 text-xs">
        {v.url && <a href={v.url} target="_blank" rel="noreferrer" className="text-sky-300">公式サイト</a>}
        {v.audit_links?.[0] && <a href={v.audit_links[0]} target="_blank" rel="noreferrer" className="text-sky-300">監査のページ</a>}
      </div>
      <DecisionButtons v={v} busy={busy} onDecide={onDecide} />
    </div>
  );
}

function PoolTable({ pools, showVenue = false }: { pools: DiscoveryPool[]; showVenue?: boolean }) {
  return (
    <table className="mt-1 w-full text-xs">
      <thead>
        <tr className="text-slate-500">
          <th className="text-left font-normal">プール</th>
          <th className="text-right font-normal">預かり額</th>
          <th className="text-right font-normal">ボーナス/年</th>
        </tr>
      </thead>
      <tbody>
        {pools.map((p) => (
          <tr key={p.pool} className="border-t border-slate-800">
            <td className="py-1 text-slate-200">
              {p.symbol}
              {showVenue && <span className="block text-slate-500">{p.venue_name}（{p.chain}）</span>}
              {p.hedge.ok && <span className="ml-1 text-emerald-400">保険◯</span>}
              {p.outlier && <span className="block text-amber-300">DefiLlama が「外れ値」の印</span>}
            </td>
            <td className="num text-right text-slate-300">{bigUsd(p.tvl_usd)}</td>
            <td className="num text-right text-slate-100">{p.apy_reward == null ? "—" : `${p.apy_reward.toLocaleString("en-US", { maximumFractionDigits: 0 })}%`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewDexes({ data, busy, onDecide }: { data: Discovery; busy: boolean; onDecide: (k: string, s: DiscoveryDecision | null) => void }) {
  return (
    <Card title="Robinhood Chain の新しい DEX（GeckoTerminal）">
      {data.gt_dex_count === 0 ? (
        <p className="text-sm text-slate-400">まだ集めていません。</p>
      ) : data.new_dexes.length === 0 ? (
        <p className="text-sm text-slate-400">前回から増えた DEX はありません（いま {data.gt_dex_count} 件を記録しています）。</p>
      ) : (
        <div className="space-y-2">
          {data.new_dexes.map((d) => (
            <div key={d.key} className="rounded-lg bg-slate-800/40 p-2 text-sm">
              <div className="flex items-center gap-1.5">
                <span className="font-semibold text-slate-100">{d.name}</span>
                <Badge tone="emerald">新着</Badge>
                {d.decision && <Badge tone={DECISION[d.decision].tone}>{DECISION[d.decision].label}</Badge>}
              </div>
              {d.tvl_top_usd != null && <div className="mt-1 text-xs text-slate-400">取引の多いプールの預かり額の合計 <span className="num text-slate-100">{bigUsd(d.tvl_top_usd)}</span></div>}
              {d.pools && d.pools.length > 0 && (
                <ul className="mt-1 text-xs text-slate-300">
                  {d.pools.slice(0, 3).map((p, i) => <li key={i}>{p.name} ・ {bigUsd(p.tvl_usd)}{p.hedge.ok && <span className="text-emerald-400"> 保険◯</span>}</li>)}
                </ul>
              )}
              {d.url && <a href={d.url} target="_blank" rel="noreferrer" className="mt-1 inline-block text-xs text-sky-300">GeckoTerminal で見る</a>}
              <DecisionButtons v={d} busy={busy} onDecide={onDecide} />
            </div>
          ))}
        </div>
      )}
      <Note>ボーナスがあるかどうかは GeckoTerminal ではわかりません。気になる DEX は「調べる」にしてください。</Note>
    </Card>
  );
}

function NewPools({ pools }: { pools: DiscoveryPool[] }) {
  return (
    <Card title="新しくボーナスが出始めたプール（30日以内）">
      {pools.length === 0 ? <p className="text-sm text-slate-400">ありません。</p> : <PoolTable pools={pools} showVenue />}
      <Note>DefiLlama に記録が載り始めてから30日以内の、ボーナスの出ているプールです（いま見ている up. は除く）。利回りは DefiLlama の計算で、急に変わることがあります。</Note>
    </Card>
  );
}
