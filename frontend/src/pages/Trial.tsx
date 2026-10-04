import { useState, type ReactNode } from "react";
import { useApi } from "../api";
import { jst, pct, plainPct, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, PageHead, Pill, Segmented } from "../ui";

// N5d「試す」の結果（2026-10-04 オーナーの指示書）。6つの項目と比べる相手を、見込み・実際・差・判定で1か所に並べる。
// 判定は「確認できた」「要注意」「記録中」の3つだけ。記録が足りないうちは「合格」「不合格」を出さない。

type St = { code: "ok" | "warn" | "rec"; label: string; why: string };
type Gap = { pred: number | null; real: number | null; diff: number | null; diff_pct: number | null };
type YearV = { day_pct: number | null; year_pct: number | null; year_shown: boolean };
type RangeRow = { r_pct: number; days: number; focus: boolean; rebalances: Gap & { ok_share: number | null; pred_median: number | null; real_median: number | null }; in_range: Gap & { ok_share: number | null } };
type EventRow = { key: string; label: string; count: number; with_after: number; after_median_pct: number | null; avoided_mean_pct: number | null; missed_mean_pct: number | null; down_after: number; up_after: number; state: St; case?: string; big_pools?: number | null };
type Base = { kind: string; kind_ja: string; pairs: number; calendar_days: number; first_day: string | null; last_day: string | null; now: YearV; wide: YearV; lend: YearV; nothing: YearV; beat_wide_share: number | null; beat_lend_share: number | null; finding: string | null };
type PracticeRow = { id: number; pair: string; status: string; days: number; rebalances: number; hedges: { perp: string; venue: "main" | "rh" }[]; compare_enabled: boolean; gamma: { pred: number | null; real: number | null }; hedge: { pred: number | null; real: number | null }; rebalance_cost: { pred: number | null; real: number | null }; open_cost_pct: number | null; close_cost_pct: number | null };

interface TrialSummary {
  periods: {
    backtest: { first_day: string | null; last_day: string | null; calendar_days: number; pools: number | null; pool_days: number | null; text: string };
    merkl: { from: string | null; to: string | null; days: number | null; campaigns: number | null };
    lighter: { from: number | null; to: number | null; markets: number | null };
    practice: { from: string | null; days: number | null; positions: number | null };
  };
  sections: {
    bonus: { state: St; decision: string; notes: string[]; summary: { pairs: number | null; pairs_in_range: number; campaigns: number; ratio_median: number | null; denominator_share_median: number | null; out_of_range_paid_share: number | null; days: number | null; skipped: Record<string, number>; errors: { campaign_id: string | null; error: string }[] };
      campaigns: { pair: string; pairs: number; pairs_in_range: number; predicted: number | null; actual: number | null; ratio: number | null; denominator_share: number | null; out_of_range_setting: boolean | null; out_of_range_paid_share: number | null }[] };
    rebalance: { state: St; kinds: Record<string, RangeRow[]>; kind_ja: Record<string, string>; notes: string[] };
    price_loss: { state: St; notes: string[]; kinds: (Gap & { kind: string; kind_ja: string; pools: number; pred_usd_day: number | null; real_usd_day: number | null; ok_share: number; vs_line: number | null })[];
      jumps: { pair: string; kind_ja: string; sigma_pred: number; sigma_real: number; times: number }[] };
    costs: { notes: string[]; items: ({ key: "rebalance"; label: string; state: St; rows: (Gap & { r_pct: number; focus: boolean; slip_pred: number | null; slip_real: number | null; ok_share: number | null })[] }
      | { key: "enter_exit"; label: string; state: St; open_pct: number | null; close_pct: number | null; open_n: number; close_n: number }
      | (Gap & { key: "hedge"; label: string; state: St; markets: number; ok_share: number | null }))[] };
    hedge: {
      notes: string[];
      funding_main: { state: St; rows: (Gap & { perp: string; days: number; ok_share: number | null })[] };
      funding_rh: { state: St; markets: number | null; hedge_markets: number | null; rows: { symbol: string; funding_daily_rh: number | null; funding_daily_main: number | null; mmf_rh_pct: number | null; mmf_main_pct: number | null }[] };
      margins: { state: St; line_pct: number; stay_days: number | null; over: string[]; rows: { symbol: string; rise_pct: number | null; jump_up_pct: number | null; span_days: number | null; over: boolean }[] };
      topups: { state: St; count: number; total_usd: number; cost_usd: number; line_frac: number | null; rows: { pair: string | null; count: number; total_usd: number | null; cost_usd: number | null }[] };
      margin_log: { state: St; rows: { pair: string | null; margin_usd: number | null; equity_usd: number | null; change_pct: number | null; rebalances: number | null }[] };
      practice_venues: Record<string, number>;
    };
    early_exit: { rows: EventRow[]; notes: string[]; practice_watch: { count: number; diff_sum_usd: Record<string, number | null>; state: St } };
  };
  baselines: { kinds: Base[]; wide_r_pct: number | null; lending: { name: string; source: string; checked: string } | null; year_note: string; state: St; notes: string[] };
  practice: { rows: PracticeRow[]; positions: number | null; since: string | null };
  backtest_present: boolean; backtest_text: string | null; computed_at: string | null;
  held: string[]; min_days: number; year_note: string; notes: string[];
}

