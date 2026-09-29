import { Link } from "react-router-dom";
import { useApi, type Paper, type PaperCard } from "../api";
import { jst, pct, signedUsd, tone, usd } from "../format";
import { Badge, Card, Loading, Note, Term } from "../ui";

export default function Practice() {
  const { data, error } = useApi<Paper>("/api/paper");
  if (!data) return <Loading error={error} />;
  const lim = data.limits;
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100"><Term k="練習">練習</Term>（ペーパートレード）</h1>

      <Card title="状態" right={data.enabled ? <Badge tone="emerald">練習モード</Badge> : <Badge tone="amber">見るだけモード</Badge>}>
        {data.enabled ? (
          <p className="text-sm leading-relaxed text-slate-300">
            お金は動きません。プール詳細の「このプールで {usd(data.capital, 0)} を試す」で<Term k="建玉" />を作ると、
            本物の値動きと報酬のデータで損益を毎時記録します。
          </p>
        ) : (
          <p className="text-sm leading-relaxed text-slate-300">練習するには、{data.how_to_enable}</p>
        )}
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-400">
          <div>1つの建玉の上限 <span className="num text-slate-200">{usd(lim.position_usd ?? null, 0)}</span></div>
          <div>1つの会場の上限 <span className="num text-slate-200">{usd(data.venue_cap_usd, 0)}</span></div>
          <div>いま使っている額 <span className="num text-slate-200">{usd(data.open_total_usd, 0)}</span></div>
          <div>1日の取引の上限 <span className="num text-slate-200">{lim.trades_per_day ?? "—"}件</span></div>
        </div>
        <Note>上限は config.yaml の limits で決まっています（変えられるのはオーナーだけ）。</Note>
      </Card>

      {data.open.length === 0 && (
        <Card><p className="text-sm text-slate-400">練習中の建玉はありません。プール → 好きなプール →「試す」で始められます。</p></Card>
      )}
      {data.open.map((c) => <PositionCard key={c.id} c={c} />)}

      {data.closed.length > 0 && (
        <Card title="閉じた練習">
          <div className="space-y-2">
            {data.closed.map((c) => (
              <Link key={c.id} to={`/practice/${c.id}`} className="flex items-center justify-between rounded-lg bg-slate-800/40 p-2 text-sm">
                <span className="text-slate-200">{c.pair} <span className="text-xs text-slate-500">{jst(c.opened_at)}〜{jst(c.closed_at)}</span></span>
                <span className={`num ${tone(c.change_usd)}`}>{signedUsd(c.change_usd)}</span>
              </Link>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

export function PositionCard({ c, link = true }: { c: PaperCard; link?: boolean }) {
  const body = (
    <Card
      title={<span>{c.pair} <span className="text-xs font-normal text-slate-400">±{c.r_pct}%</span></span>}
      right={c.in_range ? <Badge tone="emerald">レンジ内</Badge> : <Badge tone="rose">レンジ外</Badge>}
    >
      {c.red_label && <div className="mb-2"><Badge tone="rose">{c.red_label}</Badge></div>}
      <MiniRange c={c} />
      <div className="mt-3 grid grid-cols-2 gap-2 text-center">
        <div className="rounded-xl bg-slate-800/60 p-2">
          <div className="text-xs text-slate-400">評価額</div>
          <div className="num text-xl font-bold text-slate-50">{usd(c.value)}</div>
          <div className={`num text-xs ${tone(c.change_usd)}`}>{signedUsd(c.change_usd)}（{pct(c.change_pct)}）</div>
        </div>
        <div className="rounded-xl bg-slate-800/60 p-2">
          <div className="text-xs text-slate-400">1日の報酬（24時間）</div>
          <div className={`num text-xl font-bold ${tone(c.reward_24h_usd)}`}>{signedUsd(c.reward_24h_usd)}</div>
          <div className="text-xs text-slate-500">入れた額 {usd(c.capital, 0)}</div>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-center text-sm">
        <div><div className="text-[10px] text-slate-400">予測の純日利</div><div className={`num ${tone(c.predicted_daily_pct)}`}>{pct(c.predicted_daily_pct)}</div></div>
        <div><div className="text-[10px] text-slate-400">実績の純日利</div><div className={`num ${tone(c.actual_daily_pct)}`}>{pct(c.actual_daily_pct)}</div></div>
      </div>
      <Note>{jst(c.opened_at)} に開始。最後の計算 {jst(c.last_ts)}。{c.days < 1 && "まだ1日たっていないので、実績の日利は大きくぶれます。"}</Note>
    </Card>
  );
  return link ? <Link to={`/practice/${c.id}`} className="block">{body}</Link> : body;
}

function MiniRange({ c }: { c: PaperCard }) {
  const lo = c.lower, hi = c.upper;
  const span = hi - lo;
  const pad = span * 0.25;
  const min = lo - pad, max = hi + pad;
  const x = (p: number) => Math.min(100, Math.max(0, ((p - min) / (max - min)) * 100));
  return (
    <div>
      <div className="relative h-4 rounded bg-slate-800">
        <div className="absolute inset-y-0 rounded bg-amber-400/40" style={{ left: `${x(lo)}%`, width: `${x(hi) - x(lo)}%` }} />
        <div className="absolute inset-y-[-3px] w-0.5 bg-sky-400" style={{ left: `${x(c.price)}%` }} />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-slate-400">
        <span>下の端まで {edge(c.to_lower_pct)}</span>
        <span>上の端まで {edge(c.to_upper_pct)}</span>
      </div>
    </div>
  );
}

const edge = (v: number) => (v >= 0 ? `${v.toFixed(2)}%` : "外に出ています");
