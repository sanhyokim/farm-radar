import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { postApi, useApi } from "../api";
import { jst, pct, plainPct, signedUsd, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Note, PageHead, Pill, Segmented, Spark } from "../ui";

// N6「仮想のお金で渡る」（2026-10-07 指示書）。100% 仮想。練習の画面のいちばん上と、建玉の画面

interface Margin { start: number | null; equity: number | null; maintenance: number | null; liquidated: string | null; topups: number; topup_usd: number }
interface HoldDiff { held_usd: number; sold_net_usd: number; sold_usd: number; diff_usd: number }
export interface N6Pos {
  id: number; portfolio_id: string; twin_of: number | null; opp_key: string; name: string | null; pair: string | null;
  chain: string | null; venue: string | null; kind: string; hedge: boolean; amount_usd: number; opened_at: string;
  closed_at: string | null; status: string; est_apr_pct: number | null; est_old_apr_pct: number | null;
  pick_reason: string | null; exit_reason: string | null; exit_rule: string | null; exit_rule_ja: string | null;
  exit_stage: number | null; move_from: number | null; move_to: number | null; move_reason: string | null;
  value_usd: number | null; pnl_usd: number | null; pnl_pct: number | null; actual_apr_pct: number | null;
  bonus_usd: number | null; fees_usd: number | null; price_move_usd: number | null; rebalance_cost_usd: number | null;
  hedge_usd: number | null; hedge_cost_usd: number | null; funding_usd: number | null; gas_usd: number | null;
  entry_exit_cost_usd: number | null; max_drawdown_usd: number | null; rebalances: number | null;
  in_range_pct: number | null; range_pct: number | null; geo: string | null; price_src: string | null;
  valued_at: string | null; days: number; margin: Margin | null; bonus_hold?: HoldDiff;
}
interface TwinSide { pnl_usd: number | null; price_move_usd: number | null; hedge_usd: number | null; funding_usd: number | null;
  hedge_cost_usd?: number | null; bonus_usd: number | null; max_drawdown_usd: number | null; rebalances: number | null; id: number;
  margin?: Margin | null; closed_at?: string | null; exit_reason?: string | null }
interface Twin { main_id: number; name: string | null; amount_usd: number; status: string; opened_at?: string;
  closed_at?: string | null; exit_reason?: string | null; hedged: TwinSide; unhedged: TwinSide }
interface Miss { key: string; name: string | null; venue: string | null; chain: string | null; chain_name: string | null;
  apr_pct: number; danger: string | null; danger_label: string | null; code: string; reason: string; gap_pt: number;
  portfolio_id?: string; ts?: string }
interface CodeN { code: string; ja: string; n: number }
interface Funnel { error?: string; total: number; usable: number; reach: number; final: number; target: number; entered: number;
  state: string; reach_cap_dropped: number; reach_danger_dropped: number; reach_dropped: CodeN[]; counts: CodeN[];
  best_miss: Miss | null; near: Miss[]; reason: string | null }
interface FirstEntry { portfolio_id: string; size_ja: string; picker_ja: string; started_at: string; entered: boolean;
  position_id?: number; opened_at?: string; waited_hours?: number; name?: string | null; pair?: string | null; venue?: string | null;
  chain_name?: string | null; amount_usd?: number; est_apr_pct?: number | null; danger_label?: string | null; hedge?: boolean;
  pick_reason?: string | null }
interface CapDiff { small: string; big: string; ticks: number; latest_ts: string | null;
  latest: { key: string; name: string | null; venue: string | null; apr_pct: number; small_ja: string; big_ja: string }[];
  tally: { small_ja: string; big_ja: string; n: number }[] }
interface Health { ok: boolean; checked_at: string; problems: string[]; notes: string[]; last_tick: string | null; ticks: number }
interface DailyPf { id: string; size_ja: string; picker_ja: string; waiting_ticks: number; entered: number; exited: number; moved: number;
  max_positions: number; pnl_usd: number; pnl_pct: number | null; max_drawdown_usd: number;
  wait_reasons: { text: string; n: number }[]; exit_reasons: { text: string; n: number }[] }
interface DailyRow { date: string; ticks: number; ticks_ok: number; ticks_failed: number; portfolios: DailyPf[]; max_positions: number;
  wait_reasons: { text: string; n: number }[]; exit_reasons: { text: string; n: number }[]; best_miss: Miss | null;
  hedge_vs: { pairs: number; hedged_pnl_usd: number; unhedged_pnl_usd: number };
  new_vs_old: { rebalances_new: number; rebalances_old: number; loss_day_new: number; loss_day_old: number } }