const TONE = { ok: "g", warn: "y", rec: "n" } as const;
const ICON = { ok: "check", warn: "alert", rec: "clock" } as const;
const LABEL = { ok: "確認できた", warn: "要注意", rec: "記録中" } as const;

function StateBadge({ s }: { s: St }) {
  return <Pill tone={TONE[s.code]} icon={ICON[s.code]}>{s.label}</Pill>;
}

/** 1つの項目の見出し: 判定の印と、その理由 */
function Head({ no, title, s }: { no: string; title: string; s?: St }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <h2 className="bold">{no ? `${no} ` : ""}{title}</h2>
        {s && <StateBadge s={s} />}
      </div>
      {s && <p className="cap">{s.why}</p>}
    </div>
  );
}

const Notes = ({ items }: { items: string[] }) => (
  <ul className="flex list-disc flex-col gap-1 pl-5 cap">{items.map((n) => <li key={n}>{n}</li>)}</ul>
);

const num = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));
const share = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const fracPct = (v: number | null | undefined, d = 2) => (v == null ? "—" : plainPct(v * 100, d));
const day = (s: string | null | undefined) => (s ? s.slice(5).replace("-", "/") : "—");

/** 差の符号: 0 に近いときは付けない。マイナスは「−」 */
function signed(v: number | null, fmt: (v: number | null) => string): string {
  if (v == null) return "—";
  const t = fmt(Math.abs(v));
  if (/^0(\.0+)?( |%|$)/.test(t)) return t;
  return `${v > 0 ? "+" : "−"}${t}`;
}

/** 見込み・実際・差 を横に並べる（スマホでも1行に収まる大きさ） */
function Trio({ pred, real, diff, fmt }: { pred: number | null; real: number | null; diff: number | null; fmt: (v: number | null) => string }) {
  return (
    <div className="grid grid-cols-3 gap-2 text-center">
      {[["見込み", pred], ["実際", real], ["差", diff]].map(([k, v]) => (
        <div key={k as string} className="inset flex flex-col gap-1 p-2">
          <span className="cap">{k}</span>
          <span className="num bold">{k === "差" ? signed(v as number | null, fmt) : fmt(v as number | null)}</span>
        </div>
      ))}
    </div>
  );
}

export default function Trial() {
  const { data, error } = useApi<TrialSummary>("/api/trial/summary", 600_000);
  if (!data) return (<><PageHead title="試すの結果" back={{ to: "/learn", label: "学ぶ" }} /><Loading error={error} rows={4} /></>);
  const s = data.sections;
  return (
    <>
      <PageHead title="試すの結果" back={{ to: "/learn", label: "学ぶ" }} at={data.computed_at} />
      <Overview d={data} />
      <div className="grid items-start gap-4 lg:grid-cols-2 lg:gap-6">
        <div className="flex flex-col gap-4 lg:gap-6">
          <BonusCard b={s.bonus} />
          <RebalanceCard r={s.rebalance} />
          <PriceLossCard p={s.price_loss} />
          <CostsCard c={s.costs} />
        </div>
        <div className="flex flex-col gap-4 lg:gap-6">
          <HedgeCard h={s.hedge} />
          <EarlyExitCard e={s.early_exit} />
          <BaselinesCard b={data.baselines} />
          <PracticeCard rows={data.practice.rows} />
        </div>
      </div>
    </>
  );
}

