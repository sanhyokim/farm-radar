import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { postApi, useApi, type HedgeTest, type PaperDetail } from "../api";
import { AssetsLine, HourlyBars } from "../charts";
import { hoursJa, jst, pct, signedUsd, tone, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Note, PageHead, Pill, Term } from "../ui";
import { BreakdownBars } from "./PoolDetail";
import { PositionCard } from "./Practice";
import { OutlookBody, TimelineList } from "./PaperExtras";

const KIND_JA: Record<string, string> = {
  deposit: "入れる", withdraw: "引き出す", claim: "報酬の受け取り", cost: "費用",
  hedge_open: "ヘッジを持つ", hedge_close: "ヘッジを閉じる", sell_reward: "報酬を売る", hedge_adjust: "ヘッジの量を合わせる",
};

// 練習の建玉の画面（SPEC 7.4章・7.6章）。大事な数字を上に、ほかはたたむ
export default function PracticeDetail() {
  const { id = "" } = useParams();
  const { data, error, reload } = useApi<PaperDetail>(`/api/paper/positions/${id}`);
  if (!data) return <><PageHead title="練習" back={{ to: "/practice", label: "練習" }} /><Loading error={error} /></>;
  const d = data;
  const open = d.status === "open";
  const mode = d.mode === "staked" ? "ステークしてボーナス" : d.mode === "rewards" ? "ボーナスだけ" : "ステークせず手数料";
  return (
    <>
      <PageHead title={`練習: ${d.pair}`} at={d.last_ts} back={{ to: "/practice", label: "練習" }} />
      <div className="cap -mt-2">{mode}{!open && ` · 閉じた ${jst(d.closed_at)}`}</div>
      <div className="grid items-start gap-4 lg:grid-cols-2 lg:gap-6">
        <div className="flex flex-col gap-4 lg:gap-6">
          <PositionCard c={d} link={false} />
          {d.red_note && <Card><p className="sec">{d.red_note}</p></Card>}
        </div>
        <div className="flex flex-col gap-4 lg:gap-6">
          <Card title="始めてからの損益（実績）" right={<Pill>実績</Pill>}>
            <div className="grid grid-cols-2 gap-2">
              <div className="inset p-4"><div className="cap"><Term k="実現損益" /></div><div className="bold num">{signedUsd(d.realized)}</div></div>
              <div className="inset p-4"><div className="cap"><Term k="未実現損益" /></div><div className="bold num">{signedUsd(d.unrealized)}</div></div>
            </div>
            <BreakdownBars b={d.total} capital={d.capital} />
            <Note>「その他」には開く時の費用 {usd(d.open_cost_usd)}（両替とガス代）が入っています。</Note>
            {d.days < 1 ? (
              <Note>APY（1年続いた場合の仮の数字）は、始めてから1日たってから表示します（短い時間から1年分に広げると、数字が大きくぶれるため）。</Note>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <div className="inset p-4"><div className="cap"><Term k="表示APY と 純APY">表示APY</Term></div><div className="bold num">{pct(d.apy_display, 0)}</div></div>
                  <div className="inset p-4"><div className="cap">純APY</div><div className="bold num">{pct(d.apy_net, 0)}</div></div>
                </div>
                <Note>APY は{d.apy_note}です（開く時の費用を除いて計算）。</Note>
              </>
            )}
          </Card>
          {open && d.hedge_margin && <HedgeTestCard id={d.id} />}
          <DetailFolds d={d} />
          {open && <CloseButton id={d.id} onDone={reload} />}
        </div>
      </div>
    </>
  );
}

