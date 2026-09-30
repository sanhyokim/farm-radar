import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { postApi, useApi, type PaperDetail } from "../api";
import { AssetsLine, HourlyBars, Waterfall } from "../charts";
import { jst, pct, signedUsd, tone, usd } from "../format";
import { Badge, Card, Loading, Note, Term } from "../ui";
import { BreakdownTable } from "./PoolDetail";
import { PositionCard, hoursJa } from "./Practice";
import { OutlookCard, TimelineList } from "./PaperExtras";

const KIND_JA: Record<string, string> = {
  deposit: "入れる", withdraw: "引き出す", claim: "報酬の受け取り", cost: "費用",
  hedge_open: "ヘッジを持つ", hedge_close: "ヘッジを閉じる", sell_reward: "報酬を売る", hedge_adjust: "ヘッジの量を合わせる",
};

export default function PracticeDetail() {
  const { id = "" } = useParams();
  const { data, error, reload } = useApi<PaperDetail>(`/api/paper/positions/${id}`);
  if (!data) return <Loading error={error} />;
  const d = data;
  const open = d.status === "open";
  return (
    <div className="space-y-3">
      <div>
        <h1 className="text-xl font-bold text-slate-50">練習: {d.pair}</h1>
        <div className="text-xs text-slate-400">{d.venue_id} ・ {d.mode === "staked" ? "ステークしてボーナス" : d.mode === "rewards" ? "ボーナスだけ" : "ステークせず手数料"}
          {!open && ` ・ 閉じた ${jst(d.closed_at)}`}</div>
      </div>

      <PositionCard c={d} link={false} />
      {d.red_note && <Card><p className="text-sm text-rose-200">{d.red_note}</p></Card>}

      <Card title="この建玉のタイムライン（新しい順）">
        {d.timeline.length ? <TimelineList items={d.timeline.slice(0, 12)} /> : (
          <p className="text-sm text-slate-400">まだ記録はありません。レンジの端に近づく・外に出る・報酬トークンが大きく下がるなどが起きると、理由と一緒にここに残ります。</p>
        )}
      </Card>

      <Card title="始めてからの損益（実績）" right={<Badge tone="emerald">実績</Badge>}>
        <div className="rounded-xl bg-slate-800/60 p-3 text-center">
          <div className="text-xs text-slate-400"><Term k="本業の稼ぎ">本業の稼ぎ（収入 − ガンマ）</Term></div>
          <div className={`num text-3xl font-bold ${tone(d.total.core)}`}>{signedUsd(d.total.core)}</div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-center">
          <div className="rounded-xl bg-slate-800/40 p-2">
            <div className="text-xs text-slate-400"><Term k="実現損益" /></div>
            <div className={`num text-lg font-bold ${tone(d.realized)}`}>{signedUsd(d.realized)}</div>
          </div>
          <div className="rounded-xl bg-slate-800/40 p-2">
            <div className="text-xs text-slate-400"><Term k="未実現損益" /></div>
            <div className={`num text-lg font-bold ${tone(d.unrealized)}`}>{signedUsd(d.unrealized)}</div>
          </div>
        </div>
        <BreakdownTable b={d.total} />
        <Waterfall b={d.total} />
        <Note>「その他」には開く時の費用 {usd(d.open_cost_usd)}（両替とガス代）が入っています。</Note>
        {d.days < 1 ? (
          <Note>APY（1年続いた場合の仮の数字）は、始めてから1日たってから表示します（短い時間から1年分に広げると、数字が大きくぶれるため）。</Note>
        ) : (
          <>
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
        <Note>APY は{d.apy_note}です（開く時の費用を除いて計算）。</Note>
          </>
        )}
      </Card>

      <Card title="今日（日本時間 0時から）" right={<Badge tone="emerald">実績</Badge>}>
        {d.today ? (
          <>
            <BreakdownTable b={d.today} compact />
            {d.landing != null && (
              <p className="mt-2 text-sm text-slate-300">
                <Term k="着地見込み" />: <span className={`num font-bold ${tone(d.landing)}`}>{signedUsd(d.landing)}</span>{" "}
                <Badge tone="sky">推定</Badge>
              </p>
            )}
          </>
        ) : <p className="text-sm text-slate-400">今日の記録はまだありません。</p>}
      </Card>

      <Card title="戦略開始以降の1日平均" right={<Badge tone="emerald">実績</Badge>}>
        {d.since_start ? (
          <>
            <BreakdownTable b={d.since_start} compact />
            <Note>{d.since_start.days}日分の平均。判断には1日平均の方を重視してください（1日だけの数字は運に左右されやすい）。</Note>
          </>
        ) : <p className="text-sm text-slate-400">まだ記録がありません。</p>}
      </Card>

      {d.compare && !d.compare.enabled && (
        <Card title="予測と実績のちがい（1日あたり）">
          <p className="text-sm text-slate-300">
            始めて{d.compare.min_hours}時間たってから比べます（あと約{hoursJa(Math.max(0, d.compare.min_hours - d.hours))}）。
            それより短い時間の実績は、たまたまの動きが大きく、比べても当てになりません。
          </p>
          <Note>予測の純損益は1日 {signedUsd(d.compare.predicted_net)}（始めた時のスコア）。</Note>
        </Card>
      )}

      {d.compare && d.compare.enabled && (
        <Card title="予測と実績のちがい（1日あたり）">
          <table className="w-full text-sm">
            <thead><tr className="text-xs text-slate-400"><th className="text-left font-normal">区分</th>
              <th className="text-right font-normal">予測</th><th className="text-right font-normal">実績</th></tr></thead>
            <tbody>
              {d.compare.rows.map((r) => (
                <tr key={r.key} className="border-b border-slate-800/80">
                  <td className="py-1 text-slate-300">{r.label}</td>
                  <td className={`num py-1 text-right ${tone(r.predicted)}`}>{signedUsd(r.predicted)}</td>
                  <td className={`num py-1 text-right ${tone(r.actual)}`}>{signedUsd(r.actual)}</td>
                </tr>
              ))}
              <tr>
                <td className="py-1 font-semibold text-slate-100">純損益</td>
                <td className={`num py-1 text-right font-bold ${tone(d.compare.predicted_net)}`}>{signedUsd(d.compare.predicted_net)}</td>
                <td className={`num py-1 text-right font-bold ${tone(d.compare.actual_net)}`}>{signedUsd(d.compare.actual_net)}</td>
              </tr>
            </tbody>
          </table>
          <Note>{d.compare.note}</Note>
        </Card>
      )}

      <Card title={<Term k="すぐ売る前提">報酬を持ち続けた場合と、すぐ売った場合</Term>}>
        <div className="grid grid-cols-2 gap-2 text-center">
          <div>
            <div className="text-[10px] text-slate-400">持ち続けた場合</div>
            <div className={`num text-lg font-bold ${tone(d.sell_now.hold_net)}`}>{signedUsd(d.sell_now.hold_net)}</div>
            <div className="text-[10px] text-slate-500">値下がり {signedUsd(d.sell_now.hold_haircut)}</div>
          </div>
          <div>
            <div className="text-[10px] text-slate-400">{d.sell_now.hours}時間で売った場合</div>
            <div className={`num text-lg font-bold ${tone(d.sell_now.sell_net)}`}>{signedUsd(d.sell_now.sell_net)}</div>
            <div className="text-[10px] text-slate-500">値下がり {signedUsd(d.sell_now.sell_haircut)}</div>
          </div>
        </div>
        <Note>始めた時の予測の日利: 持ち続ける前提 {pct(d.sell_now.predicted_hold_pct)} / すぐ売る前提 {pct(d.sell_now.predicted_sell_pct)}。どちらが実績に近いかを M5 で比べます。</Note>
      </Card>

      <Card title="1時間ごとの純損益（実績・48時間）">
        <HourlyBars bars={d.hourly.bars} />
        <p className="mt-2 text-xs text-slate-300">
          {d.hourly.best && (d.hourly.best.net_usd ?? 0) > 0 && (
            <>いちばん稼いだ時間: <span className="num text-emerald-400">{d.hourly.best.jst} {signedUsd(d.hourly.best.net_usd)}</span><br /></>
          )}
          {d.hourly.worst && (d.hourly.worst.net_usd ?? 0) < 0 && (
            <>いちばん損した時間: <span className="num text-rose-400">{d.hourly.worst.jst} {signedUsd(d.hourly.worst.net_usd)}</span></>
          )}
        </p>
        <Note>始めた1時間には、開く時の費用が入っています。</Note>
      </Card>

      <Card title="評価額の推移">
        <AssetsLine series={d.series} capital={d.capital} />
      </Card>

      {d.outlook && <OutlookCard o={d.outlook} title="この建玉の資産の見通し" />}

      <Card title={<Term k="台帳">台帳（新しい順）</Term>}>
        <div className="max-h-80 space-y-1 overflow-y-auto text-xs">
          {d.ledger.map((l, i) => (
            <div key={i} className="flex justify-between gap-2 border-b border-slate-800/80 py-1">
              <span className="min-w-0 text-slate-300">{jst(l.ts)} {KIND_JA[l.kind] ?? l.kind}<span className="block text-[10px] text-slate-500">{l.note}</span></span>
              <span className="num shrink-0 text-right text-slate-200">
                {l.token === "USD" ? usd(l.value_usd) : `${fmtAmt(l.amount)} ${l.token}`}
                <span className="block text-[10px] text-slate-500">
                  {l.token !== "USD" && `${usd(l.value_usd)} ・ `}{l.fx_rate ? `¥${Math.round((l.value_usd ?? 0) * l.fx_rate).toLocaleString()}（${l.fx_date}のレート）` : "円のレート待ち"}
                </span>
              </span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="この計算について">
        <ul className="list-disc space-y-1 pl-4 text-xs text-slate-400">
          {d.notes.map((n) => <li key={n}>{n}</li>)}
          {d.estimated_rows > 0 && <li className="text-amber-300">収集が止まっていた時間の記録が {d.estimated_rows} 件あります（推定）。</li>}
        </ul>
      </Card>

      {open && <CloseButton id={d.id} onDone={reload} />}
    </div>
  );
}

const fmtAmt = (v: number) => (Math.abs(v) >= 1 ? v.toLocaleString("en-US", { maximumFractionDigits: 4 }) : v.toPrecision(4));

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
    <div>
      <button onClick={close} disabled={busy} className="w-full rounded-xl border border-rose-500/60 py-3 font-bold text-rose-300 disabled:opacity-50">
        {busy ? "閉じています…" : "この練習を閉じる"}
      </button>
      {err && <p className="mt-2 text-sm text-rose-300">{err}</p>}
    </div>
  );
}
