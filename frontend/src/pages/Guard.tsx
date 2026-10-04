import { Link } from "react-router-dom";
import { useApi, type Guard as GuardData, type GuardVenue, type LossBreakdown, type LossPeriod, type Stage1Watch } from "../api";
import { jst, signedUsd, usd } from "../format";
import { Icon } from "../icons";
import { Card, Line, Loading, Note, PageHead, Pill, Term, useWide } from "../ui";
import { SafetyPill } from "./opp";

/**
 * 守る（N2c。SPEC 13.1 の追加の決定 2）: 置いている額と上限（会場ごと・Lighter・チェーンごとに、上限まであといくら）。
 * N4b: 損の線（3つの期間 × 3段階と内訳）、早く出る4段階の決まりと最近の合図、保険の強制決済までの余裕。
 */
export default function Guard() {
  const wide = useWide();
  const { data, error } = useApi<GuardData>("/api/guard");
  const head = <PageHead title="守る" right={<Pill icon="info">練習の数字</Pill>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const loss = <LossLinesCard data={data} />;
  const total = <TotalCard data={data} />;
  const venues = <VenuesCard data={data} />;
  const chains = <ChainsCard data={data} />;
  const stages = <StagesCard data={data} />;
  const hedges = <HedgesCard data={data} />;
  const notes = (
    <Card title="この画面について">
      {data.notes.map((n) => <Note key={n}>{n}</Note>)}
      <Note>今の数字は練習の建玉（お金は動いていない）です。</Note>
    </Card>
  );
  return wide ? (
    <>
      {head}
      <div className="grid grid-cols-12 items-start gap-6">
        <div className="col-span-7 flex flex-col gap-6">{loss}{stages}{venues}</div>
        <div className="col-span-5 flex flex-col gap-6">{total}{hedges}{chains}{notes}</div>
      </div>
    </>
  ) : <>{head}{loss}{total}{stages}{hedges}{venues}{chains}{notes}</>;
}

/** 棒（どれだけ使ったか）。色は印だけ: 上限に近いと黄、超えたら赤 */
function Bar({ frac, warnAt = 0.8 }: { frac: number | null; warnAt?: number }) {
  const f = Math.max(0, Math.min(1, frac ?? 0));
  const color = frac == null ? "var(--n)" : frac >= 1 ? "var(--r)" : frac >= warnAt ? "var(--y)" : "var(--sec)";
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-white/10" aria-hidden="true">
      <div className="h-full rounded-full" style={{ width: `${(f * 100).toFixed(1)}%`, background: color }} />
    </div>
  );
}

const LEVEL_TONE = { caution: "y", no_new: "y", stop: "r" } as const;
const PART_JA: Record<keyof LossBreakdown, string> = {
  pool: "プールの値動き", bonus: "ボーナスのコイン", hedge: "保険", costs: "費用", income: "収入", net: "合計",
};

/** 損の線（N4b。決定 10）: 期間ごとに、今の損益と3つの線（注意・新しく入らない・すべて止める）、内訳 */
function LossLinesCard({ data }: { data: GuardData }) {
  const ll = data.loss_lines;
  const pill = ll.level ? <Pill tone={LEVEL_TONE[ll.level]} icon="alert">{ll.level_ja}</Pill>
    : data.positions ? <Pill tone="g" icon="check">線の内側</Pill> : <Pill>建玉なし</Pill>;
  return (
    <Card title={<span className="flex items-center gap-2"><Icon name="shield" size={16} /><Term k="損失ライン">損の線</Term><Pill>仮</Pill></span>} right={pill}>
      {ll.periods.map((p) => <LossRow key={p.period} p={p} />)}
      <Note>{ll.note}</Note>
      <Note>
        注意は記録と知らせだけ、「新しく入らない」を越えると新しい練習を始めません、「すべて止める」を越えると練習の建玉を全部閉じて、
        新しく始めるのも止めます。「再開」を押すと、どの期間もそこから数え直します。
      </Note>
    </Card>
  );
}