/** いちばん上: 判定の3つの意味・記録の期間・まだ決めないもの */
function Overview({ d }: { d: TrialSummary }) {
  const p = d.periods;
  const all = [d.sections.bonus.state, d.sections.rebalance.state, d.sections.price_loss.state,
    ...d.sections.costs.items.map((i) => i.state), d.sections.hedge.funding_main.state, d.sections.hedge.margins.state,
    ...d.sections.early_exit.rows.map((r) => r.state)];
  const count = (c: St["code"]) => all.filter((x) => x.code === c).length;
  return (
    <Card title="まとめ">
      <div className="grid grid-cols-3 gap-2 text-center">
        {(["ok", "warn", "rec"] as const).map((c) => (
          <div key={c} className="inset flex flex-col items-center gap-1 p-4">
            <Pill tone={TONE[c]} icon={ICON[c]}>{LABEL[c]}</Pill>
            <span className="t20 num">{count(c)}</span>
          </div>
        ))}
      </div>
      {!d.backtest_present && <p className="sec">{d.backtest_text}</p>}
      <div className="flex flex-col gap-2">
        <Line k="さかのぼり（今の版の写し）" v={`${p.backtest.calendar_days} 日`} note={p.backtest.first_day ? `${p.backtest.first_day} 〜 ${p.backtest.last_day}・${p.backtest.pools ?? "—"} プール` : "まだありません"} />
        <Line k="Merkl の配った額の記録" v={p.merkl.days != null ? `${p.merkl.days.toFixed(1)} 日` : "—"} note={p.merkl.from ? `${jst(p.merkl.from)} 〜 ${jst(p.merkl.to)}` : "まだありません"} />
        <Line k="練習の記録" v={p.practice.days != null ? `${p.practice.days.toFixed(1)} 日` : "—"} note={p.practice.from ? `${jst(p.practice.from)} から・${p.practice.positions} 件` : "練習はまだありません"} />
      </div>
      <div className="inset flex flex-col gap-1 p-4">
        <span className="bold">まだ決めないもの（記録中）</span>
        <span className="sec">{d.held.join("・")}</span>
        <span className="cap">置き直しの式は {d.min_days} 日分たまるまで変えません。たまったら「今の式・実際・差・直す案」を並べてお聞きします。</span>
      </div>
      <Notes items={["確認できた = 記録が足りていて、見込みと実際が目安の中。", "要注意 = 記録が足りていて、見込みと実際の差が目安の外。",
        "記録中 = 記録が足りないので、まだ判定しない（今の数字は途中の値）。", ...d.notes]} />
    </Card>
  );
}

// --- ① ボーナスの取り分 ---
function BonusCard({ b }: { b: TrialSummary["sections"]["bonus"] }) {
  const m = b.summary;
  return (
    <Card>
      <Head no="①" title="ボーナスの取り分（Merkl）" s={b.state} />
      <div className="flex flex-col gap-2">
        <Line k="実際 ÷ 見込み（まん中）" v={m.ratio_median != null ? `${m.ratio_median.toFixed(2)} 倍` : "—"} note="1 に近いほど、今の式どおり" />
        <Line k="見かけ上の分母の割合" v={share(m.denominator_share_median)} note="100% に近い = 全員が分母" />
        <Line k="幅の外でも配られたか" v={m.out_of_range_paid_share == null ? "まだ例がない" : share(m.out_of_range_paid_share)} note="0% なら「幅の外には配らない」のとおり" />
        <Line k="比較できた件数" v={`${m.pairs_in_range} 件`} note={`全部で ${m.pairs ?? 0} 組・${m.campaigns} キャンペーン`} />
        <Line k="実際の記録期間" v={m.days != null ? `${m.days.toFixed(1)} 日` : "—"} />
      </div>
      {m.errors.length > 0 && <p className="cap" style={{ color: "var(--y)" }}>答え合わせが一部できませんでした（{m.errors.length} 件）: {m.errors[0].error}</p>}
      <p className="sec">{b.decision}</p>
      {b.campaigns.length > 0 && (
        <Fold title={<span>キャンペーンごと（{b.campaigns.length}）</span>}>
          {b.campaigns.map((c, i) => (
            <div key={i} className="inset flex flex-col gap-1 p-4">
              <span className="bold">{c.pair}</span>
              <Line k="1回にもらう割合 見込み / 実際" v={`${fracPct(c.predicted, 3)} / ${fracPct(c.actual, 3)}`} />
              <Line k="実際 ÷ 見込み" v={c.ratio != null ? `${c.ratio.toFixed(2)} 倍` : "—"} />
              <Line k="分母の割合" v={share(c.denominator_share)} />
              <Line k="幅の外でも配られたか" v={c.out_of_range_paid_share == null ? "まだ例がない" : share(c.out_of_range_paid_share)}
                note={c.out_of_range_setting ? "キャンペーンの設定: 幅の外にも配る" : "キャンペーンの設定: 幅の中だけ"} />
              <Line k="比べた件数" v={`${c.pairs_in_range} / ${c.pairs}`} />
            </div>
          ))}
        </Fold>
      )}
      {Object.keys(m.skipped).length > 0 && (
        <Fold title="使わなかった区切りと理由">
          {Object.entries(m.skipped).map(([k, v]) => <Line key={k} k={k} v={`${v} 件`} />)}
        </Fold>
      )}
      <Notes items={b.notes} />
    </Card>
  );
}

