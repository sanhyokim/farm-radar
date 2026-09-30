import { Link } from "react-router-dom";
import { useApi, type Home as HomeData } from "../api";
import { HourStrip } from "../charts";
import { jst, pct, signedUsd, usd } from "../format";
import { Icon } from "../icons";
import { Fold, Folds, Line, Loading, PageHead, Pill, Spark, Term, usePulse, useWide } from "../ui";
import { CountsInline, EvalProgress, PoolLine, TodoCard, fromSignalCounts } from "./parts";

// ホーム（SPEC 7.1章。2026-09-30 オーナー依頼 17〜21・32）
// スマホ: 今日やること → 練習 → 評価と判定の数 → 参考のプール → 1時間ごとのグラフ
// パソコン: 左に「今日やること」と練習、右に1時間ごとのグラフと評価の進み具合
export default function Home() {
  const wide = useWide();
  const { data, error } = useApi<HomeData>("/api/home");
  const pulse = usePulse();
  const mode = data?.mode ?? pulse?.mode;
  const modePill = mode ? <Pill icon="flask">{mode === "paper" ? "練習モード" : "観察モード"}</Pill> : null;
  if (!data) return <><PageHead title="ホーム" right={modePill} /><Loading error={error} /></>;
  const counts = fromSignalCounts(data.counts);
  const total = counts.good + counts.watch + counts.danger;

  const pools = <PoolsTile data={data} />;
  const practice = <PracticeTile data={data} wide={wide} />;
  const hourly = data.paper.positions.length > 0 && data.paper.hourly ? <HourlyTile data={data} height={wide ? 120 : 80} /> : null;
  const extra = <MarketFolds data={data} />;

  if (wide) {
    return (
      <>
        <PageHead title="ホーム" at={data.scored_at}
          right={<div className="flex items-center gap-6"><CountsInline c={counts} /><span className="cap">（{total}プール）</span>{modePill}</div>} />
        <div className="grid grid-cols-12 items-start gap-6">
          <div className="col-span-7 flex flex-col gap-6"><TodoCard items={data.todo} />{practice}{pools}</div>
          <div className="col-span-5 flex flex-col gap-6">
            {hourly}
            {data.evaluation && (
              <Link to="/practice" className="card flex flex-col gap-4 p-6">
                <div className="label flex items-center gap-2"><Icon name="flag" size={16} /><span>評価（2週間）の進み具合</span></div>
                <EvalProgress e={data.evaluation} ring={96} />
              </Link>
            )}
            {extra}
          </div>
        </div>
      </>
    );
  }
  return (
    <>
      <PageHead title="ホーム" right={modePill} at={data.scored_at} />
      <TodoCard items={data.todo} />
      {practice}
      <div className="grid grid-cols-2 gap-4">
        {data.evaluation ? (
          <Link to="/practice" className="card flex flex-col gap-4 px-4 py-6">
            <span className="label">評価の進み具合</span>
            <div className="flex items-center gap-4">
              <MiniRing day={data.evaluation.day} days={data.evaluation.days} />
              <div>
                <div className="bold num">{data.evaluation.coverage_pct == null ? "—" : `${data.evaluation.coverage_pct.toFixed(1)}%`}</div>
                <div className="cap">集まり具合</div>
              </div>
            </div>
          </Link>
        ) : (
          <Link to="/practice" className="card flex flex-col gap-4 px-4 py-6">
            <span className="label">評価</span><span className="cap">まだ始めていません</span>
          </Link>
        )}
        <Link to="/pools" className="card flex flex-col gap-4 px-4 py-6">
          <span className="label">判定（{total}プール）</span>
          <CountsInline c={counts} />
        </Link>
      </div>
      {pools}
      {hourly}
      {extra}
    </>
  );
}