interface LossRow { pct: number; usd: number; base: number; level: string | null; line: Record<string, number> }
interface Portfolio {
  id: string; picker: "app" | "owner"; picker_ja: string; total_usd: number; status: string; stopped_reason: string | null;
  started_at: string; cash_usd: number; placed_usd: number; equity_usd: number; pnl_usd: number; pnl_pct: number;
  max_drawdown_usd: number; loss: { periods: Record<string, LossRow>; old_day_level: string | null } | null;
  waiting: string | null; open: N6Pos[]; closed: N6Pos[];
  rules_fired: { action: string; action_ja: string; rule: string | null; rule_ja: string | null; count: number }[];
  twins: Twin[]; funnel: Funnel | null;
}
interface N6Event { id: number; ts: string; portfolio_id: string | null; position_id: number | null; action: string;
  action_ja: string; rule: string | null; rule_ja: string | null; stage: number | null; shadow: number; message_ja: string }
interface N6Overview {
  enabled: boolean; virtual_ja: string; last_tick: { ts: string; ok: boolean; error: string | null } | null;
  rules: { new: { rebalance_factor: number; edge_buffer_pct: number; edge_wait_minutes: number; loss_day: Record<string, number> };
    old: { rebalance_factor: number; edge_buffer_pct: number; edge_wait_minutes: number; loss_day: Record<string, number> };
    loss_week: Record<string, number>; loss_since_start: Record<string, number>; bonus_sale_hour_jst: number };
  level_ja: Record<string, string>; portfolios: Portfolio[];
  app_vs_own: { size: number; since: string; own_pnl_usd: number; app_pnl_usd: number; own_pnl_pct: number; app_pnl_pct: number }[];
  shadow: { positions: number; position_days: number; rebalances: { new: number; old: number; est_new: number; est_old: number };
    rebalance_cost: { new: number; old: number }; in_range_pct: { new: number | null; old: number | null };
    bonus: { split: number; a: number }; loss_day_events: { new: number; old: number } };
  events: N6Event[]; requests: { id: number; ts: string; portfolio_id: string; opp_key: string; status: string; message_ja: string | null }[];
  bonus_sales: { count: number; usd: number; cost_usd: number }; owner_sizes: number[];
  ticks?: number; first_entries?: FirstEntry[]; cap_diff?: CapDiff | null; health?: Health;
}
interface N6Detail extends N6Pos {
  entry: Record<string, unknown> | null; marks: { ts: string; value_usd: number; pnl_usd: number }[];
  events: N6Event[]; sales: { day: string; symbol: string; units: number; price: number; usd: number; cost_usd: number }[];
  manual_exit: { ts: string; reason_ja: string; note: string | null; pnl_usd: number } | null; twin: Twin | null;
  reasons_ja: Record<string, string>;
  state: { legs: { sym: string; size: number; market: string; book: string }[]; lower: number | null; upper: number | null;
    P: number; topups: { ts: string; add_usd: number; buffer_usd: number; buffer0_usd: number }[] | null; gap_hours: number;
    old: { count: number; cost: number; bonus_usd: number } | null };
}

const PERIOD_JA: Record<string, string> = { day: "今日", week: "7日", since_start: "始めてから" };
const LEVEL_TONE: Record<string, "y" | "r"> = { caution: "y", no_new: "y", stop: "r" };

function sizeLabel(n: number) { return `$${n.toLocaleString("en-US")}`; }

/** 練習の画面のいちばん上: N6 の $1,000 / $10,000・アプリ任せ / 自分で選ぶ */
export function N6Section() {
  const { data, error, reload } = useApi<N6Overview>("/api/n6");
  const [size, setSize] = useState<string>("1000");
  const [picker, setPicker] = useState<"app" | "owner">("app");
  if (!data) return <Loading error={error} rows={2} />;
  if (!data.enabled) return null;
  const pf = data.portfolios.find((p) => p.picker === picker && String(p.total_usd) === size);
  const sizes = Array.from(new Set([...data.portfolios.map((p) => String(p.total_usd)), ...data.owner_sizes.map(String)]));
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-2">
        <h2 className="label">仮想のお金で渡る（N6）</h2>
        <Pill tone="g" icon="check">本物のお金は動きません</Pill>
      </div>
      <p className="cap">{data.virtual_ja}{data.last_tick ? ` 最後の見回り ${jst(data.last_tick.ts)}${data.last_tick.ok ? "" : "（失敗）"}。15分ごと。` : " まだ見回っていません（パソコンの更新のあと15分以内に始まります）。"}</p>
      {data.last_tick && !data.last_tick.ok && <Note>見回りに失敗しました: {data.last_tick.error}</Note>}
      {data.health && (
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={data.health.ok ? "g" : "r"} icon={data.health.ok ? "check" : undefined}>
            {data.health.ok ? "異常なし" : `異常 ${data.health.problems.length} 件`}
          </Pill>
          <span className="cap">見回り {data.health.ticks} 回</span>
        </div>
      )}
      <Segmented options={sizes.map((s) => ({ key: s, label: sizeLabel(Number(s)) }))} value={size} onChange={setSize} />
      <Segmented options={[{ key: "app" as const, label: "アプリ任せ" }, { key: "owner" as const, label: "自分で選ぶ" }]} value={picker} onChange={setPicker} />
      {pf ? <PortfolioBody pf={pf} data={data} onChanged={reload} /> : (
        <Note>{picker === "owner"
          ? `自分で選ぶ ${sizeLabel(Number(size))} の練習は、まだ始まっていません。探す → 入れる先 →「自分で選ぶ練習に入れる」で始まります。`
          : "まだ始まっていません。"}</Note>
      )}
      <Folds>
        <Fold title="毎日のまとめ"><DailyBody /></Fold>
        <Fold title="$1,000 と $10,000 の上限の差"><CapDiffBody d={data.cap_diff ?? null} /></Fold>
        <Fold title="保険あり／なしの比べ（同じ額・同じ時刻・同じ幅）"><TwinsBody pf={pf} /></Fold>
        <Fold title="アプリ任せ／自分で選ぶの比べ"><AppVsOwn data={data} /></Fold>
        <Fold title="前の決まりと新しい決まりの比べ（影の計算）"><ShadowBody data={data} /></Fold>
        <Fold title="最近の出来事"><EventList events={data.events.filter((e) => !pf || e.portfolio_id === pf.id)} /></Fold>
        <Fold title="決まり（仮）"><RulesBody data={data} /></Fold>
        <Fold title="異常のチェック"><HealthBody h={data.health} /></Fold>
      </Folds>
    </section>
  );
}