function LossRow({ p }: { p: LossPeriod }) {
  const stop = p.lines.stop.pct;
  const frac = p.pct == null || p.pct >= 0 ? 0 : p.pct / stop;
  const parts = p.breakdown;
  return (
    <div className="flex flex-col gap-2 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
      <div className="flex items-end justify-between gap-4">
        <span className="flex flex-col">
          <span className="bold">{p.label}</span>
          <span className="cap">{p.since ? `${jst(p.since)} から` : "まだ練習がありません"}</span>
        </span>
        <span className="flex flex-col items-end">
          <span className="bold num">{signedUsd(p.net_usd)}</span>
          <span className="cap num">{p.pct == null ? "—" : `${p.pct >= 0 ? "+" : ""}${p.pct.toFixed(2)}%`}{p.base_usd ? ` / 置いている ${usd(p.base_usd, 0)}` : ""}</span>
        </span>
      </div>
      <Bar frac={frac} warnAt={p.lines.caution.pct / stop} />
      <div className="cap num">
        {(["caution", "no_new", "stop"] as const).map((k) => `${p.lines[k].label} ${p.lines[k].pct}%${p.lines[k].usd != null ? `（${usd(p.lines[k].usd, 0)}）` : ""}`).join(" · ")}
      </div>
      {p.level && <Pill tone={LEVEL_TONE[p.level]} icon="alert">{p.level_ja}を越えています</Pill>}
      {parts && p.net_usd !== 0 && (
        <div className="cap num">
          内訳: {(["pool", "bonus", "hedge", "costs", "income"] as const).filter((k) => Math.abs(parts[k]) >= 0.005)
            .map((k) => `${PART_JA[k]} ${signedUsd(parts[k])}`).join(" · ")}
          {p.main_cause && p.net_usd < 0 ? `（いちばん大きいのは${PART_JA[p.main_cause]}）` : ""}
        </div>
      )}
    </div>
  );
}

/** 早く出る4段階（N4b。決定 9: 練習では4つとも自動）と最近の合図 */
function StagesCard({ data }: { data: GuardData }) {
  return (
    <Card title={<span className="flex items-center gap-2"><Icon name="alert" size={16} />早く出る4段階<Pill>仮</Pill></span>}>
      {data.stages.map((s) => (
        <div key={s.stage} className="flex flex-col gap-1 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
          <div className="flex items-center justify-between gap-2">
            <span className="bold">{s.label}</span>
            <span className="flex gap-2">
              {s.auto ? <Pill tone="g">練習は自動で出る</Pill> : <Pill tone="y">今は知らせだけ</Pill>}
              {s.auto && s.rules.some((r) => r.includes("自動では出ません")) && <Pill tone="y">一部は知らせだけ</Pill>}
              {!s.waits_for_gas && <Pill>ガス代が高くても出る</Pill>}
            </span>
          </div>
          {s.rules.map((r) => <span key={r} className="cap">・{r}</span>)}
        </div>
      ))}
      <div className="label pt-2">最近の合図</div>
      {data.signals.length === 0 ? <Note>まだありません。</Note> : data.signals.map((e) => (
        <div key={e.id} className="flex flex-col gap-1 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
          <span className="cap">{jst(e.ts)} · {e.stage_ja ?? e.level_ja}{e.rule_ja ? `・${e.rule_ja}` : ""} · {e.action_ja}</span>
          <span className="sec">{e.message}</span>
        </div>
      ))}
      <div className="label pt-2">段階1の合図のあと（出ていたら／残っていたら）</div>
      {data.stage1_watch.length === 0 ? <Note>まだありません。プールのお金が1時間で大きく減ったときに記録します。</Note>
        : data.stage1_watch.map((w) => <Stage1WatchRow key={w.id} w={w} />)}
      <Note>
        「出ていたら」は合図のときに出たら手もとに残った額（閉じる費用の見込みを引いた額）、「残っていたら」は 1・6・24 時間後に
        出たときの同じ額です。途中で閉じたら、閉じたときの額です。あとで、自動で出るようにするかを数字で決めるための記録です。
      </Note>
    </Card>
  );
}

