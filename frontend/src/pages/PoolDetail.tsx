import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { postApi, useApi, type Breakdown, type Paper, type PoolDetail as Detail, type RangeRow } from "../api";
import { AssetsLine, HourlyBars, RangeNet, Series, Waterfall } from "../charts";
import { bigUsd, jst, pct, rangeText, ratioPct, signedUsd, tone, usd } from "../format";
import { HedgeBadge } from "./Home";
import type { SwapCosts } from "../api";
import { Badge, Card, Loading, Note, SignalBadge, Term } from "../ui";

const ROWS: [keyof Breakdown, string, string][] = [
  ["income", "収入（報酬）", "LP"],
  ["direction", "方向（値動き）", "方向"],
  ["gamma", "ガンマ", "ガンマ損失"],
  ["hedge", "ヘッジ", "ヘッジ"],
  ["haircut", "報酬トークン値下がり", "報酬トークン値下がり"],
  ["other", "その他（置き直しの費用）", "リバランス"],
];

export default function PoolDetail() {
  const { id = "" } = useParams();
  const { data, error } = useApi<Detail>(`/api/pools/${encodeURIComponent(id)}`);
  if (!data) return <Loading error={error} />;
  const s = data.score;
  const d = data.daily;
  const b = d.breakdown;
  const inp = s.details.inputs ?? {};
  const ranges = s.details.ranges ?? [];
  const best = ranges.find((r) => r.r_pct === s.best_r);
  const stock = !!s.is_stock_pair;
  return (
    <div className="space-y-3">
      <div>
        <div className="flex items-center justify-between gap-2">
          <h1 className="truncate text-xl font-bold text-slate-50">{s.pair}</h1>
          <SignalBadge s={s.signal} />
        </div>
        <div className="text-xs text-slate-400">{s.venue_id} ・ 計算 {jst(s.ts)}{stock && " ・ 株ペア"}</div>
        <div className="mt-1"><Term k="保険（ヘッジ）"><HedgeBadge h={s.hedge_info} /></Term></div>
      </div>

      <Card title="なぜこの判定か">
        <p className="whitespace-pre-line text-sm leading-relaxed text-slate-200">{s.reason_ja}</p>
        {s.warnings && s.warnings.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {s.warnings.map((w, i) => (
              <Badge key={i} tone={w.level === "major" ? "rose" : "amber"}>{w.message_ja}</Badge>
            ))}
          </div>
        )}
      </Card>

      <TryCard poolId={s.pool_id} red={s.signal === "red"} />

      {b && (
        <>
          <Card title="1日の見込み（予測）" right={<Badge tone="sky"><Term k="推定">予測</Term></Badge>}>
            <div className="rounded-xl bg-slate-800/60 p-3 text-center">
              <div className="text-xs text-slate-400"><Term k="本業の稼ぎ">本業の稼ぎ（収入 − ガンマ）</Term></div>
              <div className={`num text-3xl font-bold ${tone(b.core)}`}>{signedUsd(b.core)}</div>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-center">
              <div className="rounded-xl bg-slate-800/40 p-2 ring-1 ring-sky-500/30">
                <div className="text-xs text-slate-400"><Term k="総資産あたり日利">総資産あたり</Term></div>
                <div className={`num text-xl font-bold ${tone(d.net_daily_pct)}`}>{pct(d.net_daily_pct)}</div>
                <div className="text-[10px] text-sky-300">判定に使う</div>
              </div>
              <div className="rounded-xl bg-slate-800/40 p-2">
                <div className="text-xs text-slate-400"><Term k="建玉あたり日利">建玉あたり</Term></div>
                <div className={`num text-xl font-bold ${tone(d.net_daily_pct_lp)}`}>{pct(d.net_daily_pct_lp)}</div>
                <div className="text-[10px] text-slate-500">表示用</div>
              </div>
            </div>
            {data.sell_now && <SellNow sn={data.sell_now} />}
            <Note>{d.judge_basis}。総資産 {usd(data.capital.total, 0)}（うち LP {usd(data.capital.lp, 0)}）で計算。</Note>

            <BreakdownTable b={b} />
            <Waterfall b={b} />

            <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
              <div className="rounded-lg bg-slate-800/40 p-2">
                <div className="text-xs text-slate-400"><Term k="表示APY と 純APY">表示APY</Term></div>
                <div className="num text-slate-100">{pct(d.apy_display, 0)}</div>
              </div>
              <div className="rounded-lg bg-slate-800/40 p-2">
                <div className="text-xs text-slate-400">純APY</div>
                <div className={`num ${tone(d.apy_net)}`}>{pct(d.apy_net, 0)}</div>
              </div>
            </div>
            <Note>APY は{d.apy_note}です。</Note>
            <Note>{d.realized_note}</Note>
          </Card>

          <DayCards today={data.today} since={data.since_start} />
        </>
      )}

      <Card title="1時間ごとの純損益（予測・48時間）">
        <HourlyBars bars={data.hourly.bars} />
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
          <span><span className="inline-block h-2 w-3 bg-sky-400/20 align-middle" /> <Term k="米国市場時間" /></span>
          <span><span className="inline-block h-0.5 w-3 bg-amber-400 align-middle" /> 24時間移動平均</span>
        </div>
        {data.hourly.best && data.hourly.worst && (
          <p className="mt-2 text-xs text-slate-300">
            いちばん稼いだ時間: <span className="num text-emerald-400">{data.hourly.best.jst} {signedUsd(data.hourly.best.net_usd)}</span><br />
            いちばん損した時間: <span className="num text-rose-400">{data.hourly.worst.jst} {signedUsd(data.hourly.worst.net_usd)}</span>
          </p>
        )}
        <Note>その時間のスコアの「1日の純損益 ÷ 24」を1時間分の見込みとしています。</Note>
      </Card>

      <Card title="総資産（このプールに48時間いたら）" right={<Badge tone="sky">推定</Badge>}>
        <div className="flex items-baseline justify-between">
          <span className="num text-2xl font-bold text-slate-50">{usd(data.assets.value)}</span>
          <span className={`num text-sm ${tone(data.assets.change_usd)}`}>
            {signedUsd(data.assets.change_usd)}（{pct(data.assets.change_pct)}）
          </span>
        </div>
        <AssetsLine series={data.assets.series} capital={data.assets.capital} />
        <dl className="mt-2 grid grid-cols-2 gap-y-1 text-sm">
          <dt className="text-slate-400"><Term k="建玉">建玉（LP）</Term></dt><dd className="num text-right">{usd(data.assets.parts.lp)}</dd>
          <dt className="text-slate-400">財布（予備）</dt><dd className="num text-right">{usd(data.assets.parts.wallet)}</dd>
          <dt className="text-slate-400">証拠金（ヘッジ用）</dt><dd className="num text-right">{usd(data.assets.parts.margin)}</dd>
          <dt className="text-slate-400">未回収報酬</dt><dd className="num text-right">{usd(data.assets.parts.unclaimed)}</dd>
        </dl>
        <Note>原資 {usd(data.assets.capital, 0)} から、この48時間の予測どおりに増減した場合の数字です。実際の建玉は「練習」（M5）で表示します。</Note>
      </Card>

      <Card title={<Term k="範囲（レンジ）">レンジ</Term>}>
        {s.range_prices && (
          <div className="mb-2 rounded-lg bg-amber-500/10 px-2 py-1 text-sm text-amber-100">
            最適レンジ ±{s.best_r}% ＝ <span className="num">{rangeText(s.range_prices)}</span>
          </div>
        )}
        <RangeBar price={data.price?.price ?? null} ranges={ranges} best={s.best_r} />
        <div className="mt-3 text-xs text-slate-400">レンジ幅ごとの純日利（総資産あたり。黄色が最適）</div>
        <RangeNet rows={ranges} best={s.best_r} />
      </Card>

      {data.swap && <SwapCard w={data.swap} />}

      <Card title="推移（7日）">
        <div className="space-y-3">
          <div><div className="text-xs text-slate-400">純日利（総資産あたり）</div>
            <Series history={data.history} field="net_daily_pct" fmt={(v) => `${v.toFixed(1)}%`} stock={stock} /></div>
          <div><div className="text-xs text-slate-400">プールが1日に出すボーナス</div>
            <Series history={data.history} field="reward_usd_day" fmt={(v) => bigUsd(v)} stock={stock} color="#34d399" /></div>
          <div><div className="text-xs text-slate-400"><Term k="レンジ内の流動性">レンジ内のステーク分</Term>（最適レンジ換算）</div>
            <Series history={data.history} field="staked_inrange_usd" fmt={(v) => bigUsd(v)} stock={stock} color="#a78bfa" /></div>
          <div><div className="text-xs text-slate-400">報酬トークンの価格</div>
            <Series history={data.history} field="reward_token_usd" fmt={(v) => `$${v.toPrecision(2)}`} stock={stock} color="#fbbf24" /></div>
        </div>
        {stock && <Note>青い背景は米国市場が開いている時間です。</Note>}
      </Card>

      <Card title="くわしい数字">
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1.5 text-sm">
          <Row k={<Term k="レンジ内の時間の割合">レンジ内の時間（判定用）</Term>} v={ratioPct(s.in_range_ratio)} note="置き直す前提" />
          <Row k="レンジ内の時間（参考）" v={ratioPct(s.in_range_ratio_hold)} note="置きっぱなしの場合" />
          <Row k={<Term k="値動き（σ）">ペアの値動き（1日）</Term>} v={ratioPct(s.sigma_pair, 1)} />
          <Row k="1日のリバランス回数" v={best ? best.rebalances_per_day.toFixed(1) : "—"} />
          <Row k={<Term k="TVL" />} v={bigUsd(s.tvl_usd)} />
          <Row k="24時間の取引量" v={bigUsd(inp.volume_24h_usd)} note={inp.volume_used_usd != null && inp.volume_used_usd !== inp.volume_24h_usd ? `計算は ${bigUsd(inp.volume_used_usd)} まで` : undefined} />
          <Row k="プールが1日に出すボーナス" v={bigUsd(inp.reward_usd_day)} />
          <Row k="レンジ内の流動性（最適レンジ換算）" v={bigUsd(best?.pool_inrange_usd)} note={best?.staked_inrange_usd != null ? `ステーク分 ${bigUsd(best.staked_inrange_usd)}` : undefined} />
          <Row k="自分の取り分（$550 を入れた場合）" v={ratioPct(best?.share_staked, 1)} />
          <Row k={<Term k="スリッページ">両替のずれ</Term>} v={inp.slippage == null ? "—" : ratioPct(inp.slippage, 2)} note={inp.slippage_source === "fallback" ? "初期値" : "流動性から計算"} />
          <Row k="手数料率" v={inp.fee == null ? "—" : ratioPct(inp.fee, 2)} />
          <Row k="ガス代（1回）" v={usd(inp.gas_usd_per_tx, 4)} />
          <Row k="ヘッジ先" v={s.has_perp ? "あり" : "なし"} />
        </dl>
        {s.vol_source === "external" && <Note>値動きは外部データ（GeckoTerminal）で補っています。</Note>}
        <p className="mt-2 break-all text-[10px] text-slate-500">{s.address}</p>
      </Card>
    </div>
  );
}