function PortfolioBody({ pf, data, onChanged }: { pf: Portfolio; data: N6Overview; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const loss = pf.loss?.periods;
  const reqs = data.requests.filter((r) => r.portfolio_id === pf.id && r.status === "waiting");
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-2">
        <div className="inset p-4"><div className="cap">今の値打ち（仮想）</div><div className="t20 num">{usd(pf.equity_usd)}</div></div>
        <div className="inset p-4"><div className="cap">損益（始めてから）</div><div className="t20 num">{signedUsd(pf.pnl_usd)}</div><div className="cap">{pct(pf.pnl_pct)}</div></div>
        <div className="inset p-4"><div className="cap">いちばん大きく下がった幅</div><div className="bold num">{usd(pf.max_drawdown_usd)}</div></div>
        <div className="inset p-4"><div className="cap">置いている／置いていない</div><div className="bold num">{usd(pf.placed_usd, 0)} / {usd(pf.cash_usd, 0)}</div></div>
      </div>
      {pf.status === "stopped" && (
        <div className="flex flex-col gap-2">
          <Note>損の線「すべて止める」で止まっています（{pf.stopped_reason}）。</Note>
          <button type="button" className="btn btn-quiet self-start" disabled={busy} onClick={async () => {
            setBusy(true);
            try { await postApi(`/api/n6/portfolios/${pf.id}/resume`, { confirm: true }); onChanged(); } finally { setBusy(false); }
          }}>再開する</button>
        </div>
      )}
      <FirstEntryCard fe={data.first_entries?.find((f) => f.portfolio_id === pf.id)} />
      {pf.funnel ? <FunnelCard f={pf.funnel} /> : pf.waiting && <p className="cap">待っている理由: {pf.waiting}</p>}
      {reqs.length > 0 && <p className="cap">申し込み中 {reqs.length} 件（次の15分ごとの見回りで入ります）</p>}
      {loss && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(loss).map(([p, r]) => (
            <Pill key={p} tone={r.level ? LEVEL_TONE[r.level] : "n"}>{PERIOD_JA[p]} {pct(r.pct)}{r.level ? `・${data.level_ja[r.level]}` : ""}</Pill>
          ))}
        </div>
      )}
      <div className="flex flex-col gap-2">
        <span className="label">今の建玉（{pf.open.length}）</span>
        {pf.open.length === 0 && <p className="cap">ありません。</p>}
        {pf.open.map((p) => <PosRow key={p.id} p={p} />)}
      </div>
      <Folds>
        <Fold title={`閉じた建玉（${pf.closed.length}）`}>
          <div className="flex flex-col gap-2">{pf.closed.length === 0 ? <p className="cap">ありません。</p> : pf.closed.map((p) => <PosRow key={p.id} p={p} />)}</div>
        </Fold>
        <Fold title="どの決まりが働いたか">
          {pf.rules_fired.length === 0 ? <p className="cap">まだありません。</p> : (
            <div className="flex flex-col gap-2">
              {pf.rules_fired.map((r) => <Line key={`${r.action}-${r.rule}`} k={`${r.action_ja}: ${r.rule_ja ?? "—"}`} v={`${r.count} 回`} />)}
            </div>
          )}
        </Fold>
      </Folds>
    </div>
  );
}

function PosRow({ p }: { p: N6Pos }) {
  return (
    <Link to={`/practice/n6/${p.id}`} className="inset flex flex-col gap-1 p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="bold min-w-0" style={{ overflowWrap: "anywhere" }}>{p.name ?? p.pair}</span>
        <span className="num bold shrink-0">{signedUsd(p.pnl_usd)}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Pill>{usd(p.amount_usd, 0)}</Pill>
        <Pill tone={p.hedge ? "g" : "n"}>{p.hedge ? "保険あり" : "保険なし"}</Pill>
        {p.range_pct != null && <Pill>幅 ±{p.range_pct.toFixed(1)}%</Pill>}
        {p.status === "closed" && <Pill tone="y">{p.exit_rule_ja ?? "閉じた"}</Pill>}
      </div>
      <span className="cap">
        見込み {pct(p.est_apr_pct, 1)}／年{p.actual_apr_pct != null ? `・実際 ${pct(p.actual_apr_pct, 1)}／年` : "・実際は1時間たってから"}
        {p.in_range_pct != null ? `・幅の中 ${p.in_range_pct.toFixed(0)}%` : ""}・{p.venue}
      </span>
    </Link>
  );
}