// --- ② 置き直し ---
function RebalanceCard({ r }: { r: TrialSummary["sections"]["rebalance"] }) {
  const keys = Object.keys(r.kinds);
  const [k, setK] = useState(keys[0] ?? "all");
  const rows = r.kinds[k] ?? [];
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.filter((x) => x.focus);
  return (
    <Card>
      <Head no="②" title="幅の中にいた割合・置き直し" s={r.state} />
      {keys.length > 1 && <Segmented options={keys.map((x) => ({ key: x, label: r.kind_ja[x] ?? x }))} value={k} onChange={setK} />}
      {shown.length === 0 && <p className="cap">まだ比べられる日がありません。</p>}
      {shown.map((x) => (
        <div key={x.r_pct} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between"><span className="bold">±{x.r_pct}%</span><span className="cap">比べた組 {x.days} 件</span></div>
          <span className="cap">置き直しの回数（1日あたり）</span>
          <Trio pred={x.rebalances.pred} real={x.rebalances.real} diff={x.rebalances.diff} fmt={(v) => (v == null ? "—" : `${v.toFixed(1)} 回`)} />
          <Line k="合っていた割合" v={share(x.rebalances.ok_share)} note={`まん中: 見込み ${num(x.rebalances.pred_median)} 回 / 実際 ${num(x.rebalances.real_median)} 回`} />
          <Line k="幅の中にいた割合 見込み / 実際" v={`${share(x.in_range.pred)} / ${share(x.in_range.real)}`} note={`合っていた割合 ${share(x.in_range.ok_share)}`} />
        </div>
      ))}
      <button type="button" className="ghost" onClick={() => setAll(!all)}>{all ? "±0.5%・±2%・±15% だけ出す" : "ほかの幅も出す"}</button>
      <Notes items={r.notes} />
    </Card>
  );
}

// --- ③ 値動きの損 ---
function PriceLossCard({ p }: { p: TrialSummary["sections"]["price_loss"] }) {
  return (
    <Card>
      <Head no="③" title="値動きによる損" s={p.state} />
      {p.kinds.length === 0 && <p className="cap">まだ比べられる日がありません。</p>}
      {p.kinds.map((k) => (
        <div key={k.kind} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between"><span className="bold">{k.kind_ja}</span><span className="cap">{k.pools} プール</span></div>
          <span className="cap">1日の損（建玉のお金に対する %）</span>
          <Trio pred={k.pred} real={k.real} diff={k.diff} fmt={(v) => fracPct(v, 2)} />
          <Line k="ドルでは（1日）見込み / 実際" v={`${usd(k.pred_usd_day)} / ${usd(k.real_usd_day)}`} />
          <Line k="合格の目安（見込みの±30%）との差" v={k.vs_line == null ? "—" : k.vs_line <= 1 ? "目安の中" : `目安の ${k.vs_line.toFixed(1)} 倍`} note={`目安の中だったプール ${share(k.ok_share)}`} />
        </div>
      ))}
      {p.jumps.length > 0 && (
        <Fold title={<span>値動きが急に大きくなった（{p.jumps.length}）</span>} open>
          {p.jumps.slice(0, 10).map((j) => (
            <Line key={j.pair} k={<span>{j.pair} <span className="cap">{j.kind_ja}</span></span>} v={`σ ${fracPct(j.sigma_pred, 1)} → ${fracPct(j.sigma_real, 1)}`} note={`見込みの ${j.times.toFixed(1)} 倍`} />
          ))}
        </Fold>
      )}
      <Notes items={p.notes} />
    </Card>
  );
}

