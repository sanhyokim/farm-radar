import { Link } from "react-router-dom";
import { useApi, type Guard, type Home as HomeData, type OpportunitiesResp } from "../api";
import { HourStrip } from "../charts";
import { jst, pct, signedUsd, usd } from "../format";
import { Icon } from "../icons";
import { Fold, Folds, Line, Loading, PageHead, Pill, Spark, Term, usePulse, useWide } from "../ui";
import { EvalProgress, TodoCard } from "./parts";
import { FeedsCard } from "./Feeds";
import { VenueMatchPill, isUncertainVenue } from "./opp";

// ホーム（SPEC 7.1章・13.1 の追加の決定 2。N2c で形を変えた）
// スマホ: 全体の損益と損失ライン → 持っている建玉 → 知らせ → 探す（狙い以上の数）→ 一覧の保存 → 1時間ごとのグラフ
// パソコン: 左に損益・建玉・知らせ、右に探す・一覧の保存・グラフ
export default function Home() {
  const wide = useWide();
  const { data, error } = useApi<HomeData>("/api/home");
  const { data: guard } = useApi<Guard>("/api/guard");
  const pulse = usePulse();
  const mode = data?.mode ?? pulse?.mode;
  const modePill = mode ? <Pill icon="flask">{mode === "paper" ? "練習モード" : "観察モード"}</Pill> : null;
  if (!data) return <><PageHead title="ホーム" right={modePill} /><Loading error={error} /></>;

  const money = <MoneyTile g={guard} />;
  const practice = <PracticeTile data={data} wide={wide} />;
  const notices = <TodoCard items={data.todo} title="知らせ" />;
  const explore = <ExploreTile />;
  const hourly = data.paper.positions.length > 0 && data.paper.hourly ? <HourlyTile data={data} height={wide ? 120 : 80} /> : null;
  const evalTile = data.evaluation && data.evaluation.state === "running" ? (
    <Link to="/practice" className="card flex flex-col gap-4 p-6">
      <div className="label flex items-center gap-2"><Icon name="flag" size={16} /><span>評価（2週間）の進み具合</span></div>
      <EvalProgress e={data.evaluation} ring={96} />
    </Link>
  ) : null;
  const extra = <MarketFolds data={data} />;

  if (wide) {
    return (
      <>
        <PageHead title="ホーム" at={data.scored_at} right={modePill} />
        <div className="grid grid-cols-12 items-start gap-6">
          <div className="col-span-7 flex flex-col gap-6">{money}{practice}{notices}</div>
          <div className="col-span-5 flex flex-col gap-6">{explore}<FeedsCard />{hourly}{evalTile}{extra}</div>
        </div>
      </>
    );
  }
  return (
    <>
      <PageHead title="ホーム" right={modePill} at={data.scored_at} />
      {money}
      {practice}
      {notices}
      {explore}
      <FeedsCard />
      {hourly}
      {evalTile}
      {extra}
    </>
  );
}

/** 全体の損益と損失ライン（「守る」の数字の要約） */
function MoneyTile({ g }: { g: Guard | null }) {
  const l = g?.loss_line;
  const pill = !l ? null : l.state === "hit" ? <Pill tone="r" icon="alert">損失ラインに達した</Pill>
    : l.state === "near" ? <Pill tone="y" icon="alert">損失ラインに近い</Pill>
    : l.state === "ok" ? <Pill tone="g" icon="check">損失ラインまで余裕あり</Pill> : null;
  return (
    <Link to="/guard" className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-4">
        <span className="label flex items-center gap-2"><Icon name="shield" size={16} /><span>全体の損益</span></span>
        <span className="flex text-sec"><Icon name="right" /></span>
      </div>
      {!g ? <span className="cap">読み込み中…</span> : (
        <>
          <div className="flex items-end justify-between gap-4">
            <div className="flex flex-col">
              <span className="cap">持っている建玉の損益</span>
              <span className="hero num">{signedUsd(g.pnl.open_change_usd)}</span>
            </div>
            <div className="flex flex-col items-end">
              <span className="cap">今日</span>
              <span className="bold num">{signedUsd(g.pnl.today_usd)}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {pill}
            <span className="cap">
              置いている {usd(g.placed_usd, 0)}
              {l?.line_usd != null ? ` · 今日の損失ライン ${usd(l.line_usd)}（${l.pct}%）` : ` · 損失ラインは今日の損が置いている額の ${l?.pct ?? 5}%`}
            </span>
          </div>
        </>
      )}
    </Link>
  );
}

/** 探す（狙い利回り以上の入れる先の数。$1,000・控えめ） */
function ExploreTile() {
  const { data } = useApi<OpportunitiesResp>("/api/opportunities?amount=1000&limit=3", 5 * 60_000);
  return (
    <section className="card flex flex-col gap-2 px-2 pt-6 pb-2">
      <div className="flex items-center justify-between gap-2 px-4 pb-2">
        <span className="label flex items-center gap-2"><Icon name="search" size={16} />探す</span>
        {data && <span className="cap">おすすめ {data.counts.recommended ?? 0}件（狙い 年{+data.target_apr_pct.toFixed(2)}% 以上 {data.counts.above_target}件）</span>}
      </div>
      {!data ? <p className="cap px-4 pb-4">読み込み中…</p> : data.items.length === 0 ? <p className="cap px-4 pb-4">一覧に出せる入れる先はまだありません。</p>
        : data.items.map((o) => (
          <Link key={o.key} to={`/explore/${encodeURIComponent(o.key)}`} className="flex items-center gap-4 rounded-2xl px-4 py-2 hover:bg-white/[0.02]">
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate">{o.name ?? o.key}</span>
              <span className="cap truncate">{[o.chain_name, o.venue_name ?? o.venue].filter(Boolean).join(" · ")}</span>
              {isUncertainVenue(o) && <span className="flex"><VenueMatchPill /></span>}
            </span>
            <span className="bold num shrink-0">{o.best ? `${o.best.apr_pct.toFixed(1)}%` : "—"}</span>
          </Link>
        ))}
      <Link to="/explore" className="more" style={{ borderTop: "1px solid var(--line-soft)" }}><span>入れる先をすべて見る</span><Icon name="right" /></Link>
    </section>
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
        <p className="sec">{data.chain_reads === false ? "この版はチェーンを読みません（一覧の保存だけ）。チェーンのデータは今までの版（ポート 18000）で集めています。"
          : stale ? "最新のデータが古くなっています。パソコンと Docker が動いているか確かめてください。" : `${data.snapshot_minutes}分ごとに集めています。`}</p>
        {data.collection.map((c) => (
          <Line key={c.venue_id} k={`${c.name ?? c.venue_id}${c.observe ? "（観察だけ）" : ""}`}
            v={c.last_ok_at ? jst(c.last_ok_at) : "まだ"} note={c.off ? "この版では読みません" : c.stale ? (c.observe ? "読み取りを休んでいます（評価の会場を優先）" : "止まっています") : "最後に集めた時刻"} />
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