function DetailFolds({ d }: { d: PaperDetail }) {
  return (
    <Folds>
      <Fold title="この建玉のタイムライン（新しい順）">
        {d.timeline.length ? <TimelineList items={d.timeline.slice(0, 12)} /> : (
          <p className="cap">まだ記録はありません。レンジの端に近づく・外に出る・報酬トークンが大きく下がるなどが起きると、理由と一緒にここに残ります。</p>
        )}
      </Fold>
      <Fold title="今日（日本時間 0時から）">
        {d.today ? (
          <>
            <BreakdownBars b={d.today} />
            {d.landing != null && <Line k={<><Term k="着地見込み" />（推定）</>} v={signedUsd(d.landing)} strong />}
          </>
        ) : <p className="cap">今日の記録はまだありません。</p>}
      </Fold>
      <Fold title="戦略開始以降の1日平均">
        {d.since_start ? (
          <><BreakdownBars b={d.since_start} /><Note>{d.since_start.days}日分の平均。1日だけの数字は運に左右されやすいので、平均の方を重視してください。</Note></>
        ) : <p className="cap">まだ記録がありません。</p>}
      </Fold>
      {d.compare && (
        <Fold title="予測と実績のちがい（1日あたり）">
          {!d.compare.enabled ? (
            <>
              <p className="sec">始めて{d.compare.min_hours}時間たってから比べます（あと約{hoursJa(Math.max(0, d.compare.min_hours - d.hours))}）。それより短い時間の実績は、たまたまの動きが大きく、比べても当てになりません。</p>
              <Note>予測の純損益は1日 {signedUsd(d.compare.predicted_net)}（始めた時のスコア）。</Note>
            </>
          ) : (
            <>
              <table className="tbl">
                <thead><tr><th>区分</th><th className="r">予測</th><th className="r">実績</th></tr></thead>
                <tbody>
                  {d.compare.rows.map((r) => (
                    <tr key={r.key}><td className="sec">{r.label}</td>
                      <td className={`num r ${tone(r.predicted)}`}>{signedUsd(r.predicted)}</td>
                      <td className={`num r ${tone(r.actual)}`}>{signedUsd(r.actual)}</td></tr>
                  ))}
                  <tr><td className="bold">純損益</td><td className="num r bold">{signedUsd(d.compare.predicted_net)}</td><td className="num r bold">{signedUsd(d.compare.actual_net)}</td></tr>
                </tbody>
              </table>
              <Note>{d.compare.note}</Note>
            </>
          )}
        </Fold>
      )}
      <Fold title={<Term k="すぐ売る前提">報酬を持ち続けた場合と、すぐ売った場合</Term>}>
        <div className="grid grid-cols-2 gap-2">
          <div className="inset p-4"><div className="cap">持ち続けた場合</div><div className="bold num">{signedUsd(d.sell_now.hold_net)}</div><div className="cap num">値下がり {signedUsd(d.sell_now.hold_haircut)}</div></div>
          <div className="inset p-4"><div className="cap">{d.sell_now.hours}時間で売った場合</div><div className="bold num">{signedUsd(d.sell_now.sell_net)}</div><div className="cap num">値下がり {signedUsd(d.sell_now.sell_haircut)}</div></div>
        </div>
        <Note>始めた時の予測の日利: 持ち続ける前提 {pct(d.sell_now.predicted_hold_pct)} / すぐ売る前提 {pct(d.sell_now.predicted_sell_pct)}。</Note>
      </Fold>
      <Fold title="1時間ごとの純損益（実績・48時間）">
        <HourlyBars bars={d.hourly.bars} />
        {d.hourly.best && (d.hourly.best.net_usd ?? 0) > 0 && <Line k="いちばん稼いだ時間" v={`${d.hourly.best.jst} ${signedUsd(d.hourly.best.net_usd)}`} />}
        {d.hourly.worst && (d.hourly.worst.net_usd ?? 0) < 0 && <Line k="いちばん損した時間" v={`${d.hourly.worst.jst} ${signedUsd(d.hourly.worst.net_usd)}`} />}
        <Note>始めた1時間には、開く時の費用が入っています。</Note>
      </Fold>
      <Fold title="評価額の推移"><AssetsLine series={d.series} capital={d.capital} /></Fold>
      {d.outlook && <Fold title="この建玉の資産の見通し（推定）"><OutlookBody o={d.outlook} /></Fold>}
      <Fold title={<Term k="台帳">台帳（新しい順）</Term>}>
        <div className="flex max-h-80 flex-col overflow-y-auto">
          {d.ledger.map((l, i) => (
            <div key={i} className="flex justify-between gap-4 border-b border-white/[0.06] py-2">
              <span className="min-w-0">{jst(l.ts)} {KIND_JA[l.kind] ?? l.kind}<span className="cap block">{l.note}</span></span>
              <span className="num shrink-0 text-right">
                {l.token === "USD" ? usd(l.value_usd) : `${fmtAmt(l.amount)} ${l.token}`}
                <span className="cap block">
                  {l.token !== "USD" && `${usd(l.value_usd)} · `}{l.fx_rate ? `¥${Math.round((l.value_usd ?? 0) * l.fx_rate).toLocaleString()}（${l.fx_date}のレート）` : "円のレート待ち"}
                </span>
              </span>
            </div>
          ))}
        </div>
      </Fold>
      <Fold title="この計算について">
        <ul className="flex flex-col gap-2">
          {d.notes.map((n) => <li key={n} className="sec">{n}</li>)}
          {d.estimated_rows > 0 && <li>収集が止まっていた時間の記録が {d.estimated_rows} 件あります（推定）。</li>}
        </ul>
      </Fold>
    </Folds>
  );
}