function Stage1WatchRow({ w }: { w: Stage1Watch }) {
  const cell = (h: 1 | 6 | 24) => {
    const v = w[`stay_${h}h_usd`];
    const d = w[`diff_${h}h_usd`];
    return `${h}時間後 ${v == null ? "まだ" : usd(v)}${d == null ? "" : `（${signedUsd(d)}）`}`;
  };
  return (
    <div className="flex flex-col gap-1 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
      <span className="cap">{jst(w.ts)} · {w.pair ?? w.pool_id} · プールのお金 −{(w.drop_pct ?? 0).toFixed(0)}%/1時間</span>
      <span className="num">出ていたら {w.exit_value_usd == null ? "—" : usd(w.exit_value_usd)}</span>
      <span className="cap num">残っていたら: {cell(1)} · {cell(6)} · {cell(24)}</span>
      {w.closed_at && <span className="cap">{jst(w.closed_at)} に閉じました</span>}
    </div>
  );
}

/** 保険の強制決済までの余裕（N4b） */
function HedgesCard({ data }: { data: GuardData }) {
  return (
    <Card title={<span className="flex items-center gap-2"><Icon name="shield" size={16} />保険（Lighter）の余裕</span>}>
      {data.hedges.length === 0 ? <Note>保険のある練習の建玉はありません。</Note> : data.hedges.map((h) => {
        const st = h.status;
        const pill = !st ? <Pill>保険は0（閉じている）</Pill> : st.state === "liquidated" ? <Pill tone="r" icon="alert">強制決済の線</Pill>
          : st.state === "alert" ? <Pill tone="y" icon="alert">余裕が少ない</Pill> : <Pill tone="g" icon="check">余裕あり</Pill>;
        return (
          <Link key={h.position_id} to={`/practice/${h.position_id}`} className="flex flex-col gap-2 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
            <div className="flex items-center justify-between gap-2"><span className="bold">{h.pair}</span>{pill}</div>
            {st && <Bar frac={st.buffer_frac == null ? null : 1 - st.buffer_frac} warnAt={1 - st.alert_frac} />}
            <span className="cap num">
              預けたお金 {usd(h.margin_usd, 0)}{st ? ` · 余裕 ${usd(st.buffer_usd)}（はじめの ${st.buffer_frac == null ? "—" : (st.buffer_frac * 100).toFixed(0)}%）· あと約 ${st.to_liquidation_pct.toFixed(0)}% 上がると強制決済` : ""}
            </span>
          </Link>
        );
      })}
      <Note>余裕（担保 − 維持に要る額）が、はじめの半分を切ったら知らせます（仮）。練習の詳しい画面で「値段が○%上がったら」を試せます。</Note>
      <div className="label pt-2">預け金の減り方（1時間ごとの記録）</div>
      {(data.margin_log ?? []).length === 0 ? <Note>まだありません。保険のある練習の建玉で、1時間に1回記録します。</Note>
        : data.margin_log.map((m) => (
          <div key={m.position_id} className="flex flex-col gap-1 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
            <span className="cap">{m.pair ?? `建玉 ${m.position_id}`}{m.status === "closed" ? "（閉じた）" : ""} · {jst(m.since)} から {m.points} 回</span>
            <span className="num">
              預けたお金 {usd(m.margin_usd)} → 今 {m.equity_usd == null ? "—" : usd(m.equity_usd)}
              {m.change_pct == null ? "" : `（${m.change_pct >= 0 ? "+" : ""}${m.change_pct.toFixed(1)}%）`}
            </span>
            <span className="cap num">
              いちばん低いとき {m.low_equity_usd == null ? "—" : usd(m.low_equity_usd)}（{jst(m.low_at)}）· 置き直し {m.rebalances ?? 0} 回
              {m.per_rebalance_usd == null ? "" : ` · 1回あたり ${signedUsd(m.per_rebalance_usd)}`}
            </span>
          </div>
        ))}
      <Note>置き直すと、保険の損は Lighter に、プールの得はチェーンに分かれてたまります。そのため長くいると、Lighter の預け金だけが減っていくことがあります。</Note>
      <div className="label pt-2">預け金を足したとしたら（記録だけ）</div>
      {(data.topups ?? []).length === 0
        ? <Note>まだありません。余裕がはじめの{((data.topup_line_frac ?? 0.5) * 100).toFixed(0)}%を切ったら、はじめの額まで足したとして記録します。</Note>
        : (data.topups ?? []).map((t) => (
          <div key={t.position_id} className="flex flex-col gap-1 py-2" style={{ borderTop: "1px solid var(--line-soft)" }}>
            <span className="cap">{t.pair ?? `建玉 ${t.position_id}`}{t.status === "closed" ? "（閉じた）" : ""} · 最後 {jst(t.at)}</span>
            <span className="num">{t.count} 回 · 合計 {usd(t.total_usd)} · 見込みの費用 {t.cost_usd == null ? "—" : usd(t.cost_usd)}</span>
            <span className="cap num">
              最後の回: プール {t.last.pool_usd == null ? "—" : usd(t.last.pool_usd, 0)} から {usd(t.last.topup_usd)} を出す ·
              売り {t.last.short_before_usd == null ? "—" : usd(t.last.short_before_usd, 0)} → {t.last.short_after_usd == null ? "—" : usd(t.last.short_after_usd, 0)}
            </span>
          </div>
        ))}
      <Note>決まり（仮）: 余裕がはじめの半分を切ったら、はじめの額まで足します。足すお金はプールから出すので、プールの値動きするコインも減ります。そのぶん、保険の売りも減らします。本物のお金を始めるまでは実際には足さず、回数・額・そのときのプールの額・費用の見込みを記録します。</Note>
    </Card>
  );
}