// --- ④ 費用 ---
function CostsCard({ c }: { c: TrialSummary["sections"]["costs"] }) {
  return (
    <Card>
      <Head no="④" title="費用" />
      {c.items.map((it) => (
        <div key={it.key} className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2"><span className="bold">{it.label}</span><StateBadge s={it.state} /></div>
          <p className="cap">{it.state.why}</p>
          {it.key === "rebalance" && it.rows.filter((x) => x.focus).map((x) => (
            <div key={x.r_pct} className="inset flex flex-col gap-2 p-4">
              <span className="bold">±{x.r_pct}%（1日、建玉のお金に対する %）</span>
              <Trio pred={x.pred} real={x.real} diff={x.diff} fmt={(v) => fracPct(v, 3)} />
              <Line k="両替のずれ 見込み / 実際" v={`${fracPct(x.slip_pred, 2)} / ${fracPct(x.slip_real, 2)}`} note={`合っていた割合 ${share(x.ok_share)}`} />
            </div>
          ))}
          {it.key === "enter_exit" && (
            <div className="inset flex flex-col gap-2 p-4">
              <Line k="入る費用（練習の記録の平均）" v={it.open_pct == null ? "—" : plainPct(it.open_pct, 2)} note={`${it.open_n} 件`} />
              <Line k="出る費用（練習の記録の平均）" v={it.close_pct == null ? "—" : plainPct(it.close_pct, 2)} note={`${it.close_n} 件`} />
            </div>
          )}
          {it.key === "hedge" && (
            <div className="inset flex flex-col gap-2 p-4">
              <span className="cap">Lighter 本体・売り1ドルあたり1日（{it.markets} 市場の平均）</span>
              <Trio pred={it.pred} real={it.real} diff={it.diff} fmt={(v) => fracPct(v, 4)} />
            </div>
          )}
        </div>
      ))}
      <Notes items={c.notes} />
    </Card>
  );
}