function TwinsBody({ pf }: { pf?: Portfolio }) {
  if (!pf || pf.twins.length === 0) return <p className="cap">まだありません（保険の売り場がある入れる先だけ、対を作ります）。</p>;
  return (
    <div className="flex flex-col gap-4">
      {pf.twins.map((t) => (
        <div key={t.main_id} className="inset flex flex-col gap-2 p-4">
          <span className="bold">{t.name}（{usd(t.amount_usd, 0)}・{t.status === "open" ? "続いている" : "閉じた"}）</span>
          <div className="grid grid-cols-3 gap-2 cap">
            <span /> <span className="bold">保険あり</span> <span className="bold">保険なし</span>
            <span>損益</span><span className="num">{signedUsd(t.hedged.pnl_usd)}</span><span className="num">{signedUsd(t.unhedged.pnl_usd)}</span>
            <span>値動きの損</span><span className="num">{signedUsd(t.hedged.price_move_usd)}</span><span className="num">{signedUsd(t.unhedged.price_move_usd)}</span>
            <span>保険の損益</span><span className="num">{signedUsd(t.hedged.hedge_usd)}</span><span className="num">—</span>
            <span>資金調達料</span><span className="num">{usd(t.hedged.funding_usd)}</span><span className="num">—</span>
            <span>預け金</span><span className="num">{usd(t.hedged.margin?.equity)}</span><span className="num">—</span>
            <span>足したとしたら</span><span className="num">{t.hedged.margin ? `${t.hedged.margin.topups} 回・${usd(t.hedged.margin.topup_usd)}` : "—"}</span><span className="num">—</span>
            <span>大きく下がった幅</span><span className="num">{usd(t.hedged.max_drawdown_usd)}</span><span className="num">{usd(t.unhedged.max_drawdown_usd)}</span>
            <span>保険の費用</span><span className="num">{usd(t.hedged.hedge_cost_usd)}</span><span className="num">—</span>
          </div>
          {t.closed_at && <span className="cap">出た時刻 {jst(t.closed_at)}・出た理由 {t.exit_reason ?? "—"}（対も同じ時刻に閉じます）</span>}
        </div>
      ))}
      <Note>対（反対側）は比べるためだけの仮想の建玉で、練習の総額・上限・損の線には数えません。</Note>
    </div>
  );
}

function FirstEntryCard({ fe }: { fe?: FirstEntry }) {
  if (!fe) return null;
  if (!fe.entered) return <p className="cap">まだ1度も入っていません（{jst(fe.started_at)} から見回っています）。</p>;
  return (
    <div className="inset flex flex-col gap-2 p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="label">初めて入った</span>
        <Pill tone="g">{fe.size_ja}・{fe.picker_ja}</Pill>
      </div>
      <Link to={`/practice/n6/${fe.position_id}`} className="bold" style={{ overflowWrap: "anywhere" }}>{fe.name ?? fe.pair}</Link>
      <Line k="入った日時" v={jst(fe.opened_at ?? "")} />
      <span className="cap">練習を始めてから {(fe.waited_hours ?? 0).toFixed(1)} 時間後</span>
      <Line k="会場・チェーン" v={`${fe.venue ?? "—"}・${fe.chain_name ?? "—"}`} />
      <Line k="仮想で入れた額" v={usd(fe.amount_usd ?? null, 0)} />
      <Line k="始めたときの年利（控えめ）" v={plainPct(fe.est_apr_pct ?? null, 1)} />
      <Line k="危なさ・保険" v={`${fe.danger_label ?? "—"}・${fe.hedge ? "保険あり" : "保険なし"}`} />
      <span className="cap">選んだ理由: {fe.pick_reason ?? "—"}</span>
    </div>
  );
}

function MissLine({ m, target }: { m: Miss; target?: number }) {
  return (
    <span className="cap" style={{ overflowWrap: "anywhere" }}>
      {m.name}（{m.venue ?? "—"}・{m.chain_name ?? m.chain ?? "—"}）／ 控えめの年利 {plainPct(m.apr_pct, 1)} ／ 危なさ {m.danger_label ?? "—"}
      {m.gap_pt > 0 ? ` ／ 狙い${target != null ? ` ${target}%` : ""}まであと ${m.gap_pt.toFixed(1)}pt` : ""} ／ {m.reason}
    </span>
  );
}

