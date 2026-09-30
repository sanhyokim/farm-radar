import { useState } from "react";
import { postApi, useApi, type Discovery, type DiscoveryDecision, type DiscoveryPool, type DiscoveryVenue } from "../api";
import { bigUsd, jst, pct } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Loading, Note, Pill, Term } from "../ui";

/** 候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1）。読み取りだけで、お金は動かさない */

const DECISION: Record<DiscoveryDecision, { label: string; tone: "g" | "y" | "n" }> = {
  study: { label: "調べる", tone: "g" },
  hold: { label: "保留", tone: "y" },
  skip: { label: "見送り", tone: "n" },
};
type Filter = "open" | DiscoveryDecision | "all";
const FILTERS: [Filter, string][] = [["open", "まだ決めていない"], ["study", "調べる"], ["hold", "保留"], ["skip", "見送り"], ["all", "すべて"]];
type Decide = (k: string, s: DiscoveryDecision | null) => void;

export function DiscoverySection() {
  const { data, error, reload } = useApi<Discovery>("/api/discovery");
  const [filter, setFilter] = useState<Filter>("open");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(5);
  if (!data) return <Loading error={error} rows={1} />;

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
  const match = (v: DiscoveryVenue, f: Filter) => (f === "all" ? true : f === "open" ? !v.decision : v.decision === f);
  const shown = all.filter((v) => match(v, filter));
  const open = all.filter((v) => match(v, "open")).length;
  const run = data.last_run;

  return (
    <>
      <Card title={<Term k="候補の会場">新しい会場の候補（週1回）</Term>}
        right={<span className="cap">{data.last_ok_at ? `${jst(data.last_ok_at)} 更新` : "まだ集めていません"}</span>}>
        <div className="t20">{open > 0 ? `まだ決めていない候補が ${open} 件あります` : "決めることはありません"}</div>
        {run && run.status === "partial" && <Pill tone="y" icon="alert">前回は一部のサイトを読めませんでした（読めた分だけ出しています）</Pill>}
        {run && run.status === "error" && <Pill tone="r" icon="alert">前回は読み取りに失敗しました</Pill>}
        {run && run.status === "error" && run.error && <p className="cap">{run.error}</p>}

        <div className="scroll-x -mx-6 px-6">
          <div className="flex gap-2">
            {FILTERS.map(([f, label]) => (
              <button key={f} type="button" className="chip" aria-pressed={filter === f} onClick={() => { setFilter(f); setLimit(5); }}>
                {label}<span className="cap num">{all.filter((v) => match(v, f)).length}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-2">
          {shown.length === 0 && <p className="sec">ここに出す候補はありません。</p>}
          {shown.slice(0, limit).map((v) => <VenueItem key={v.key} v={v} busy={busy} onDecide={decide} />)}
          {shown.length > limit && (
            <button type="button" className="ghost justify-center" onClick={() => setLimit(shown.length)}>
              <Icon name="down" size={16} />残りの{shown.length - limit}件を見る
            </button>
          )}
        </div>
        {msg && <p>{msg}</p>}
        <Note>
          「調べる」を押しても、自動で監視や練習は始まりません。アドレスと報酬の仕組みを出典つきで確かめてから、会場として足します。
          「調べる」にした会場は、チャットで知らせてください。
        </Note>
      </Card>

      <Folds>
        <Fold title="集め方と今すぐ更新">
          <p className="sec">{data.schedule_ja}に DefiLlama と GeckoTerminal から集めます（次は {jst(data.next_run)}）。見るチェーン: {data.chains.join("・")}。</p>
          <p className="cap">{data.criteria_ja}</p>
          <div>
            <button type="button" onClick={refresh} disabled={busy || !!data.refresh_block} className="ghost">
              <Icon name="refresh" size={16} />今すぐ更新
            </button>
          </div>
          {data.refresh_block && <p className="cap">{data.refresh_block}</p>}
        </Fold>
        <Fold title={`Robinhood Chain の新しい DEX（${data.new_dexes.length}件）`}>
          <NewDexes data={data} busy={busy} onDecide={decide} />
        </Fold>
        <Fold title={`新しくボーナスが出始めたプール（${data.new_pools.length}件）`}>
          {data.new_pools.length === 0 ? <p className="sec">ありません。</p> : <PoolTable pools={data.new_pools} showVenue />}
          <Note>DefiLlama に記録が載り始めてから30日以内の、ボーナスの出ているプールです（いま見ている up. は除く）。利回りは DefiLlama の計算で、急に変わることがあります。</Note>
        </Fold>
      </Folds>
    </>
  );
}

function DecisionButtons({ v, busy, onDecide }: { v: DiscoveryVenue; busy: boolean; onDecide: Decide }) {
  return (
    <div className="grid grid-cols-3 gap-2">
      {(Object.keys(DECISION) as DiscoveryDecision[]).map((s) => (
        <button key={s} type="button" disabled={busy} aria-pressed={v.decision === s} className="chip justify-center"
          onClick={() => onDecide(v.key, v.decision === s ? null : s)}>
          {v.decision === s && <Icon name="check" size={16} />}{DECISION[s].label}
        </button>
      ))}
    </div>
  );
}

function VenueItem({ v, busy, onDecide }: { v: DiscoveryVenue; busy: boolean; onDecide: Decide }) {
  const [open, setOpen] = useState(false);
  const tokens = (v.reward_token_info ?? []).filter((t) => t.symbol);
  return (
    <div className="inset flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="bold">{v.name}</span>
        <Pill>{v.chain}</Pill>
        {v.new && <Pill>新着</Pill>}
        {v.decision && <Pill tone={DECISION[v.decision].tone}>{DECISION[v.decision].label}</Pill>}
        {v.not_in_latest && <Pill>今週の一覧にはない</Pill>}
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="cap"><Term k="週のボーナスの目安">週のボーナス</Term></div>
          <div className="bold num">{v.weekly_reward_usd == null ? "なし" : bigUsd(v.weekly_reward_usd)}</div>
          {v.weekly_ratio_pct != null && <div className="cap num">預かり額の {v.weekly_ratio_pct.toFixed(1)}%</div>}
        </div>
        <div>
          <div className="cap"><Term k="TVL">預かり額</Term></div>
          <div className="bold num">{bigUsd(v.tvl_usd)}</div>
          {v.change_7d_pct != null && <div className="cap num">7日で {pct(v.change_7d_pct, 0)}{v.multi_chain && "（全チェーン）"}</div>}
        </div>
      </div>
      <button type="button" onClick={() => setOpen(!open)} className="flex items-center gap-2 text-left text-sec" aria-expanded={open}>
        <Icon name={open ? "up" : "down"} size={16} />{open ? "たたむ" : "詳しく見る"}
      </button>
      {open && (
        <>
          <div className="flex flex-col gap-2">
            <div className="sec">
              ボーナスのトークン:{" "}
              {tokens.length === 0 ? "—" : tokens.slice(0, 3).map((t, i) => (
                <span key={i} className="num text-ink">
                  {t.symbol}{t.change_7d_pct != null && <span className="text-sec">（7日で {pct(t.change_7d_pct, 0)}）</span>}
                  {i < Math.min(tokens.length, 3) - 1 && "・"}
                </span>
              ))}
            </div>
            <div className="sec">
              {v.age_days != null ? <>掲載 <span className="num text-ink">{v.age_days}日前</span></> : "掲載日 —"}
              {" ・ "}監査の記録 <span className="text-ink">{v.audit_links && v.audit_links.length > 0 ? "あり" : "なし"}</span>
            </div>
            <div className="sec">
              <Term k="保険（ヘッジ）">保険のきくペア</Term>:{" "}
              {v.hedge_pools && v.hedge_pools.length > 0 ? <span className="text-ink">{v.hedge_pools.join("、")}</span> : "見つかりません"}
              {v.hedge_pools && v.hedge_pools.length > 0 && <span className="cap block">記号が同じだけで、同じトークンかは未確認</span>}
            </div>
          </div>
          {v.top_pools && v.top_pools.length > 0 && (
            <div className="flex flex-col gap-2">
              <span className="cap">ボーナスの高いプール（{v.reward_pools_n}件中 {v.top_pools.length}件）</span>
              <PoolTable pools={v.top_pools} />
            </div>
          )}
          {(v.url || v.audit_links?.[0]) && (
            <div className="flex flex-wrap gap-4">
              {v.url && <a href={v.url} target="_blank" rel="noreferrer" className="flex items-center gap-2 text-sec"><Icon name="link" size={16} />公式サイト</a>}
              {v.audit_links?.[0] && <a href={v.audit_links[0]} target="_blank" rel="noreferrer" className="flex items-center gap-2 text-sec"><Icon name="link" size={16} />監査のページ</a>}
            </div>
          )}
        </>
      )}
      <DecisionButtons v={v} busy={busy} onDecide={onDecide} />
    </div>
  );
}

function PoolTable({ pools, showVenue = false }: { pools: DiscoveryPool[]; showVenue?: boolean }) {
  return (
    <table className="tbl">
      <thead>
        <tr><th>プール</th><th className="r">預かり額</th><th className="r">ボーナス/年</th></tr>
      </thead>
      <tbody>
        {pools.map((p) => (
          <tr key={p.pool}>
            <td>
              {p.symbol}
              {p.hedge.ok && <span className="cap"> ・ 保険あり</span>}
              {showVenue && <span className="cap block">{p.venue_name}（{p.chain}）</span>}
              {p.outlier && <span className="cap block">DefiLlama が「外れ値」の印</span>}
            </td>
            <td className="num r sec">{bigUsd(p.tvl_usd)}</td>
            <td className="num r">{p.apy_reward == null ? "—" : `${p.apy_reward.toLocaleString("en-US", { maximumFractionDigits: 0 })}%`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewDexes({ data, busy, onDecide }: { data: Discovery; busy: boolean; onDecide: Decide }) {
  return (
    <>
      {data.gt_dex_count === 0 ? (
        <p className="sec">まだ集めていません。</p>
      ) : data.new_dexes.length === 0 ? (
        <p className="sec">前回から増えた DEX はありません（いま {data.gt_dex_count} 件を記録しています）。</p>
      ) : (
        <div className="flex flex-col gap-2">
          {data.new_dexes.map((d) => (
            <div key={d.key} className="inset flex flex-col gap-2 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="bold">{d.name}</span>
                <Pill>新着</Pill>
                {d.decision && <Pill tone={DECISION[d.decision].tone}>{DECISION[d.decision].label}</Pill>}
              </div>
              {d.tvl_top_usd != null && <div className="sec">取引の多いプールの預かり額の合計 <span className="num text-ink">{bigUsd(d.tvl_top_usd)}</span></div>}
              {d.pools && d.pools.length > 0 && (
                <ul className="sec">
                  {d.pools.slice(0, 3).map((p, i) => <li key={i}>{p.name} ・ <span className="num">{bigUsd(p.tvl_usd)}</span>{p.hedge.ok && " ・ 保険あり"}</li>)}
                </ul>
              )}
              {d.url && <a href={d.url} target="_blank" rel="noreferrer" className="flex items-center gap-2 text-sec"><Icon name="link" size={16} />GeckoTerminal で見る</a>}
              <DecisionButtons v={d} busy={busy} onDecide={onDecide} />
            </div>
          ))}
        </div>
      )}
      <Note>ボーナスがあるかどうかは GeckoTerminal ではわかりません。気になる DEX は「調べる」にしてください。</Note>
    </>
  );
}