// --- ⑤ 保険 ---
function HedgeCard({ h }: { h: TrialSummary["sections"]["hedge"] }) {
  const v = h.practice_venues;
  return (
    <Card>
      <Head no="⑤" title="保険" />
      <div className="grid grid-cols-2 gap-2 text-center">
        <div className="inset flex flex-col gap-1 p-4"><span className="cap">練習の保険（本体）</span><span className="t20 num">{v.lighter ?? 0}</span></div>
        <div className="inset flex flex-col gap-1 p-4"><span className="cap">練習の保険（RH版）</span><span className="t20 num">{v.lighter_rh ?? 0}</span></div>
      </div>
      <Sub title="資金調達料・Lighter 本体（USDC）" s={h.funding_main.state}>
        {h.funding_main.rows.length === 0 ? <p className="cap">まだ比べられる日がありません。</p> :
          h.funding_main.rows.slice(0, 12).map((r) => (
            <Line key={r.perp} k={`${r.perp}（${r.days} 日）`} v={`${fracPct(r.pred, 4)} → ${fracPct(r.real, 4)}`} note={`見込み → 実際（1日）・合っていた ${share(r.ok_share)}`} />
          ))}
      </Sub>
      <Sub title="資金調達料・Robinhood Chain 版（USDG）" s={h.funding_rh.state}>
        {h.funding_rh.rows.length === 0 ? <p className="cap">まだ RH版の記録がありません。</p> : (
          <Fold title={<span>保険に使う RH版の市場（{h.funding_rh.rows.length}）</span>}>
            {h.funding_rh.rows.map((r) => (
              <Line key={r.symbol} k={r.symbol} v={`RH版 ${fracPct(r.funding_daily_rh, 4)}`}
                note={`本体 ${fracPct(r.funding_daily_main, 4)}・維持に要る割合 RH版 ${r.mmf_rh_pct == null ? "—" : plainPct(r.mmf_rh_pct, 1)} / 本体 ${r.mmf_main_pct == null ? "—" : plainPct(r.mmf_main_pct, 1)}`} />
            ))}
          </Fold>
        )}
        <p className="cap">1日の資金調達料は、過去7日の平均（売り1ドルあたり）。RH版の見込みと実際の答え合わせは、記録がたまってから出します。</p>
      </Sub>
      <Sub title={`預け金・過去${h.margins.stay_days ?? 14}日の大きな上げ（Lighter 本体の値段）`} s={h.margins.state}>
        {h.margins.over.length > 0 && <p className="sec">{h.margins.line_pct}% を超えた市場: {h.margins.over.join("・")}</p>}
        {h.margins.rows.length > 0 && (
          <Fold title={<span>市場ごと（{h.margins.rows.length}）</span>}>
            {h.margins.rows.map((m) => (
              <Line key={m.symbol} k={m.symbol} v={m.rise_pct == null ? "—" : pct(m.rise_pct, 1)} note={`1時間の大きな上げ ${m.jump_up_pct == null ? "—" : pct(m.jump_up_pct, 1)}・記録 ${num(m.span_days, 0)} 日${m.over ? "・目安を超えた" : ""}`} />
            ))}
          </Fold>
        )}
      </Sub>
      <Sub title="預け金を「足したとしたら」" s={h.topups.state}>
        <Line k="回数" v={`${h.topups.count} 回`} />
        <Line k="合計の額" v={usd(h.topups.total_usd)} note={`見込みの費用 ${usd(h.topups.cost_usd)}`} />
        {h.topups.rows.slice(0, 5).map((t, i) => <Line key={i} k={t.pair ?? "—"} v={`${t.count} 回・${usd(t.total_usd)}`} />)}
      </Sub>
      {h.margin_log.rows.length > 0 && (
        <Sub title="練習の預け金の減り方" s={h.margin_log.state}>
          {h.margin_log.rows.slice(0, 5).map((m, i) => (
            <Line key={i} k={m.pair ?? "—"} v={`${usd(m.margin_usd)} → ${usd(m.equity_usd)}`} note={`${m.change_pct == null ? "—" : pct(m.change_pct, 1)}・置き直し ${m.rebalances ?? 0} 回`} />
          ))}
        </Sub>
      )}
      <Notes items={h.notes} />
    </Card>
  );
}

function Sub({ title, s, children }: { title: string; s?: St; children: ReactNode }) {
  return (
    <div className="inset flex flex-col gap-2 p-4">
      <div className="flex items-start justify-between gap-2"><span className="bold">{title}</span>{s && <StateBadge s={s} />}</div>
      {s && <span className="cap">{s.why}</span>}
      {children}
    </div>
  );
}

// --- ⑥ 早く出る決まり ---
function EarlyExitCard({ e }: { e: TrialSummary["sections"]["early_exit"] }) {
  const w = e.practice_watch;
  return (
    <Card>
      <Head no="⑥" title="早く出る決まり" />
      {e.rows.map((r) => (
        <Sub key={r.key} title={r.label} s={r.state}>
          <Line k="合図が出た回数" v={`${r.count} 回`} note={r.big_pools != null ? `大きいプールだけ ${r.big_pools} 回` : undefined} />
          <Line k="出たあとの値動き（24時間後・まん中）" v={r.after_median_pct == null ? "—" : pct(r.after_median_pct, 1)} note={`下がった ${r.down_after} 回・上がった ${r.up_after} 回（分かった ${r.with_after} 回）`} />
          <Line k="避けられた可能性のある損（平均）" v={r.avoided_mean_pct == null ? "—" : plainPct(r.avoided_mean_pct, 1)} />
          <Line k="早く出て逃した可能性のある利益（平均）" v={r.missed_mean_pct == null ? "—" : plainPct(r.missed_mean_pct, 1)} />
          {r.case && <p className="cap">{r.case}</p>}
        </Sub>
      ))}
      <Sub title="練習: 段階1の合図のあと（出ていたら／残っていたら）" s={w.state}>
        <Line k="記録の数" v={`${w.count} 件`} />
        {(["1", "6", "24"] as const).map((hh) => (
          <Line key={hh} k={`${hh}時間後の差（残っていたら − 出ていたら）`} v={w.diff_sum_usd[hh] == null ? "—" : usd(w.diff_sum_usd[hh])} />
        ))}
      </Sub>
      <Notes items={e.notes} />
    </Card>
  );
}