function FunnelCard({ f }: { f: Funnel }) {
  if (f.error) return <Note>この回の内訳を記録できませんでした: {f.error}（入る・出るの判断は続いています）</Note>;
  const find = (c: string) => f.reach_dropped.find((x) => x.code === c)?.n ?? 0;
  return (
    <div className="inset flex flex-col gap-2 p-4">
      <span className="label">この回の候補（{f.entered > 0 ? `${f.entered} つ入った` : "入らなかった"}）</span>
      {f.reason && <span className="cap">入らなかった理由: {f.reason}</span>}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 cap">
        <span>候補の数</span><span className="num">{f.total}</span>
        <span>計算できて使える</span><span className="num">{f.usable}</span>
        <span>年{f.target}%以上</span><span className="num">{f.reach}</span>
        <span>　うち上限で落ちた</span><span className="num">{f.reach_cap_dropped}</span>
        <span>　うち危なさで落ちた</span><span className="num">{f.reach_danger_dropped}</span>
        <span>　うち5%上限</span><span className="num">{find("pool_5pct")}</span>
        <span>　うち会場の上限</span><span className="num">{find("venue_cap")}</span>
        <span>　うち保険の預け金</span><span className="num">{find("lighter")}</span>
        <span>最後に入れる候補</span><span className="num">{f.final}</span>
      </div>
      {f.best_miss && (
        <div className="flex flex-col gap-1">
          <span className="bold">いちばん惜しかった候補</span>
          <MissLine m={f.best_miss} target={f.target} />
          {f.best_miss.gap_pt > 0 && <span className="cap">狙いの年{f.target}% 未満の候補には入りません（待つのも練習のうち）。</span>}
        </div>
      )}
      {f.counts.length > 0 && (
        <Folds>
          <Fold title="すべての候補の内訳">
            <div className="flex flex-col gap-1">{f.counts.map((c) => <Line key={c.code} k={c.ja} v={`${c.n}`} />)}</div>
          </Fold>
        </Folds>
      )}
    </div>
  );
}

function CapDiffBody({ d }: { d: CapDiff | null }) {
  if (!d) return <p className="cap">$1,000 と $10,000 のアプリ任せの練習がそろうと比べます。</p>;
  if (d.ticks === 0) return <p className="cap">内訳の記録がまだありません（パソコンの更新のあとの見回りから記録します）。</p>;
  return (
    <div className="flex flex-col gap-2">
      <span className="cap">最近 {d.ticks} 回の見回りで、年30%以上の同じ候補の扱いが $1,000 と $10,000 で分かれた数（左が $1,000、右が $10,000）。</span>
      {d.tally.length === 0 ? <p className="cap">分かれたことはありません。</p> :
        d.tally.map((t, i) => <Line key={i} k={`${t.small_ja} ／ ${t.big_ja}`} v={`${t.n} 回`} />)}
      {d.latest.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="bold">最後の回（{jst(d.latest_ts ?? "")}）</span>
          {d.latest.map((x) => <span key={x.key} className="cap">{x.name}（{plainPct(x.apr_pct, 1)}）: $1,000 は {x.small_ja}、$10,000 は {x.big_ja}</span>)}
        </div>
      )}
    </div>
  );
}

function DailyBody() {
  const { data, error } = useApi<{ days: DailyRow[] }>("/api/n6/daily");
  if (!data) return <Loading error={error} rows={2} />;
  if (data.days.length === 0) return <p className="cap">まだ記録がありません。</p>;
  return (
    <div className="flex flex-col gap-4">
      {data.days.map((d) => (
        <div key={d.date} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="bold shrink-0" style={{ whiteSpace: "nowrap" }}>{d.date}</span>
            <span className="cap text-right">見回り {d.ticks} 回{d.ticks_failed ? `（失敗 ${d.ticks_failed}）` : ""}・最大の建玉 {d.max_positions}</span>
          </div>
          {d.ticks === 0 && <span className="cap">この日は N6 の見回りがありません（パソコンが止まっていたなど）。</span>}
          {d.ticks > 0 && d.portfolios.map((p) => (
            <span key={p.id} className="cap">
              {p.size_ja} {p.picker_ja}: 損益 {signedUsd(p.pnl_usd)}（{pct(p.pnl_pct, 2)}）・大きく下がった幅 {usd(p.max_drawdown_usd)}・
              待った {p.waiting_ticks} 回・入った {p.entered}・出た {p.exited}・移った {p.moved}
            </span>
          ))}
          {d.wait_reasons.length > 0 && <span className="cap">入れなかった主な理由: {d.wait_reasons.map((w) => `${w.text}（${w.n} 回）`).join("、")}</span>}
          {d.best_miss && <span className="cap">その日いちばん惜しかった候補: {d.best_miss.name}（{plainPct(d.best_miss.apr_pct, 1)}・あと {d.best_miss.gap_pt.toFixed(1)}pt）</span>}
          {d.exit_reasons.length > 0 && <span className="cap">出た主な理由: {d.exit_reasons.map((w) => `${w.text}（${w.n}）`).join("、")}</span>}
          {d.hedge_vs.pairs > 0 && <span className="cap">保険あり {signedUsd(d.hedge_vs.hedged_pnl_usd)} ／ なし {signedUsd(d.hedge_vs.unhedged_pnl_usd)}（{d.hedge_vs.pairs} 組）</span>}
          <span className="cap">置き直し 新 {d.new_vs_old.rebalances_new} 回 ／ 旧 {d.new_vs_old.rebalances_old} 回・1日の損の線の合図 新 {d.new_vs_old.loss_day_new} ／ 旧 {d.new_vs_old.loss_day_old}</span>
        </div>
      ))}
    </div>
  );
}

