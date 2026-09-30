import { useApi, type SkippedVenue, type Venue } from "../api";
import { bigUsd, pct } from "../format";
import { Icon } from "../icons";
import { Fold, Folds, Loading, Note, PageHead, Pill, Spark, Term } from "../ui";
import { DiscoverySection } from "./Discovery";

const LAMP = { ok: "dot-g", warn: "dot-y", bad: "dot-r", unknown: "dot-n" } as const;
const COND = {
  C1: "ボーナスが在庫に比べて多い",
  C2: "報酬トークンの価格が崩れていない",
  C3: "値動きが小さい、またはヘッジできる",
  C4: "安全で低コスト",
} as Record<string, string>;

export default function Venues() {
  const { data, error } = useApi<{ venues: Venue[]; skipped?: SkippedVenue[] }>("/api/venues", 300_000);
  const head = <PageHead title="会場" right={<span className="cap">{data ? `${data.venues.length}つ` : ""}</span>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  return (
    <>
      {head}
      <div className="grid items-start gap-4 lg:grid-cols-2 lg:gap-6">
        {data.venues.map((v) => <VenueCard key={v.venue_id} v={v} />)}
      </div>
      <SkippedCard rows={data.skipped ?? []} />
      <DiscoverySection />
    </>
  );
}

function VenueCard({ v }: { v: Venue }) {
  const rt = v.reward_token;
  const tvlNow = v.tvl.length ? v.tvl[v.tvl.length - 1].v : null;
  const vals = (pts: { v: number | null }[]) => pts.map((p) => p.v).filter((x): x is number => x != null);
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-start justify-between gap-2">
        <div><h2 className="t20">{v.name}</h2><div className="cap">{v.venue_id}</div></div>
        {v.practice === false && <Pill icon="eye">観察だけ</Pill>}
      </div>
      <div className="flex flex-wrap gap-2">
        {v.audited === true ? <Pill>監査あり</Pill> : v.audited === false ? <Pill tone="r">監査なし</Pill> : <Pill tone="y">監査: 未確認</Pill>}
        {v.age_days != null && <Pill>稼働 {v.age_days}日</Pill>}
        {v.unverified_contracts.length > 0 && <Pill tone="r">未確認のコントラクト {v.unverified_contracts.length}件</Pill>}
      </div>
      <div className="flex flex-col gap-2">
        <div className="label">4つの条件（会場全体の目安）</div>
        {v.conditions.map((c) => (
          <div key={c.code} className="inset flex items-start gap-4 p-4">
            <span className={`dot ${LAMP[c.state]} mt-2`} />
            <span className="min-w-0"><span>{c.code} {COND[c.code]}</span><span className="cap block">{c.text}</span></span>
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="flex min-w-0 flex-col gap-2">
          <div className="cap">{rt.symbol ?? "報酬トークン"} の価格（7日）</div>
          <div className="bold num">{rt.price_usd == null ? "—" : `$${rt.price_usd.toPrecision(3)}`} <span className="cap">{rt.change_7d == null ? "" : pct(rt.change_7d * 100, 0)}</span></div>
          <Spark values={vals(rt.sparkline)} height={40} />
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <div className="cap"><Term k="TVL">TVL</Term> の合計（7日）</div>
          <div className="bold num">{bigUsd(tvlNow)}</div>
          <Spark values={vals(v.tvl)} height={40} />
        </div>
      </div>
      {v.warnings.length > 0 && (
        <div className="flex flex-col gap-2">
          {v.warnings.map((w, i) => (
            <div key={i} className="cap flex items-start gap-2 text-sec">
              <Icon name="alert" size={16} color={w.level === "major" ? "var(--r)" : "var(--y)"} />
              <span>{w.code}（{w.level === "major" ? "危険" : "軽い警告"}）: {w.title_ja ?? w.key}</span>
            </div>
          ))}
        </div>
      )}
      <Note>TVL の合計は、判定をしたプールの TVL（GeckoTerminal）を足したものです。</Note>
    </section>
  );
}

/** 見送り中の会場: 調べたが、今は実装していない会場と理由（docs/plans.yaml。2026-09-30 オーナー追加） */
function SkippedCard({ rows }: { rows: SkippedVenue[] }) {
  if (rows.length === 0) return null;
  return (
    <Folds>
      <Fold title={`見送り中の会場（${rows.length}つ）`}>
        <p className="cap">調べたけれど、今はアプリに入れていない会場です。</p>
        {rows.map((v) => (
          <div key={v.key} className="inset flex flex-col gap-1 p-4">
            <div className="flex items-baseline justify-between gap-2"><span className="bold">{v.name}</span>{v.chain && <span className="cap">{v.chain}</span>}</div>
            <p className="sec">{v.reason}</p>
            {v.decided && <p className="cap">{v.decided}</p>}
            {v.source && <p className="cap break-all">出典: {v.source}</p>}
          </div>
        ))}
      </Fold>
    </Folds>
  );
}