function TryCard({ poolId, red }: { poolId: string; red: boolean }) {
  const { data } = useApi<Paper>("/api/paper");
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!data) return null;
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await postApi<{ position_id: number }>("/api/paper/positions", { pool_id: poolId });
      nav(`/practice/${r.position_id}`);
    } catch (e) {
      setErr(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<Term k="練習">練習</Term>}>
      {red && <div className="mb-2"><Badge tone="rose">練習用・判定は🔴</Badge></div>}
      <button onClick={go} disabled={busy || !data.enabled || data.stopped}
        className="w-full rounded-xl bg-sky-600 py-3 font-bold text-white disabled:bg-slate-700 disabled:text-slate-400">
        {busy ? "作っています…" : `このプールで ${usd(data.capital, 0)} を試す`}
      </button>
      {!data.enabled && <Note>今は「見るだけ」モードです。{data.how_to_enable}</Note>}
      {data.stopped && <Note>練習は停止中です。</Note>}
      {err && <p className="mt-2 text-sm text-rose-300">{err}</p>}
      <Note>お金は動きません。本物の値動きと報酬のデータで「入れていたらどうなったか」を毎時記録します。</Note>
    </Card>
  );
}

function SellNow({ sn }: { sn: NonNullable<Detail["sell_now"]> }) {
  return (
    <div className="mt-2 rounded-xl border border-dashed border-slate-600 p-2">
      <div className="text-xs text-slate-400"><Term k="すぐ売る前提">参考: 報酬をすぐ売る前提</Term></div>
      <div className="mt-1 grid grid-cols-2 gap-2 text-center">
        <div>
          <div className="text-[10px] text-slate-400">持ち続ける前提（判定）</div>
          <div className={`num text-lg font-bold ${tone(sn.hold_net_daily_pct)}`}>{pct(sn.hold_net_daily_pct)}</div>
          <div className="text-[10px] text-slate-500">値下がり {signedUsd(sn.hold_haircut == null ? null : -sn.hold_haircut)}/日</div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">{sn.hours}時間で売る前提（参考）</div>
          <div className={`num text-lg font-bold ${tone(sn.net_daily_pct)}`}>{pct(sn.net_daily_pct)}</div>
          <div className="text-[10px] text-slate-500">値下がり {signedUsd(-sn.haircut)}/日 ・ ±{sn.best_r}%</div>
        </div>
      </div>
      <Note>{sn.note}</Note>
    </div>
  );
}