function HealthBody({ h }: { h?: Health }) {
  if (!h) return <p className="cap">パソコンの更新のあとに出ます。</p>;
  return (
    <div className="flex flex-col gap-2">
      <span className="cap">確かめた時刻 {jst(h.checked_at)}・最後の見回り {h.last_tick ? jst(h.last_tick) : "—"}</span>
      {h.ok ? <p className="cap">異常はありません（見回りの失敗・30分以上の空き・総額や5%上限の超え・重複・対の欠け・閉じ忘れ・$1,000 と $10,000 の混ざり・本物のお金のコード を確かめています）。</p>
        : h.problems.map((p, i) => <Note key={i}>{p}</Note>)}
      {h.notes.map((n, i) => <span key={i} className="cap">{n}</span>)}
    </div>
  );
}

function AppVsOwn({ data }: { data: N6Overview }) {
  if (data.app_vs_own.length === 0) return <p className="cap">自分で選ぶ練習を始めると、同じ期間・同じ総額で比べます。</p>;
  return (
    <div className="flex flex-col gap-2">
      {data.app_vs_own.map((r) => (
        <div key={r.size} className="inset flex flex-col gap-1 p-4">
          <span className="bold">{sizeLabel(r.size)}（{jst(r.since)} から）</span>
          <Line k="アプリ任せ" v={`${signedUsd(r.app_pnl_usd)}（${pct(r.app_pnl_pct)}）`} />
          <Line k="自分で選ぶ" v={`${signedUsd(r.own_pnl_usd)}（${pct(r.own_pnl_pct)}）`} />
        </div>
      ))}
    </div>
  );
}

function ShadowBody({ data }: { data: N6Overview }) {
  const s = data.shadow;
  if (s.positions === 0) return <p className="cap">幅に配るプールの建玉ができると、1日目から記録します。</p>;
  return (
    <div className="flex flex-col gap-2">
      <Line k="置き直しの回数（新／前）" v={`${s.rebalances.new} / ${s.rebalances.old} 回`}
        note={`見込み: 新 ${s.rebalances.est_new.toFixed(1)} 回（補正 ×${data.rules.new.rebalance_factor}）／前 ${s.rebalances.est_old.toFixed(1)} 回`} />
      <Line k="置き直しの費用（新／前）" v={`${usd(s.rebalance_cost.new)} / ${usd(s.rebalance_cost.old)}`} />
      <Line k="幅の中にいた時間（新／前）" v={`${s.in_range_pct.new?.toFixed(0) ?? "—"}% / ${s.in_range_pct.old?.toFixed(0) ?? "—"}%`} />
      <Line k="ボーナス（新の分母／A だけ）" v={`${usd(s.bonus.split)} / ${usd(s.bonus.a)}`} note="条件がそろったキャンペーンだけ B（幅の中の預け方だけ）" />
      <Line k="1日の損の線が働いた回数（新／前）" v={`${s.loss_day_events.new} / ${s.loss_day_events.old} 回`} />
      <Note>前の決まり: 境目を出て {data.rules.old.edge_wait_minutes} 分で置き直す・1日の線 {data.rules.old.loss_day.caution}/{data.rules.old.loss_day.no_new}/{data.rules.old.loss_day.stop}%。
        新しい決まり: 境目から幅の {data.rules.new.edge_buffer_pct}% 外に {data.rules.new.edge_wait_minutes} 分・1日の線 {data.rules.new.loss_day.caution}/{data.rules.new.loss_day.no_new}/{data.rules.new.loss_day.stop}%。</Note>
    </div>
  );
}

function RulesBody({ data }: { data: N6Overview }) {
  const r = data.rules;
  return (
    <div className="flex flex-col gap-2 cap">
      <span>・置き直し: 境目から幅の {r.new.edge_buffer_pct}% 外に {r.new.edge_wait_minutes} 分いたら（前は 0%・15分。影で両方計算）。</span>
      <span>・置き直しの見込み:（1日の値動き ÷ 幅）² × {r.new.rebalance_factor}（仮）。</span>
      <span>・損の線（%）: 今日 {r.new.loss_day.caution}/{r.new.loss_day.no_new}/{r.new.loss_day.stop}、7日 {r.loss_week.caution}/{r.loss_week.no_new}/{r.loss_week.stop}、始めてから {r.loss_since_start.caution}/{r.loss_since_start.no_new}/{r.loss_since_start.stop}（注意／新しく入らない／すべて止める）。</span>
      <span>・ボーナスは毎日 {r.bonus_sale_hour_jst} 時（日本時間）に売ったとして数えます（実際には売りません）。持ち続けたときとの差も残します。</span>
      <span>・プールに置く分はプールの預かり額の 5% まで。1つの会場と Lighter は総額の 50% まで（$2,000 未満は1か所でもよい）。</span>
    </div>
  );
}

