import { useState } from "react";
import { Link } from "react-router-dom";
import { postApi, useApi, type Paper, type PaperCard, type RiskEvent, type Watch } from "../api";
import { hoursJa, jst, jstDay, pct, signedUsd, untilText, usd } from "../format";
import { Icon } from "../icons";
import { Fold, Folds, Line, Loading, Note, PageHead, Pill, Spark, Term, usePulse, useWide } from "../ui";
import { CalendarBody, CsvBody, EvaluationCard, HedgeVenuesBody, OutlookBody, TimelineList } from "./PaperExtras";

export { hoursJa };

// 練習（SPEC 7.4章。2026-09-30 オーナー依頼 25〜28）
// 結論 → 建玉のカード（評価額・損益・もらった報酬を大きく）→ 評価の進み具合 → 停止・全部閉じる（控えめ）→ たたんだ説明
export default function Practice() {
  const wide = useWide();
  const { data, error, reload } = useApi<Paper>("/api/paper");
  const head = <PageHead title="練習" right={<Pill icon="info">{wide ? "練習モード · お金は動きません" : "お金は動きません"}</Pill>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const cards = data.open.map((c) => <PositionCard key={c.id} c={c} onChanged={reload} />);
  const compare = data.enabled ? <CompareCard data={data} /> : null;
  const rest = <RestFolds data={data} />;
  const controls = data.enabled ? <Controls data={data} reload={reload} /> : null;
  return (
    <>
      {head}
      <Conclusion data={data} />
      {data.open.length === 0 && (
        <section className="card p-6"><p className="sec">練習中の建玉はありません。{data.evaluation_block ? "" : "探す → 入れる先 →「$1,000 を試す」で始められます（今は自分で読む会場のプールだけ）。"}</p></section>
      )}
      {wide ? (
        <>
          {cards.length > 0 && <div className="grid grid-cols-2 items-start gap-6">{cards}</div>}
          <div className="grid grid-cols-12 items-start gap-6">
            <div className="col-span-7 flex flex-col gap-6">{compare}{data.enabled && <EvaluationCard />}</div>
            <div className="col-span-5 flex flex-col gap-6">{rest}{controls}</div>
          </div>
        </>
      ) : (
        <>
          {cards}
          {compare}
          {controls}
          {data.enabled && <EvaluationCard />}
          {rest}
        </>
      )}
    </>
  );
}

/** 結論（1行）と、木曜の切り替え（この画面ではここに1回だけ） */
function Conclusion({ data }: { data: Paper }) {
  const n = data.open.length;
  const warn = data.open.filter((c) => c.cautions.length > 0 || !c.in_range);
  const text = !data.enabled ? "練習は使っていません" :
    data.stopped ? "停止中です（新しい建玉は作りません）" :
    n === 0 ? "練習中の建玉はありません" :
    warn.length > 0 ? `${n}つのうち${warn.length}つに注意があります` :
    n === 1 ? "見張り中で、問題はありません" : `${n}つとも見張り中で、問題はありません`;
  const flip = usePulse()?.next_flip;
  return (
    <div className="flex flex-col gap-2">
      <p className="t20">{text}</p>
      {!data.enabled && <p className="sec">練習するには、{data.how_to_enable}</p>}
      {data.stopped && data.stopped_reason && <p className="cap">止めた理由: {data.stopped_reason}（{jst(data.stopped_since)}）</p>}
      {n > 0 && flip && (
        <div className="cap flex items-start gap-2 text-sec">
          <Icon name="clock" size={16} color="var(--y)" />
          <span>{jstDay(flip)} にボーナスが<Term k="エポック">切り替わります</Term>（{untilText(flip)}）。{n === 1 ? "練習中の建玉にも関係します" : `${n}つとも関係します`}</span>
        </div>
      )}
      {data.evaluation_block && (
        <div className="cap flex items-start gap-2 text-sec"><Icon name="flag" size={16} /><span>{data.evaluation_block.message}</span></div>
      )}
    </div>
  );
}

/** 建玉のカード（オーナー依頼 25・27・28） */
export function PositionCard({ c, link = true, onChanged }: { c: PaperCard; link?: boolean; onChanged?: () => void }) {
  const warn = c.cautions[0] ?? (!c.in_range ? "レンジの外" : null);
  const open = c.status === "open";
  return (
    <section className="card flex flex-col overflow-hidden">
      <div className="flex flex-col gap-4 p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="bold">{c.pair}</div>
            <div className="cap">{c.venue_id === "up-robinhood" ? "up." : c.venue_id} · {usd(c.capital, 0)} · {jst(c.opened_at)} に開始 · ±{c.r_pct}%</div>
          </div>
          {open ? (
            warn ? <Pill tone="y" icon="alert">注意: {warn}</Pill> : <Pill icon="check">見張り: 問題なし</Pill>
          ) : <Pill>閉じた{c.close_reason_ja ? `（${c.close_reason_ja}）` : ""}</Pill>}
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2">
          <div className="col-span-2"><div className="cap">評価額</div><div className="t32 num">{usd(c.value)}</div></div>
          <div><div className="cap">損益</div><div className="t20 num">{signedUsd(c.change_usd)}</div><div className="cap num">今日 {signedUsd(c.today_usd)}</div></div>
          <div>
            <div className="cap">{c.reward_hours < 24 ? `もらった報酬（${hoursJa(c.reward_hours)}）` : "もらった報酬（24時間）"}</div>
            <div className="t20 num">{usd(c.reward_24h_usd)}</div>
          </div>
        </div>
        {c.spark && c.spark.length > 1 && <Spark values={c.spark} height={40} />}
        <Payback c={c} />
        {c.reference && <div className="cap flex items-center gap-2"><span className="dot dot-n dot-sm" /><span>参考の練習（評価の合否には使いません）</span></div>}
        {c.red_label && <div className="cap flex items-center gap-2"><span className="dot dot-r dot-sm" /><span>判定が危険のときに始めた練習</span></div>}
        {c.bonus_drop && (
          <div className="inset flex items-start gap-4 p-4">
            <span className="flex pt-0.5" style={{ color: "var(--y)" }}><Icon name="clock" /></span>
            <div>
              <div>切り替えでボーナスが{c.bonus_drop.ratio === 0 ? "0になりました" : `前の週の${(c.bonus_drop.ratio * 100).toFixed(0)}%に減りました`}</div>
              <div className="cap">次の切り替えまでの見込みで、いちばん損が少ないのは「{c.bonus_drop.best_ja}」。今は記録だけで、建玉はそのままです（{jst(c.bonus_drop.at)}）</div>
            </div>
          </div>
        )}
      </div>
      {open && onChanged && <ExitButton id={c.id} onDone={onChanged} />}
      {link ? (
        <Fold title="詳しく見る（範囲・置き直し・両替のずれ・日利）">
          <CardDetails c={c} />
          <Link to={`/practice/${c.id}`} className="btn btn-quiet self-start">この建玉の画面を開く<Icon name="right" size={16} /></Link>
        </Fold>
      ) : (
        <Fold title="範囲・置き直し・両替のずれ・日利"><CardDetails c={c} /></Fold>
      )}
    </section>
  );
}

/** 手動の「出る」（N2c。2026-10-02 オーナー依頼: 練習の建玉ごとに。理由は「手動」と記録される） */
function ExitButton({ id, onDone }: { id: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const exit = async () => {
    if (!window.confirm("この練習から出ますか？（建玉を閉じます。お金は動きません）")) return;
    setBusy(true);
    try {
      await postApi(`/api/paper/positions/${id}/close`);
      onDone();
    } catch (e) {
      setErr(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex items-center justify-between gap-4 px-6 pb-4">
      <span className="cap">{err ?? "押すと確認が出ます。閉じた理由は「手動」と残ります。"}</span>
      <button type="button" onClick={exit} disabled={busy} className="ghost shrink-0"><Icon name="xc" size={16} />{busy ? "出ています…" : "出る"}</button>
    </div>
  );
}

/** 自分で選ぶ練習 と アプリ任せの練習 の比べ（形だけ。アプリ任せの練習は N6 で始まる） */
function CompareCard({ data }: { data: Paper }) {
  const all = [...data.open, ...data.closed];
  const pnl = all.reduce((a, c) => a + (c.change_usd ?? 0), 0);
  const capital = data.open.reduce((a, c) => a + (c.capital ?? 0), 0);
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="label flex items-center gap-2"><Icon name="swap" size={16} /><span>自分で選ぶ練習 と アプリ任せの練習</span></div>
      <div className="grid grid-cols-2 gap-2">
        <div className="inset flex flex-col gap-1 p-4">
          <span className="cap">自分で選ぶ</span>
          <span className="bold num">{signedUsd(pnl)}</span>
          <span className="cap">建玉 {data.open.length}つ · 置いている {usd(capital, 0)}</span>
        </div>
        <div className="inset flex flex-col gap-1 p-4">
          <span className="cap">アプリ任せ</span>
          <span className="bold num text-cap">—</span>
          <span className="cap">N6 で始まります</span>
        </div>
      </div>
      <Note>
        アプリ任せの練習は、狙い利回りと早く出る決まりで、同じ期間・同じ金額で自動で出入りします（N6）。
        そのときに、ここで儲けを並べて比べます。左の数字は、今の練習の損益の合計（閉じた分も入れる）です。
      </Note>
    </section>
  );
}

/** 始めた費用を取り返すまで。最初の24時間は「データ不足」の灰色（オーナー依頼 28） */
function Payback({ c }: { c: PaperCard }) {
  if (c.status !== "open" || c.open_cost_usd <= 0) return null;
  const head = `始めた費用 ${usd(c.open_cost_usd)} を取り返すまで`;
  if (c.hours < 24) {
    return <div className="flex flex-wrap items-center gap-2"><Pill icon="info">{head}: データ不足</Pill><span className="cap">24時間たつと出ます</span></div>;
  }
  const v = c.payback_left_hours;
  const text = v === null ? "今のペースでは取り返せません（費用を除いた稼ぎがマイナス）" : v <= 0 ? "取り返しました" :
    `あと約${hoursJa(v)}（全部で約${hoursJa(c.payback_total_hours ?? 0)}）`;
  return <div className="flex flex-wrap items-center gap-2"><Pill icon="info">{head}</Pill><span className="cap">{text}</span></div>;
}

function CardDetails({ c }: { c: PaperCard }) {
  const short = c.actual_state === "short";
  return (
    <>
      <MiniRange c={c} />
      <Line k={<Term k="置き直し">置き直し</Term>} v={`${c.rebalances}回 · 合計 ${usd(c.rebalance_cost)}`} />
      {c.skipped.length > 0 && <Line k="置き直しを見送り中" v="ガス代が高いため" />}
      {c.swap && (c.swap.slippage_pct_now != null || c.swap.open) && (
        <>
          <Line k={<><Term k="スリッページ">両替のずれ</Term>（$550 を両替した場合）</>} v={c.swap.slippage_pct_now == null ? "—" : `${c.swap.slippage_pct_now.toFixed(2)}%`} />
          {c.swap.open && <Line k={`始めた費用のうち、ずれの分${c.swap.open.estimated ? "（今のプールで見積もり）" : ""}`} v={usd(c.swap.open.slippage)} />}
          <Line k="置き直しの費用のうち、ずれの分（これまで）" v={usd(c.swap.rebalance_slippage_total)} />
          {c.swap.rebalance_slippage_next != null && <Line k="次に置き直すときのずれの見込み" v={usd(c.swap.rebalance_slippage_next)} />}
        </>
      )}
      {c.close_cost_usd != null && <Line k="閉じる費用（両替・ガス・ヘッジの手数料）" v={usd(c.close_cost_usd)} />}
      <div className="grid grid-cols-3 gap-2">
        <div className="inset p-4"><div className="cap">予測の純日利</div><div className="bold num">{pct(c.predicted_daily_pct)}</div></div>
        <div className="inset p-4"><div className="cap">実績（費用を除く）</div><div className={`bold num ${short ? "text-cap" : ""}`}>{short ? "データ不足" : pct(c.actual_daily_pct)}</div></div>
        <div className="inset p-4"><div className="cap">実績（費用込み）</div><div className={`bold num ${short ? "text-cap" : ""}`}>{short ? "データ不足" : pct(c.actual_daily_pct_with_cost)}</div></div>
      </div>
      {short && <Note>始めて{c.actual_min_hours}時間たつまでは、実績は大きくぶれるので出しません。</Note>}
      <Note>{jst(c.opened_at)} に開始（{hoursJa(c.hours)}たちました）。最後の計算 {jst(c.last_ts)}。費用込みの日利 = 始めてからの純損益の合計 ÷ たった時間（1日に直す）。</Note>
    </>
  );
}

/** 前の名前のまま（建玉の画面から使う） */
export function ActualRates({ c }: { c: PaperCard }) {
  return <CardDetails c={c} />;
}

function MiniRange({ c }: { c: PaperCard }) {
  const lo = c.lower, hi = c.upper;
  const span = hi - lo;
  const pad = span * 0.25;
  const min = lo - pad, max = hi + pad;
  const x = (p: number) => Math.min(100, Math.max(0, ((p - min) / (max - min)) * 100));
  return (
    <div className="flex flex-col gap-2">
      <div className="label"><Term k="範囲（レンジ）">範囲</Term>と今の値段</div>
      <div className="relative h-3 rounded-full bg-white/[0.06]">
        <div className="absolute inset-y-0 rounded-full bg-white/25" style={{ left: `${x(lo)}%`, width: `${x(hi) - x(lo)}%` }} />
        <div className="absolute -inset-y-1 w-0.5 rounded bg-ink" style={{ left: `${x(c.price)}%` }} />
      </div>
      <div className="cap flex justify-between"><span>下の端まで {edge(c.to_lower_pct)}</span><span>上の端まで {edge(c.to_upper_pct)}</span></div>
    </div>
  );
}

const edge = (v: number) => (v >= 0 ? `${v.toFixed(2)}%` : "外に出ています");

/** 停止・再開・全部閉じる（建玉の下に控えめに。全部閉じるは確認あり。オーナー依頼 26） */
function Controls({ data, reload }: { data: Paper; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const [ask, setAsk] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const run = async (path: string, body?: unknown) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await postApi<{ message: string }>(path, body);
      setMsg(r.message);
      reload();
    } catch (e) {
      setMsg(String((e as Error).message));
    } finally {
      setBusy(false);
      setAsk(false);
    }
  };
  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex flex-wrap justify-end gap-2">
        {data.stopped ? (
          <button type="button" className="ghost" disabled={busy} onClick={() => run("/api/paper/resume")}><Icon name="play" size={16} /><span>再開する</span></button>
        ) : (
          <button type="button" className="ghost" disabled={busy} onClick={() => run("/api/paper/stop")}><Icon name="pause" size={16} /><span>新しい練習を止める</span></button>
        )}
        <button type="button" className="ghost" disabled={busy || data.open.length === 0} onClick={() => setAsk(true)}><Icon name="xc" size={16} /><span>全部閉じる</span></button>
      </div>
      {ask && (
        <div className="inset flex w-full flex-col gap-4 p-4">
          <p>練習の建玉（{data.open.length}件）を全部閉じて、新しく始めるのも止めます。よろしいですか？（お金は動きません）</p>
          <div className="flex gap-2">
            <button type="button" disabled={busy} onClick={() => run("/api/paper/exit_all", { confirm: true })} className="btn btn-danger">はい、全部閉じる</button>
            <button type="button" onClick={() => setAsk(false)} className="btn btn-quiet">やめる</button>
          </div>
        </div>
      )}
      {msg && <p className="sec self-stretch">{msg}</p>}
      <p className="cap text-right">「止める」は新しい建玉を作らないだけで、持っている建玉の見張りは続けます。「全部閉じる」は押すと確認が出ます。</p>
    </div>
  );
}

const LEVEL_TONE: Record<RiskEvent["level"], "y" | "n" | "r"> = {
  caution: "y", rebalance: "n", exit: "r", emergency: "r", info: "n",
};

export function EventList({ events }: { events: RiskEvent[] }) {
  return (
    <div className="flex flex-col gap-2">
      {events.map((e) => (
        <div key={e.id} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-2"><Pill tone={LEVEL_TONE[e.level]}>{e.level_ja}</Pill><span className="cap">{jst(e.ts)}</span></div>
          <div>{e.message}</div>
          {e.action !== "none" && <div className="cap">→ {e.action_ja}</div>}
        </div>
      ))}
    </div>
  );
}

/** たたんでおく項目（説明・記録・見張り） */
function RestFolds({ data }: { data: Paper }) {
  const lim = data.limits;
  return (
    <Folds>
      {data.outlook && <Fold title="資産の見通し（推定）"><OutlookBody o={data.outlook} /></Fold>}
      {data.enabled && (
        <Fold title="タイムライン（新しい順）">
          {data.timeline.length ? <TimelineList items={data.timeline} /> : <p className="cap">まだ記録はありません。30分ごとの定時レビューと、見張りで起きたことがここに並びます。</p>}
          <Link to="/practice/timeline" className="btn btn-quiet self-start">すべて見る<Icon name="right" size={16} /></Link>
        </Fold>
      )}
      {data.enabled && <Fold title="損益カレンダー"><CalendarBody /></Fold>}
      <Fold title="ヘッジ先の担保"><HedgeVenuesBody /></Fold>
      {data.enabled && (
        <Fold title="見張りのルール">
          {data.risk.map((r) => (
            <div key={r.level_ja} className="inset flex flex-col items-start gap-2 p-4">
              <Pill tone={LEVEL_TONE[r.level]}>{r.level_ja}</Pill><div>{r.rule}</div><div className="cap">→ {r.action}</div>
            </div>
          ))}
          <Note>15分ごとの記録のたびに調べます（米国市場が開く30分前〜開いた1時間後は5分ごと。日本時間では夏 22:00〜23:30、冬 23:00〜翌0:30）。数字は config.yaml の risk で変えられます。</Note>
        </Fold>
      )}
      {data.enabled && <Fold title="会場プログラムと USDG の見張り"><WatchBody w={data.watch} /></Fold>}
      <Fold title="練習のしくみと上限">
        <p className="sec">お金は動きません。プールの画面の「試す」で<Term k="建玉" />を作ると、本物の値動きと報酬のデータで損益を毎時記録します。</p>
        <Line k="1つの建玉の上限" v={usd(lim.position_usd ?? null, 0)} />
        <Line k="1つの会場の上限" v={usd(data.venue_cap_usd, 0)} />
        <Line k="いま使っている額" v={usd(data.open_total_usd, 0)} />
        <Line k="1日の取引の上限" v={`${lim.trades_per_day ?? "—"}件`} />
        <Note>上限は config.yaml の limits で決まっています（変えられるのはオーナーだけ）。</Note>
      </Fold>
      {data.enabled && <Fold title="台帳のダウンロード（月ごと）"><CsvBody months={data.ledger_months} /></Fold>}
      <Fold title={`閉じた練習（${data.closed.length}件）`}>
        {data.closed.length === 0 ? <p className="cap">まだありません。</p> : data.closed.map((c) => (
          <Link key={c.id} to={`/practice/${c.id}`} className="inset flex items-center justify-between gap-4 p-4">
            <span className="min-w-0">{c.pair}<span className="cap block">{jst(c.opened_at)}〜{jst(c.closed_at)}{c.close_reason_ja && `（${c.close_reason_ja}）`}</span></span>
            <span className="num">{signedUsd(c.change_usd)}</span>
          </Link>
        ))}
      </Fold>
    </Folds>
  );
}

/** 会場プログラムと USDG の見張り（読めない項目は「未確認」） */
function WatchBody({ w }: { w: Watch }) {
  const unconf = w.contracts.filter((c) => c.unconfirmed.length > 0).length;
  const last = w.contracts.reduce<string | null>((m, c) => (c.checked_at && (!m || c.checked_at > m) ? c.checked_at : m), null);
  const u = w.usdg;
  return (
    <>
      <Line k={`USDG の外の値段（${u?.source === "geckoterminal" ? "GeckoTerminal" : "外部"}）`} v={u ? `$${u.price.toFixed(4)}` : "まだ取れていません"} note={u ? jst(u.ts) : undefined} />
      <Line k="見張っている相手" v={`${w.contracts.length}件`} note={last ? `最後の確認 ${jst(last)}` : undefined} />
      {unconf > 0 && <Line k="「未確認」の項目がある相手" v={`${unconf}件`} />}
      {w.contracts.map((c) => (
        <div key={c.address} className="inset flex flex-col gap-1 p-4">
          <div className="flex items-center justify-between gap-2"><span>{c.label}</span>{c.changed_at && <Pill tone="r">変化 {jst(c.changed_at)}</Pill>}</div>
          {c.ok.length > 0 && <div className="cap">読めている: {c.ok.join("・")}</div>}
          {c.unconfirmed.length > 0 && <div className="cap">未確認: {c.unconfirmed.join("・")}</div>}
        </div>
      ))}
      <Note>15分ごとに読み取りだけで確かめ、停止・持ち主の変更・プログラムの入れ替えがあれば、全部閉じて止めます。「未確認」は、そのプログラムにその項目を読む仕組みがなく、確かめられないという意味です。USDG は外の値段が2回続けて $0.98 を下回ったら、全部閉じて止めます。</Note>
    </>
  );
}