function Row({ k, v, note }: { k: React.ReactNode; v: string; note?: string }) {
  return (
    <>
      <dt className="min-w-0 text-slate-400">{k}{note && <span className="block text-[10px] text-slate-500">{note}</span>}</dt>
      <dd className="num text-right text-slate-100">{v}</dd>
    </>
  );
}

export function BreakdownTable({ b, compact = false }: { b: Breakdown; compact?: boolean }) {
  return (
    <div className={compact ? "" : "mt-3"}>
      <table className="w-full text-sm">
        <tbody>
          {ROWS.map(([k, label, term]) => (
            <tr key={k} className="border-b border-slate-800/80">
              <td className="py-1 text-slate-300"><Term k={term}>{label}</Term></td>
              <td className={`num py-1 text-right ${tone(b[k] as number)}`}>{signedUsd(b[k] as number)}</td>
            </tr>
          ))}
          <tr>
            <td className="py-1 font-semibold text-slate-100">純損益</td>
            <td className={`num py-1 text-right font-bold ${tone(b.net)}`}>{signedUsd(b.net)}</td>
          </tr>
        </tbody>
      </table>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
        <span><Term k="ヘッジのずれ" />: <span className={`num ${tone(b.hedge_gap)}`}>{signedUsd(b.hedge_gap)}</span></span>
        <span><Term k="運の要素" />: <span className="num">{ratioPct(b.luck_ratio)}</span></span>
      </div>
      {b.lucky && <p className="mt-1 text-xs text-amber-300">⚠ 運の要素が大きい日です（本業以外が50%超）。</p>}
    </div>
  );
}