function EventList({ events }: { events: N6Event[] }) {
  if (events.length === 0) return <p className="cap">まだありません。</p>;
  return (
    <div className="flex flex-col gap-2">
      {events.slice(0, 40).map((e) => (
        <div key={e.id} className="flex flex-col gap-1">
          <span className="cap">{jst(e.ts)}・{e.action_ja}{e.shadow ? "（影・比べるだけ）" : ""}{e.rule_ja && e.rule_ja !== e.action_ja ? `・${e.rule_ja}` : ""}</span>
          <span className="sec" style={{ overflowWrap: "anywhere" }}>{e.message_ja}</span>
        </div>
      ))}
    </div>
  );
}

/** 建玉の画面（/practice/n6/:id） */
export function N6PositionPage() {
  const { id = "" } = useParams();
  const { data, error, reload } = useApi<N6Detail>(`/api/n6/positions/${id}`);
  const back = { to: "/practice", label: "練習" };
  if (!data) return <><PageHead title="仮想の建玉" back={back} /><Loading error={error} /></>;
  const d = data;
  const open = d.status === "open";
  return (
    <>
      <PageHead title={d.name ?? d.pair ?? "仮想の建玉"} at={d.valued_at} back={back} />
      <div className="cap -mt-2">{d.twin_of ? "対（比べるための反対側）・" : ""}{d.hedge ? "保険あり" : "保険なし"}・{usd(d.amount_usd, 0)}・{d.venue}・始めた {jst(d.opened_at)}{d.closed_at ? `・閉じた ${jst(d.closed_at)}` : ""}</div>
      <div className="grid items-start gap-4 lg:grid-cols-2 lg:gap-6">
        <div className="flex flex-col gap-4 lg:gap-6">
          <Card title="損益（仮想）" right={<Pill tone="g">本物のお金は動きません</Pill>}>
            <div className="grid grid-cols-2 gap-2">
              <div className="inset p-4"><div className="cap">今の値打ち</div><div className="t20 num">{usd(d.value_usd)}</div></div>
              <div className="inset p-4"><div className="cap">損益</div><div className="t20 num">{signedUsd(d.pnl_usd)}</div><div className="cap">{pct(d.pnl_pct)}</div></div>
              <div className="inset p-4"><div className="cap">始めたときの見込み（年）</div><div className="bold num">{pct(d.est_apr_pct, 1)}</div><div className="cap">前の決まりなら {pct(d.est_old_apr_pct, 1)}</div></div>
              <div className="inset p-4"><div className="cap">実際（年に直すと）</div><div className="bold num">{d.actual_apr_pct == null ? "—" : pct(d.actual_apr_pct, 1)}</div><div className="cap">{d.days < 1 ? "1日たつまでは大きくぶれます" : `${d.days.toFixed(1)} 日`}</div></div>
            </div>
            {d.marks.length > 1 && <Spark values={d.marks.map((m) => m.pnl_usd)} height={64} />}
            {d.exit_reason && <Note>出た理由: {d.exit_reason}</Note>}
            {d.pick_reason && <p className="cap">選んだ理由: {d.pick_reason}</p>}
            {d.move_from && <Link className="cap underline" to={`/practice/n6/${d.move_from}`}>移ってきた元の建玉を見る</Link>}
            {d.move_to && <Link className="cap underline" to={`/practice/n6/${d.move_to}`}>移った先の建玉を見る</Link>}
          </Card>
          {open && !d.twin_of && <ExitForm id={d.id} reasons={d.reasons_ja} valuedAt={d.valued_at} onDone={reload} />}
        </div>
        <div className="flex flex-col gap-4 lg:gap-6">
          <Card title="内訳（始めてから）">
            <Line k="ボーナス（売ったとして）" v={signedUsd(d.bonus_usd)} />
            <Line k="手数料・元の利回り" v={signedUsd(d.fees_usd)} note="プールの手数料は記録がないので数えていません" />
            <Line k="値動きの損" v={signedUsd(d.price_move_usd)} />
            <Line k="保険の損益（売り）" v={signedUsd(d.hedge_usd)} />
            <Line k="資金調達料（払った分）" v={usd(d.funding_usd)} />
            <Line k="保険の手数料（合わせた分）" v={usd(d.hedge_cost_usd)} />
            <Line k="置き直しの費用" v={usd(d.rebalance_cost_usd)} note={`${d.rebalances ?? 0} 回`} />
            <Line k="入る・出る費用" v={usd(d.entry_exit_cost_usd)} />
            <Line k="いちばん大きく下がった幅" v={usd(d.max_drawdown_usd)} />
            <Line k="幅の中にいた時間" v={d.in_range_pct == null ? "—" : `${d.in_range_pct.toFixed(0)}%`} note={d.range_pct != null ? `幅 ±${d.range_pct.toFixed(1)}%` : undefined} />
            {d.margin && <Line k="保険の預け金（今／はじめ）" v={`${usd(d.margin.equity)} / ${usd(d.margin.start)}`}
              note={`維持に要る額 ${usd(d.margin.maintenance)}${d.margin.liquidated ? `・強制決済 ${jst(d.margin.liquidated)}` : ""}・足したとしたら ${d.margin.topups} 回 ${usd(d.margin.topup_usd)}`} />}
            {d.bonus_hold && <Line k="ボーナスを持ち続けたとしたら" v={signedUsd(d.bonus_hold.diff_usd)} note={`売った ${usd(d.bonus_hold.sold_net_usd)}（費用のあと）／持っていたら ${usd(d.bonus_hold.held_usd)}`} />}
            <p className="cap">値段の出どころ: {d.price_src ?? "—"}{d.state.gap_hours > 0 ? `・記録の切れ目 ${d.state.gap_hours.toFixed(1)} 時間（その間のボーナスは数えていません）` : ""}</p>
          </Card>
          <Folds>
            {d.twin && <Fold title="保険あり／なしの比べ"><TwinsBody pf={{ twins: [d.twin] } as unknown as Portfolio} /></Fold>}
            <Fold title={`ボーナスを売ったとしたら（${d.sales.length} 日）`}>
              {d.sales.length === 0 ? <p className="cap">まだありません。</p> : d.sales.map((s) => (
                <Line key={`${s.day}-${s.symbol}`} k={`${s.day} ${s.symbol}`} v={usd(s.usd)} note={`${s.units.toPrecision(4)} 枚 × ${usd(s.price, 4)}・費用 ${usd(s.cost_usd)}`} />
              ))}
            </Fold>
            {d.state.old && <Fold title="前の決まりなら（影）">
              <Line k="置き直し" v={`${d.state.old.count} 回`} note={`費用 ${usd(d.state.old.cost)}`} />
              <Line k="ボーナス（A の分母・前の幅）" v={usd(d.state.old.bonus_usd)} />
            </Fold>}
            <Fold title="出来事"><EventList events={d.events} /></Fold>
          </Folds>
        </div>
      </div>
    </>
  );
}

