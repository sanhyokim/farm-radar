import { useApi, type Venue } from "../api";
import { bigUsd, pct } from "../format";
import { Sparkline } from "../charts";
import { Badge, Card, Loading, Note, Term } from "../ui";

const LAMP = { ok: "bg-emerald-400", warn: "bg-amber-300", bad: "bg-rose-500", unknown: "bg-slate-600" } as const;
const COND = {
  C1: "ボーナスが在庫に比べて多い",
  C2: "報酬トークンの価格が崩れていない",
  C3: "値動きが小さい、またはヘッジできる",
  C4: "安全で低コスト",
} as Record<string, string>;

export default function Venues() {
  const { data, error } = useApi<{ venues: Venue[] }>("/api/venues");
  if (!data) return <Loading error={error} />;
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">会場</h1>
      {data.venues.map((v) => <VenueCard key={v.venue_id} v={v} />)}
    </div>
  );
}

function VenueCard({ v }: { v: Venue }) {
  const rt = v.reward_token;
  const tvlNow = v.tvl.length ? v.tvl[v.tvl.length - 1].v : null;
  return (
    <Card title={<span className="text-base text-slate-50">{v.name}</span>} right={<span className="text-xs text-slate-400">{v.venue_id}</span>}>
      <div className="mb-3 flex flex-wrap gap-1.5">
        {v.audited === true ? <Badge tone="emerald">監査あり</Badge> : v.audited === false ? <Badge tone="rose">監査なし</Badge> : <Badge tone="amber">監査: 未確認</Badge>}
        {v.age_days != null && <Badge>稼働 {v.age_days}日</Badge>}
        {v.unverified_contracts.length > 0 && <Badge tone="rose">未確認のコントラクト {v.unverified_contracts.length}件</Badge>}
      </div>

      <div className="mb-3">
        <div className="mb-1 text-xs font-semibold text-slate-300">4つの条件（会場全体の目安）</div>
        <ul className="space-y-1.5">
          {v.conditions.map((c) => (
            <li key={c.code} className="flex gap-2 text-sm">
              <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${LAMP[c.state]}`} />
              <span className="min-w-0">
                <span className="text-slate-200">{c.code} {COND[c.code]}</span>
                <span className="block text-xs text-slate-400">{c.text}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="min-w-0">
          <div className="text-xs text-slate-400">{rt.symbol ?? "報酬トークン"} の価格（7日）</div>
          <div className="num text-sm text-slate-100">
            {rt.price_usd == null ? "—" : `$${rt.price_usd.toPrecision(3)}`}{" "}
            <span className={rt.change_7d != null && rt.change_7d < 0 ? "text-rose-400" : "text-emerald-400"}>
              {rt.change_7d == null ? "" : pct(rt.change_7d * 100, 0)}
            </span>
          </div>
          <Sparkline data={rt.sparkline} color="#fbbf24" />
        </div>
        <div className="min-w-0">
          <div className="text-xs text-slate-400"><Term k="TVL">TVL</Term> の合計（7日）</div>
          <div className="num text-sm text-slate-100">{bigUsd(tvlNow)}</div>
          <Sparkline data={v.tvl} />
        </div>
      </div>
      {v.warnings.length > 0 && (
        <div className="mt-3 space-y-1">
          {v.warnings.map((w, i) => (
            <p key={i} className={`text-xs ${w.level === "major" ? "text-rose-300" : "text-amber-300"}`}>
              ⚠ {w.code}（{w.level === "major" ? "重大" : "軽微"}）: {w.title_ja ?? w.key}
            </p>
          ))}
        </div>
      )}
      <Note>TVL の合計は、判定をしたプールの TVL（GeckoTerminal）を足したものです。</Note>
    </Card>
  );
}
