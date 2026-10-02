import { Link } from "react-router-dom";
import { useApi, type Guard as GuardData, type GuardVenue } from "../api";
import { signedUsd, usd } from "../format";
import { Icon } from "../icons";
import { Card, Line, Loading, Note, PageHead, Pill, Term, useWide } from "../ui";
import { SafetyPill } from "./opp";

/**
 * 守る（N2c。SPEC 13.1 の追加の決定 2）: 置いている額と上限（会場ごと・チェーンごとに、上限まであといくら）、損失ライン。
 * 今ある数字で作り、N4 で中身（危なさの点数・保険の預け金の上限・強制決済に近いときの知らせ）を足す。
 */
export default function Guard() {
  const wide = useWide();
  const { data, error } = useApi<GuardData>("/api/guard");
  const head = <PageHead title="守る" right={<Pill icon="info">練習の数字</Pill>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const loss = <LossLine data={data} />;
  const total = <TotalCard data={data} />;
  const venues = <VenuesCard data={data} />;
  const chains = <ChainsCard data={data} />;
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
        <div className="col-span-7 flex flex-col gap-6">{loss}{venues}</div>
        <div className="col-span-5 flex flex-col gap-6">{total}{chains}{notes}</div>
      </div>
    </>
  ) : <>{head}{loss}{total}{venues}{chains}{notes}</>;
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

function LossLine({ data }: { data: GuardData }) {
  const l = data.loss_line;
  const pill = l.state === "hit" ? <Pill tone="r" icon="alert">ラインに達した</Pill>
    : l.state === "near" ? <Pill tone="y" icon="alert">ラインに近い</Pill>
    : l.state === "ok" ? <Pill tone="g" icon="check">大丈夫</Pill> : <Pill>建玉なし</Pill>;
  return (
    <Card title={<span className="flex items-center gap-2"><Icon name="shield" size={16} /><Term k="損失ライン">損失ライン</Term>（今日）</span>} right={pill}>
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col">
          <span className="cap">今日の損益（日本時間の0時から）</span>
          <span className="t32 num">{signedUsd(l.today_usd)}</span>
        </div>
        <div className="flex flex-col items-end">
          <span className="cap">ライン</span>
          <span className="num">{l.line_usd == null ? "—" : usd(l.line_usd)}</span>
        </div>
      </div>
      <Bar frac={l.used_frac} warnAt={l.near_frac} />
      <Note>
        今日の損が、置いている額の {l.pct}% に達したら、練習の建玉を全部閉じます（今の版の緊急離脱と同じ決まり）。
        ラインの {(l.near_frac * 100).toFixed(0)}% まで来たら「近い」と出します。
      </Note>
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