function ExitForm({ id, reasons, valuedAt, onDone }: { id: number; reasons: Record<string, string>; valuedAt: string | null; onDone: () => void }) {
  const [reason, setReason] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const nav = useNavigate();
  return (
    <Card title="出る（仮想）">
      <p className="cap">直近の15分ごとの計算（{jst(valuedAt)}）の値で閉じます。そのときの見込みと理由を残します。</p>
      <div className="flex flex-wrap gap-2">
        {Object.entries(reasons).map(([k, v]) => (
          <button key={k} type="button" className={`btn ${reason === k ? "" : "btn-quiet"}`} aria-pressed={reason === k} onClick={() => setReason(k)}>{v}</button>
        ))}
      </div>
      <textarea className="inset p-4" rows={2} maxLength={300} placeholder="メモ（なくてもよい）" value={note} onChange={(e) => setNote(e.target.value)} />
      {err && <Note>{err}</Note>}
      <button type="button" className="btn btn-danger self-start" disabled={!reason || busy} onClick={async () => {
        setBusy(true); setErr(null);
        try {
          await postApi(`/api/n6/positions/${id}/exit`, { reason, note });
          onDone();
          nav(`/practice/n6/${id}`, { replace: true });
        } catch (e) { setErr(String((e as Error).message)); } finally { setBusy(false); }
      }}><Icon name="x" size={16} />{busy ? "閉じています…" : "出る"}</button>
    </Card>
  );
}

/** 探すの入れる先の画面: 自分で選ぶ練習に入れる（申し込み。次の15分ごとの見回りで入る） */
export function N6OwnerEntry({ oppKey }: { oppKey: string }) {
  const [size, setSize] = useState<string>("1000");
  const [hedge, setHedge] = useState<"auto" | "yes" | "no">("auto");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <section className="card flex flex-col gap-2 p-6">
      <span className="label">自分で選ぶ練習（仮想のお金）</span>
      <Segmented options={[{ key: "1000", label: "$1,000 の練習" }, { key: "10000", label: "$10,000 の練習" }]} value={size} onChange={setSize} />
      <Segmented options={[{ key: "auto" as const, label: "保険は良い方" }, { key: "yes" as const, label: "保険あり" }, { key: "no" as const, label: "保険なし" }]} value={hedge} onChange={setHedge} />
      <button type="button" className="btn self-start" disabled={busy} onClick={async () => {
        setBusy(true); setMsg(null);
        try {
          const r = await postApi<{ message_ja: string }>("/api/n6/owner", { opp_key: oppKey, size: Number(size), hedge });
          setMsg(r.message_ja);
        } catch (e) { setMsg(String((e as Error).message)); } finally { setBusy(false); }
      }}><Icon name="flask" size={16} />自分で選ぶ練習に入れる</button>
      <span className="cap">{msg ?? "額は上限（危なさ・会場・プールの 5%）に収まるいちばん大きい段にします。次の15分ごとの見回りで入ります。"}</span>
    </section>
  );
}