// --- 比べる相手 ---
function YearCell({ v }: { v: YearV }) {
  if (v.day_pct == null) return <span className="cap">—</span>;
  return (
    <span className="flex flex-col items-end">
      <span className="num">1日 {pct(v.day_pct, 3)}</span>
      <span className="cap">{v.year_shown ? `年 ${pct(v.year_pct, 1)}（参考）` : "年は大きすぎるので出しません"}</span>
    </span>
  );
}

function BaselinesCard({ b }: { b: TrialSummary["baselines"] }) {
  const keys = b.kinds.map((k) => k.kind);
  const [k, setK] = useState(keys.includes("stock") ? "stock" : keys[0] ?? "all");
  const x = b.kinds.find((r) => r.kind === k);
  return (
    <Card>
      <Head no="" title="比べる相手（4つ）" s={b.state} />
      {b.kinds.length === 0 ? <p className="cap">まだ比べられる日がありません。</p> : (
        <>
          <Segmented options={b.kinds.map((r) => ({ key: r.kind, label: r.kind_ja }))} value={k} onChange={setK} />
          {x && (
            <div className="inset flex flex-col gap-2 p-4">
              <span className="cap">比べた組 {x.pairs} 件・実際の期間 {x.calendar_days} 日（{day(x.first_day)} 〜 {day(x.last_day)}）</span>
              <Line k="今のやり方" v={<YearCell v={x.now} />} />
              <Line k={`±${b.wide_r_pct ?? 15}% で広く置きっぱなし`} v={<YearCell v={x.wide} />} />
              <Line k="Aave v3 USDC の貸し出し" v={<YearCell v={x.lend} />} />
              <Line k="何もしない" v={<YearCell v={x.nothing} />} />
              <Line k="今のやり方が勝った組（対 置きっぱなし）" v={share(x.beat_wide_share)} />
              <Line k="今のやり方が勝った組（対 貸し出し）" v={share(x.beat_lend_share)} />
              {x.finding && (
                <div className="flex items-start gap-2 rounded-2xl p-4" style={{ background: "rgba(251,191,36,0.08)" }}>
                  <Icon name="flask" size={16} color="var(--y)" />
                  <span className="sec"><span className="bold">現在の発見: </span>{x.finding}</span>
                </div>
              )}
            </div>
          )}
        </>
      )}
      <p className="cap" style={{ color: "var(--y)" }}>「年」は{b.year_note}。</p>
      <Notes items={b.notes} />
    </Card>
  );
}

// --- 練習の建玉ごと ---
function PracticeCard({ rows }: { rows: PracticeRow[] }) {
  if (rows.length === 0) return null;
  const loss = (v: number | null) => (v == null ? "—" : usd(Math.abs(v)));
  return (
    <Folds>
      <div className="px-6 pt-6 pb-2"><span className="label">練習の建玉ごと（見込みと実際、1日あたり）</span></div>
      {rows.slice(0, 20).map((r) => (
        <Fold key={r.id} title={<span>{r.pair} <span className="cap">{r.status === "open" ? "続いている" : "閉じた"}・{r.days < 0.01 ? "始めたばかり" : `${r.days.toFixed(1)} 日`}</span></span>}>
          {!r.compare_enabled && <p className="cap">始めてから24時間たつまでは、比べません（記録中）。</p>}
          <Line k="値動きの損 見込み / 実際" v={`${loss(r.gamma.pred)} / ${loss(r.gamma.real)}`} />
          <Line k="置き直しの費用 見込み / 実際" v={`${loss(r.rebalance_cost.pred)} / ${loss(r.rebalance_cost.real)}`} note={`置き直し ${r.rebalances} 回`} />
          <Line k="保険の費用 見込み / 実際" v={`${loss(r.hedge.pred)} / ${loss(r.hedge.real)}`}
            note={r.hedges.length ? r.hedges.map((h) => `${h.perp}（${h.venue === "rh" ? "RH版" : "本体"}）`).join("・") : "保険なし"} />
          <Line k="入る費用" v={r.open_cost_pct == null ? "—" : plainPct(r.open_cost_pct, 2)} note={r.close_cost_pct == null ? undefined : `出る費用 ${plainPct(r.close_cost_pct, 2)}`} />
        </Fold>
      ))}
    </Folds>
  );
}