const fmtAmt = (v: number) => (Math.abs(v) >= 1 ? v.toLocaleString("en-US", { maximumFractionDigits: 4 }) : v.toPrecision(4));

/** 保険の試し（N4b）: 「値動きするコインが今から ○% 上がったら」強制決済までの余裕と、アプリがすることを計算する（建玉は変えない） */
function HedgeTestCard({ id }: { id: number }) {
  const [rise, setRise] = useState("30");
  const [res, setRes] = useState<HedgeTest | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => {
    setErr(null);
    try {
      const r = await fetch(`/api/paper/positions/${id}/hedge-test?rise_pct=${encodeURIComponent(rise)}`);
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `エラー ${r.status}`);
      setRes(await r.json());
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };
  const st = res?.status;
  return (
    <Card title="保険の試し（強制決済に近づいたら）" right={<Pill>試しの計算</Pill>}>
      <div className="flex items-center gap-2">
        <span className="cap">値段が</span>
        <input type="number" inputMode="decimal" value={rise} onChange={(e) => setRise(e.target.value)} className="w-20 rounded-lg bg-white/10 px-2 py-1 num" />
        <span className="cap">% 上がったら</span>
        <button type="button" onClick={run} className="ghost">試す</button>
      </div>
      {err && <p className="sec">{err}</p>}
      {st && res && (
        <>
          <Line k="アプリがすること" v={res.would_ja} />
          <Line k="余裕（担保 − 維持に要る額）" v={usd(st.buffer_usd)} note={`はじめの ${st.buffer_frac == null ? "—" : (st.buffer_frac * 100).toFixed(0)}%`} />
          <Line k="保険の損益（その値段で）" v={signedUsd(st.hedge_pnl_usd)} />
          <Line k="お金を足すなら" v={usd(res.options.add.usd)} note="はじめの余裕に戻る額" />
          <Line k="出るなら（閉じる費用の見込み）" v={usd(res.options.exit.close_cost_usd)} />
          {res.message_ja && <Note>{res.message_ja}</Note>}
          <Note>{res.note}{st.mmf_from_lighter ? "" : " 維持の割合は Lighter から読めていないので、仮の値（5%）です。"}</Note>
        </>
      )}
    </Card>
  );
}

function CloseButton({ id, onDone }: { id: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const nav = useNavigate();
  const close = async () => {
    if (!window.confirm("この練習の建玉を閉じますか？（お金は動きません）")) return;
    setBusy(true);
    try {
      await postApi(`/api/paper/positions/${id}/close`);
      onDone();
      nav(`/practice/${id}`);
    } catch (e) {
      setErr(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-end gap-2">
      <button type="button" onClick={close} disabled={busy} className="ghost"><Icon name="xc" size={16} />{busy ? "出ています…" : "出る（この練習を閉じる）"}</button>
      {err && <p className="sec">{err}</p>}
      <p className="cap">押すと確認が出ます。</p>
    </div>
  );
}