function MiniRing({ day, days }: { day: number; days: number }) {
  const size = 64, stroke = 8, r = (size - stroke) / 2, c = 2 * Math.PI * r, m = size / 2;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={m} cy={m} r={r} fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth={stroke} />
        <circle cx={m} cy={m} r={r} fill="none" stroke="var(--text)" strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${(c * day / days).toFixed(1)} ${c.toFixed(1)}`} transform={`rotate(-90 ${m} ${m})`} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center"><span className="bold">{day}</span><span className="cap">/{days}日</span></div>
    </div>
  );
}

/** 練習のまとめ（評価額・今日の損益・状態。オーナー依頼 18） */
function PracticeTile({ data, wide }: { data: HomeData; wide: boolean }) {
  const p = data.paper;
  const n = p.positions.length;
  const warn = p.positions.find((x) => x.tone === "attention");
  return (
    <Link to="/practice" className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-4">
        <span className="label flex items-center gap-2"><Icon name="flask" size={16} /><span>{n > 0 ? `練習中の評価額（${n}つ）` : "練習"}</span></span>
        <span className="flex text-sec"><Icon name="right" /></span>
      </div>
      {n === 0 ? (
        <p className="sec">{p.text}{p.enabled && " プールの画面の「試す」で始められます。"}</p>
      ) : (
        <>
          <div className="flex flex-col gap-2">
            <span className="hero num">{usd(p.total.value)}</span>
            <div className="flex flex-wrap items-center gap-2">
              <Pill>今日 {signedUsd(p.total.today_usd)}</Pill>
              <span className="cap">始めた時 {usd(p.total.capital)} · 損益 {signedUsd(p.total.change_usd)}</span>
            </div>
          </div>
          {p.spark && p.spark.length > 1 && <Spark values={p.spark} height={wide ? 72 : 56} />}
          <div className="grid grid-cols-2 gap-2">
            {p.positions.map((x) => (
              <div key={x.id} className="inset flex flex-col p-4">
                <span className="cap truncate">{x.pair}</span>
                <span className="bold num">{usd(x.value)}</span>
                <span className="cap num">今日 {signedUsd(x.today_usd)}</span>
              </div>
            ))}
          </div>
          <div className="cap flex items-center gap-2" style={{ color: warn ? "var(--y)" : "var(--sec)" }}>
            <Icon name={warn || p.stopped ? "alert" : "check"} size={16} />
            <span>{p.stopped ? p.text : warn ? `${warn.pair}: ${warn.status}` : "見張り中 · 問題なし"}</span>
          </div>
        </>
      )}
    </Link>
  );
}

/** 参考のプール（🟢 があればそれ、なければ純日利が高い順に3つ）。様子見の数字は白のまま（オーナー依頼 20） */
function PoolsTile({ data }: { data: HomeData }) {
  const rows = data.greens.length > 0 ? data.greens : data.near;
  return (
    <section className="card flex flex-col gap-2 px-2 pt-6 pb-2">
      <div className="flex items-center justify-between gap-2 px-4 pb-2">
        <span className="label">{data.greens.length > 0 ? "良いプール" : "参考: 純日利が高い順"}</span>
        {data.greens.length === 0 && rows.length > 0 && <Pill tone={rows.every((r) => r.signal === "red") ? "r" : "y"}>{rows.every((r) => r.signal === "red") ? "どれも危険" : "良いはありません"}</Pill>}
      </div>
      {rows.length === 0 && <p className="cap px-4 pb-4">判定できたプールがまだありません。</p>}
      {rows.map((p) => <PoolLine key={p.pool_id} p={p} compact />)}
      <Link to="/pools" className="more" style={{ borderTop: "1px solid var(--line-soft)" }}><span>プールをすべて見る</span><Icon name="right" /></Link>
    </section>
  );
}

function HourlyTile({ data, height }: { data: HomeData; height: number }) {
  const bars = data.paper.hourly!.bars;
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-2">
        <span className="label">1時間ごとの損益</span>
        <span className="cap">練習中の{data.paper.positions.length}つ · 1本が1時間</span>
      </div>
      <HourStrip bars={bars} height={height} />
      <div className="cap flex justify-between"><span>{bars[0]?.jst}</span><span>{bars[bars.length - 1]?.jst}</span></div>
      <div className="cap flex items-center gap-2">
        <span className="h-1 w-4 rounded-full bg-white/30" /><span>下の帯: <Term k="米国市場時間">米国の市場が開いている時間</Term></span>
      </div>
    </section>
  );
}

/** 市場の状態とデータ集め（たたんでおく） */
function MarketFolds({ data }: { data: HomeData }) {
  const gaps = data.collection.flatMap((c) => c.gaps_7d);
  const stale = data.collection.some((c) => c.stale && !c.observe);
  return (
    <Folds>
      <Fold title="市場の状態">
        <Line k={<Term k="米国市場時間">米国市場</Term>} v={data.market.us_open ? "開いている" : "閉まっている"}
          note={data.market.us_day?.holiday ? `今日は休日（${data.market.us_day.holiday}）` :
            data.market.us_day?.early_close ? `今日は ${data.market.us_day.early_close} までの短縮取引（ニューヨーク時間）` : undefined} />
        <Line k="ガス代（1回）" v={usd(data.market.gas_usd_per_tx, 4)} />
        {data.market.reward_tokens.map((t) => (
          <Line key={t.venue_id} k={`${t.symbol} の24時間の変化`} v={t.change_24h == null ? "—" : pct(t.change_24h * 100, 1)} />
        ))}
      </Fold>
      <Fold title={<span className="flex items-center gap-2">データ集め{stale && <Pill tone="y">止まっています</Pill>}</span>}>
        <p className="sec">{stale ? "最新のデータが古くなっています。パソコンと Docker が動いているか確かめてください。" : `${data.snapshot_minutes}分ごとに集めています。`}</p>
        {data.collection.map((c) => (
          <Line key={c.venue_id} k={`${c.name ?? c.venue_id}${c.observe ? "（観察だけ）" : ""}`}
            v={c.last_ok_at ? jst(c.last_ok_at) : "まだ"} note={c.stale ? (c.observe ? "読み取りを休んでいます（評価の会場を優先）" : "止まっています") : "最後に集めた時刻"} />
        ))}
        {gaps.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="cap"><Term k="欠損">直近7日の欠損</Term></span>
            {gaps.map((g, i) => <span key={i} className="num cap">{jst(g.start_slot)} 〜 {jst(g.end_slot)}</span>)}
          </div>
        )}
      </Fold>
    </Folds>
  );
}
