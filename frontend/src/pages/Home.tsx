import { useState } from "react";
import { Link } from "react-router-dom";
import { useApi, type EpochFlip, type HedgeInfo, type Home as HomeData, type PoolRow } from "../api";
import { SIGNAL, jst, jstDay, pct, rangeText, untilText, usd } from "../format";
import { Badge, Card, Loading, Note, SignalBadge, Term } from "../ui";

export default function Home() {
  const { data, error } = useApi<HomeData>("/api/home");
  if (!data) return <Loading error={error} />;
  const mode = data.mode === "paper" ? "ペーパー" : "観察";
  // 練習と評価に使う会場（up.）だけで「古い」を判断する。観察だけの会場（Alandale）は休むことがあるので別に書く（M6）
  const stale = data.collection.some((c) => c.stale && !c.observe);
  const resting = data.collection.filter((c) => c.stale && c.observe);
  const gaps = data.collection.flatMap((c) => c.gaps_7d);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-slate-100">Farm Radar</h1>
        <Badge tone="sky">
          <Term k="観察モード">モード: {mode}</Term>
        </Badge>
      </div>

      <Card title="今日の結論">
        <p className="text-lg font-semibold leading-snug text-slate-50">{data.summary}</p>
        <Note>最終計算 {jst(data.scored_at)}（1時間ごと）。判定は<Term k="総資産あたり日利" />で行います。</Note>
      </Card>

      <div className="grid grid-cols-3 gap-2">
        {(["green", "yellow", "red"] as const).map((s) => (
          <Link key={s} to={`/pools?signal=${s}`} className="rounded-2xl bg-slate-900 p-3 text-center ring-1 ring-slate-800">
            <div className="text-2xl">{SIGNAL[s].emoji}</div>
            <div className="num text-2xl font-bold text-slate-50">{data.counts[s] ?? 0}</div>
            <div className="text-xs text-slate-400">{SIGNAL[s].ja}</div>
          </Link>
        ))}
      </div>

      {data.greens.length > 0 ? (
        <Card title="🟢 攻め候補">
          <div className="space-y-2">{data.greens.map((p) => <PoolCard key={p.pool_id} p={p} />)}</div>
        </Card>
      ) : (
        <Card title="参考: 純日利が高い順（🟢ではありません）">
          <div className="space-y-2">{data.near.map((p) => <PoolCard key={p.pool_id} p={p} />)}</div>
          {data.near.length === 0 && <p className="text-sm text-slate-400">判定できたプールがまだありません。</p>}
        </Card>
      )}

      <Card title="市場の状態">
        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-slate-400"><Term k="米国市場時間">米国市場</Term></dt>
          <dd className="text-right">
            {data.market.us_open ? <Badge tone="emerald">開いている</Badge> : <Badge>閉まっている</Badge>}
            {data.market.us_day?.holiday && <div className="text-[11px] text-amber-300">今日は休日（{data.market.us_day.holiday}）</div>}
            {data.market.us_day?.early_close && <div className="text-[11px] text-amber-300">今日は {data.market.us_day.early_close} までの短縮取引（ニューヨーク時間）</div>}
            {data.market.us_day && !data.market.us_day.calendar_covered && <div className="text-[11px] text-slate-500">休日の表が未確認の年です</div>}
          </dd>
          <dt className="text-slate-400">ガス代（1回）</dt>
          <dd className="num text-right text-slate-100">{usd(data.market.gas_usd_per_tx, 4)}</dd>
          {data.market.reward_tokens.map((t) => (
            <FragmentRow key={t.venue_id} label={`${t.symbol} の24時間の変化`} value={t.change_24h == null ? "—" : pct(t.change_24h * 100, 1)}
              cls={t.change_24h == null ? "" : t.change_24h < 0 ? "text-rose-400" : "text-emerald-400"} />
          ))}
        </dl>
      </Card>

      <Card title="データ集め">
        {stale ? (
          <p className="text-sm text-amber-300">⚠ 最新のデータが古くなっています。パソコンと Docker が動いているか確かめてください。</p>
        ) : (
          <p className="text-sm text-emerald-300">✓ 15分ごとに集めています。</p>
        )}
        {resting.map((c) => (
          <p key={c.venue_id} className="mt-1 text-xs text-slate-400">
            {c.name}（観察だけ）は、up. の収集を優先して読み取りを休んでいます。最後に集めた時刻 {jst(c.last_ok_at)}
          </p>
        ))}
        {gaps.length > 0 && (
          <div className="mt-2 text-xs text-slate-400">
            <Term k="欠損">直近7日の欠損</Term>:
            <ul className="mt-1 space-y-0.5">
              {gaps.map((g, i) => <li key={i} className="num">{jst(g.start_slot)} 〜 {jst(g.end_slot)}</li>)}
            </ul>
          </div>
        )}
      </Card>
    </div>
  );
}

function FragmentRow({ label, value, cls }: { label: string; value: string; cls: string }) {
  return (
    <>
      <dt className="text-slate-400">{label}</dt>
      <dd className={`num text-right ${cls}`}>{value}</dd>
    </>
  );
}

export function PoolCard({ p }: { p: PoolRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl bg-slate-800/60 p-3">
      <div className="flex items-start justify-between gap-2">
        <Link to={`/pools/${encodeURIComponent(p.pool_id)}`} className="min-w-0">
          <div className="truncate font-semibold text-slate-50">{p.pair}</div>
          <div className="text-xs text-slate-400">{p.venue_name ?? p.venue_id}{p.best_r != null && ` ・ 最適レンジ ±${p.best_r}%`}</div>
          {p.range_prices && <div className="num text-xs text-slate-300">{rangeText(p.range_prices)}</div>}
        </Link>
        <div className="shrink-0 text-right">
          <SignalBadge s={p.signal} />
          <div className="num text-lg font-bold text-slate-50">{pct(p.net_daily_pct)}</div>
        </div>
      </div>
      <div className="mt-1 flex items-center gap-2 text-xs">
        <HedgeBadge h={p.hedge_info} />
        {p.range_prices && <Term k="範囲（レンジ）"><span className="text-slate-500">範囲とは</span></Term>}
      </div>
      {p.epoch_flip && <FlipLine f={p.epoch_flip} />}
      <button className="mt-1 text-xs text-sky-300" onClick={() => setOpen(!open)}>
        {open ? "閉じる" : "なぜ?"}
      </button>
      {open && <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-slate-300">{p.reason_ja}</p>}
    </div>
  );
}

/** 「⏰ 10/1(木) 9:00 に切り替え（あと1日9時間）。来週ボーナスがなくなることも」（2026-09-30 オーナー追加） */
export function FlipLine({ f }: { f: EpochFlip }) {
  return (
    <div className="mt-1 text-xs text-amber-200/90">
      ⏰ {jstDay(f.at)} に<Term k="エポック">切り替え</Term>（{untilText(f.at)}）。来週はボーナスがなくなることもあります
    </div>
  );
}

/** 「保険あり（Lighter）」/「保険なし」の印（2026-09-29 オーナー追加） */
export function HedgeBadge({ h }: { h?: HedgeInfo }) {
  if (!h) return null;
  return (
    <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs ${h.has ? "bg-emerald-500/15 text-emerald-300" : "bg-slate-700 text-slate-300"}`}>
      {h.has ? `🛡 ${h.label}` : h.label}
    </span>
  );
}