function TotalCard({ data }: { data: GuardData }) {
  const cap = data.limits.total_usd;
  return (
    <Card title="全体">
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col">
          <span className="cap">置いている額</span>
          <span className="t32 num">{usd(data.placed_usd, 0)}</span>
        </div>
        <div className="flex flex-col items-end">
          <span className="cap">上限まであと</span>
          <span className="bold num">{usd(data.total_left_usd, 0)}</span>
        </div>
      </div>
      <Bar frac={cap ? data.placed_usd / cap : null} />
      <Line k="合計の上限" v={usd(cap, 0)} />
      <Line k="1つの建玉の上限" v={usd(data.limits.position_usd, 0)} />
      <Line k="1つの会場の上限" v={usd(data.limits.venue_cap_usd, 0)}
        note={data.limits.per_venue_share != null ? `合計の ${(data.limits.per_venue_share * 100).toFixed(0)}%` : undefined} />
      <Line k="持っている建玉の損益" v={signedUsd(data.pnl.open_change_usd)} note={`${data.positions}つ`} />
    </Card>
  );
}

function VenuesCard({ data }: { data: GuardData }) {
  return (
    <section className="card flex flex-col overflow-hidden">
      <div className="label flex items-center gap-2 px-6 pt-6 pb-2"><Icon name="venue" size={16} />会場ごと</div>
      {data.venues.map((v) => <VenueRow key={v.venue_id} v={v} />)}
      <Link to="/venues" className="more" style={{ borderTop: "1px solid var(--line-soft)" }}><span>会場の詳しい確認（契約・4つの条件）</span><Icon name="right" /></Link>
    </section>
  );
}

function VenueRow({ v }: { v: GuardVenue }) {
  return (
    <div className="flex flex-col gap-2 px-6 py-4" style={{ borderTop: "1px solid var(--line-soft)" }}>
      <div className="flex items-start justify-between gap-4">
        <span className="flex min-w-0 flex-col">
          <span className="bold truncate">{v.name}</span>
          <span className="cap">{v.chain_name ?? v.chain ?? "—"}{v.practice ? "" : " · 観察だけ（練習に使わない）"}{v.positions > 0 ? ` · 建玉 ${v.positions}つ` : ""}</span>
        </span>
        <SafetyPill s={v.safety} short />
      </div>
      <Bar frac={v.used_frac} />
      <div className="flex justify-between gap-4">
        <span className="cap num">置いている {usd(v.placed_usd, 0)} / 上限 {usd(v.cap_usd, 0)}</span>
        <span className="cap num">あと {usd(v.left_usd, 0)}</span>
      </div>
    </div>
  );
}

function ChainsCard({ data }: { data: GuardData }) {
  return (
    <Card title="チェーンごと">
      {data.chains.map((c) => (
        <Line key={c.chain} k={c.name} v={usd(c.placed_usd, 0)}
          note={`会場 ${c.venues}つ · チェーンの上限はまだ無し（全体の上限まであと ${usd(c.left_usd, 0)}）`} />
      ))}
    </Card>
  );
}