function DayCards({ today, since }: { today: Breakdown | null; since: Breakdown | null }) {
  return (
    <div className="grid gap-3">
      <Card title="今日（日本時間 0時から）の平均" right={<Badge tone="sky">予測</Badge>}>
        {today ? <BreakdownTable b={today} compact /> : <p className="text-sm text-slate-400">今日の計算はまだありません。</p>}
      </Card>
      <Card title="戦略開始以降の1日平均" right={<Badge tone="sky">予測</Badge>}>
        {since ? (
          <>
            <BreakdownTable b={since} compact />
            <Note>{since.days}日分（{jst(since.since ?? null)} から）。判断には1日平均の方を重視してください（1日だけの数字は運に左右されやすい）。</Note>
          </>
        ) : <p className="text-sm text-slate-400">まだデータがありません。</p>}
      </Card>
    </div>
  );
}

function RangeBar({ price, ranges, best }: { price: number | null; ranges: RangeRow[]; best: number | null }) {
  if (!ranges.length) return <p className="text-sm text-slate-400">判定できなかったため、レンジはありません。</p>;
  const max = Math.max(...ranges.map((r) => r.r_pct));
  const x = (p: number) => 50 + (p / max) * 46;
  return (
    <div>
      <svg viewBox="0 0 100 34" className="w-full">
        {ranges.map((r, i) => {
          const y = 4 + i * (26 / ranges.length);
          const on = r.r_pct === best;
          return (
            <g key={r.r_pct}>
              <rect x={x(-r.r_pct)} y={y} width={x(r.r_pct) - x(-r.r_pct)} height={26 / ranges.length - 1}
                rx={1} fill={on ? "#fbbf24" : "#334155"} opacity={on ? 0.9 : 0.8} />
              {i === ranges.length - 1 && null}
            </g>
          );
        })}
        <line x1={50} x2={50} y1={1} y2={32} stroke="#38bdf8" strokeWidth={0.6} />
      </svg>
      <div className="flex justify-between text-[10px] text-slate-500">
        <span>−{max}%</span>
        <span className="text-sky-300">今の価格 {price == null ? "—" : price.toPrecision(5)}</span>
        <span>+{max}%</span>
      </div>
      <Note>青い線が今の価格、帯が各レンジ（上から ±{ranges[0].r_pct}% 〜 ±{max}%）。黄色が最適レンジ（±{best}%）。</Note>
    </div>
  );
}

/** 両替のずれと、それが費用にいくら含まれるか（2026-09-29 オーナー追加） */
function SwapCard({ w }: { w: SwapCosts }) {
  const row = (k: string, v: number, strong = false) => (
    <div className="flex justify-between"><span className="text-slate-400">{k}</span>
      <span className={`num ${strong ? "text-rose-300" : "text-slate-200"}`}>{usd(v)}</span></div>
  );
  return (
    <Card title={<Term k="スリッページ">両替のずれ</Term>}>
      <div className="mb-2 text-sm text-slate-200">
        ${w.trade_usd.toFixed(0)} を両替したときの値段のずれ <span className="num font-bold">{w.slippage_pct.toFixed(2)}%</span>
        <span className="ml-1 text-xs text-slate-500">（{w.source === "fallback" ? "初期値" : "プールの流動性から計算"}）</span>
      </div>
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="space-y-1 rounded-lg bg-slate-800/40 p-2">
          <div className="text-slate-300">始めた費用 {usd(w.open_total)}</div>
          {row("うち 両替のずれ", w.slippage, true)}
          {row("両替の手数料", w.swap_fee)}
          {row("ガス代（2回）", w.gas)}
          {row("ヘッジの手数料", w.hedge_fee)}
        </div>
        <div className="space-y-1 rounded-lg bg-slate-800/40 p-2">
          <div className="text-slate-300">置き直し1回 {usd(w.rebalance_total)}</div>
          {row("うち 両替のずれ", w.slippage, true)}
          {row("両替の手数料", w.swap_fee)}
          {row("ガス代（2回）", w.gas)}
        </div>
      </div>
      <Note>両替するのは LP に置く額の半分（{usd(w.swap_usd, 0)}）。ずれは ${w.trade_usd.toFixed(0)} で見積もっているので、少し多めです。置き直しのヘッジの手数料は、量を調整した分だけかかります。</Note>
    </Card>
  );
}
