import { useState } from "react";
import { Link } from "react-router-dom";
import { postApi, useApi, type Evaluation, type Outlook, type PaperCalendar, type TimelineItem } from "../api";
import { jst, pct, signedUsd, tone, usd } from "../format";
import { Badge, Card, Loading, Note } from "../ui";

const TYPE_STYLE: Record<string, { tone: "amber" | "sky" | "rose" | "emerald" | "slate"; label?: string; bar: string }> = {
  review: { tone: "slate", label: "定時レビュー", bar: "border-slate-500" },
  open: { tone: "emerald", label: "開始", bar: "border-emerald-400" },
  close: { tone: "sky", label: "終了", bar: "border-violet-400" },
  caution: { tone: "amber", bar: "border-amber-400" },
  rebalance: { tone: "sky", bar: "border-sky-400" },
  exit: { tone: "rose", bar: "border-rose-400" },
  emergency: { tone: "rose", bar: "border-rose-500" },
  info: { tone: "slate", bar: "border-slate-400" },
};

/** タイムライン（SPEC 7.4章）: 定時レビュー・見張りの記録・開始・終了を、種類ごとに色分けして時刻の順に */
export function TimelineList({ items }: { items: TimelineItem[] }) {
  return (
    <div className="space-y-2">
      {items.map((it) => {
        const st = TYPE_STYLE[it.type === "event" ? it.level : it.type] ?? TYPE_STYLE.info;
        return (
          <div key={it.key} className={`rounded-lg border-l-4 bg-slate-800/40 p-2 text-sm ${st.bar}`}>
            <div className="flex items-center justify-between gap-2">
              <Badge tone={st.tone}>{it.level === "emergency" ? "🚨 " : ""}{it.type === "event" ? it.title : st.label ?? it.title}</Badge>
              <span className="shrink-0 text-xs text-slate-500">{jst(it.ts)}</span>
            </div>
            <div className="mt-1 whitespace-pre-line leading-relaxed text-slate-200">{it.body}</div>
          </div>
        );
      })}
    </div>
  );
}

const FILTERS = [
  { k: "", label: "すべて" }, { k: "review", label: "定時レビュー" }, { k: "event", label: "見張り" },
  { k: "open,close", label: "開始・終了" },
];

export function TimelinePage() {
  const [kind, setKind] = useState("");
  const { data, error } = useApi<{ items: TimelineItem[] }>(`/api/paper/timeline${kind ? `?kind=${kind}` : ""}`);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-slate-100">タイムライン</h1>
        <Link to="/practice" className="text-sm text-sky-300">← 練習へ</Link>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button key={f.k} onClick={() => setKind(f.k)}
            className={`rounded-full px-3 py-1 text-xs ${kind === f.k ? "bg-sky-500/20 text-sky-200" : "bg-slate-800 text-slate-400"}`}>
            {f.label}
          </button>
        ))}
      </div>
      {!data ? <Loading error={error} /> : data.items.length ? <TimelineList items={data.items} /> : (
        <Card><p className="text-sm text-slate-400">まだ記録はありません。</p></Card>
      )}
      <Note>定時レビューは30分ごとに、建玉の様子をルールで文にしたものです（AI は使っていません）。</Note>
    </div>
  );
}

const WEEK = ["月", "火", "水", "木", "金", "土", "日"];

/** 損益カレンダー（日本時間の1日ごとの純損益。緑=プラス、赤=マイナス） */
export function CalendarCard() {
  const [month, setMonth] = useState<string | null>(null);
  const { data, error } = useApi<PaperCalendar>(`/api/paper/calendar${month ? `?month=${month}` : ""}`);
  if (!data) return <Card title="損益カレンダー"><Loading error={error} /></Card>;
  const byDay = Object.fromEntries(data.days.map((d) => [d.day, d]));
  const max = Math.max(1e-9, ...data.days.map((d) => Math.abs(d.net)));
  const cells: (number | null)[] = [...Array(data.first_weekday).fill(null),
    ...Array.from({ length: data.days_in_month }, (_, i) => i + 1)];
  const [y, m] = data.month.split("-");
  return (
    <Card title="損益カレンダー" right={<span className={`num text-sm ${tone(data.total)}`}>{signedUsd(data.total)}</span>}>
      <div className="mb-2 flex items-center justify-between text-sm">
        <button disabled={!data.prev} onClick={() => setMonth(data.prev)} className="px-2 text-sky-300 disabled:text-slate-700">‹ 前の月</button>
        <span className="text-slate-200">{y}年{Number(m)}月</span>
        <button disabled={!data.next} onClick={() => setMonth(data.next)} className="px-2 text-sky-300 disabled:text-slate-700">次の月 ›</button>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center">
        {WEEK.map((w) => <div key={w} className="text-[10px] text-slate-500">{w}</div>)}
        {cells.map((d, i) => {
          if (d === null) return <div key={`e${i}`} />;
          const key = `${data.month}-${String(d).padStart(2, "0")}`;
          const x = byDay[key];
          const a = x ? 0.15 + 0.6 * Math.min(1, Math.abs(x.net) / max) : 0;
          const bg = x ? (x.net >= 0 ? `rgba(16,185,129,${a})` : `rgba(244,63,94,${a})`) : undefined;
          return (
            <div key={key} style={{ background: bg }}
              className={`min-h-[44px] rounded-md p-0.5 ${x ? "" : "bg-slate-800/30"} ${key === data.today ? "ring-1 ring-sky-400" : ""}`}>
              <div className="text-[10px] text-slate-400">{d}</div>
              {x && <div className="num text-[10px] leading-tight text-slate-100">{x.net >= 0 ? "+" : "−"}{Math.abs(x.net) >= 100 ? Math.abs(x.net).toFixed(0) : Math.abs(x.net).toFixed(1)}{x.estimated ? "*" : ""}</div>}
            </div>
          );
        })}
      </div>
      <Note>練習の建玉の純損益（ドル）を日本時間の1日ごとに合計しています。* はパソコンが止まっていた時間を含む推定の日です。始めた日は開く時の費用も入ります。</Note>
    </Card>
  );
}

/** 資産の見通し（SPEC 7.4章。必ず「推定」。2026-09-29 オーナー指示: 1か月後まで・単純な足し算・始めた費用込みの平均・24時間未満は出さない） */
export function OutlookCard({ o, title = "資産の見通し" }: { o: Outlook; title?: string }) {
  return (
    <Card title={title} right={<Badge tone="amber">推定</Badge>}>
      {o.short ? (
        <div className="rounded-lg bg-slate-800/40 p-3 text-center text-sm text-slate-300">
          <span className="rounded-full bg-slate-700 px-2 py-0.5 text-xs text-slate-200">データ不足</span>
          <p className="mt-2">始めてから{o.min_hours}時間たったら出します（今は{o.hours < 1 ? "1時間未満" : `${o.hours.toFixed(1)}時間`}）。</p>
        </div>
      ) : (
        <>
          <div className="mb-2 grid grid-cols-2 gap-2 text-center text-xs">
            <div className="rounded-lg bg-slate-800/40 p-2">
              <div className="text-slate-400">今の評価額</div>
              <div className="num text-base text-slate-100">{usd(o.value_now)}</div>
            </div>
            <div className="rounded-lg bg-slate-800/40 p-2">
              <div className="text-slate-400">始めてからの1日平均（始めた費用込み）</div>
              <div className={`num text-base ${tone(o.daily_usd)}`}>{signedUsd(o.daily_usd)}</div>
              <div className="text-[10px] text-slate-500">{pct(o.daily_pct)}・下限 {signedUsd(o.daily_low_usd)}</div>
            </div>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-slate-400"><th className="text-left font-normal">いつ</th><th className="text-right font-normal">このペースなら</th><th className="text-right font-normal">控えめな下限</th></tr>
            </thead>
            <tbody>
              {o.rows.map((r) => (
                <tr key={r.label} className="border-t border-slate-800">
                  <td className="py-1 text-slate-300">{r.label}</td>
                  <td className="num py-1 text-right text-slate-100">{usd(r.value)}</td>
                  <td className="num py-1 text-right text-slate-300">{usd(r.low)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <Note>{o.note}</Note>
    </Card>
  );
}

/** 台帳の月次CSV（SPEC 12.4章） */
export function CsvCard({ months }: { months: string[] }) {
  if (!months.length) return null;
  return (
    <Card title="台帳のダウンロード（月ごと）">
      <div className="flex flex-wrap gap-2">
        {months.map((m) => (
          <a key={m} href={`/api/paper/ledger.csv?month=${m}`} download
            className="rounded-lg bg-slate-800 px-3 py-1.5 text-sm text-sky-300">{m.replace("-", "年")}月 CSV</a>
        ))}
      </div>
      <Note>練習の取引の記録（日時・種類・数量・ドルと円の金額・円のレートの日付）です。Excel で開けます。税務の形の確認用で、本物の取引ではありません。</Note>
    </Card>
  );
}

const EVAL_STATE: Record<Evaluation["state"], [string, "slate" | "emerald" | "amber" | "sky"]> = {
  not_started: ["まだ始めていません", "slate"], running: ["評価中", "emerald"],
  stopped: ["途中でやめました", "amber"], finished: ["期間が終わりました", "sky"],
};

const hoursJa = (h: number) => (h >= 24 ? `${Math.floor(h / 24)}日${Math.round(h % 24)}時間` : `${h.toFixed(1)}時間`);

/** 2週間の評価（M5d。SPEC 11章）。予測（スコア）と実績（練習の記録）を比べる。合格の基準はまだ決まっていない。 */
export function EvaluationCard() {
  const { data, error, reload } = useApi<Evaluation>("/api/paper/evaluation");
  const [ask, setAsk] = useState<"start" | "stop" | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  if (!data) return <Card title="2週間の評価"><Loading error={error} /></Card>;
  const run = async (what: "start" | "stop") => {
    setBusy(true);
    try {
      const r = await postApi<{ message: string }>(`/api/paper/evaluation/${what}`, { confirm: true });
      setMsg(r.message);
      reload();
    } catch (e) {
      setMsg(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
      setAsk(null);
    }
  };
  const [label, t] = EVAL_STATE[data.state];
  const running = data.state === "running";
  const days = data.evaluation_days;
  return (
    <Card title="2週間の評価" right={<Badge tone={t}>{label}</Badge>}>
      {data.state === "not_started" ? (
        <p className="text-sm leading-relaxed text-slate-300">
          {days}日間、練習の記録と「始める前の見込み（スコア）」を比べて、見込みがどれくらい当たるかを確かめます。
          始めるとこのカードに毎日の結果がたまっていきます（お金は動きません）。
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 text-xs text-slate-400">
            <div>始めた <span className="text-slate-200">{jst(data.started_at ?? null)}</span></div>
            <div>{running ? "残り" : "終わり"} <span className="text-slate-200">{running ? hoursJa(data.left_hours ?? 0) : jst(data.ends_at ?? null)}</span></div>
            <div>記録できた時間 <span className="num text-slate-200">{hoursJa(data.observed_hours ?? 0)}</span></div>
            <div>止まっていた時間（推定） <span className="num text-slate-200">{hoursJa(data.estimated_hours ?? 0)}</span></div>
          </div>
          {data.coverage && data.coverage.length > 0 && (
            <div className="mt-2 space-y-1 text-xs">
              {data.coverage.map((c) => (
                <div key={c.venue_id} className="flex justify-between rounded bg-slate-800/40 px-2 py-1">
                  <span className="text-slate-300">データの集まり具合（{c.venue_id}）</span>
                  <span className="num text-slate-100">{c.ok}/{c.expected}回（{c.ratio === null ? "—" : `${(c.ratio * 100).toFixed(0)}%`}）</span>
                </div>
              ))}
            </div>
          )}
          {data.actual_net_day === null || data.actual_net_day === undefined ? (
            <p className="mt-3 text-sm text-slate-400">まだ比べられる記録がありません。練習の建玉を持つと、1時間ごとに記録がたまります。</p>
          ) : (
            <>
              <div className="mt-3 grid grid-cols-2 gap-2 text-center text-xs">
                <div className="rounded-lg bg-slate-800/40 p-2">
                  <div className="text-slate-400">見込み（1日あたり）</div>
                  <div className={`num text-base ${tone(data.predicted_net_day ?? null)}`}>{signedUsd(data.predicted_net_day ?? null)}</div>
                </div>
                <div className="rounded-lg bg-slate-800/40 p-2">
                  <div className="text-slate-400">実際（1日あたり）</div>
                  <div className={`num text-base ${tone(data.actual_net_day ?? null)}`}>{signedUsd(data.actual_net_day ?? null)}</div>
                  <div className="text-[10px] text-slate-500">見込みとの差 {data.gap_pct === null || data.gap_pct === undefined ? "—" : pct(data.gap_pct)}</div>
                </div>
              </div>
              <table className="mt-2 w-full text-xs">
                <thead><tr className="text-slate-500"><th className="text-left font-normal">内訳（1日あたり）</th><th className="text-right font-normal">見込み</th><th className="text-right font-normal">実際</th></tr></thead>
                <tbody>
                  {(data.compare ?? []).map((c) => (
                    <tr key={c.key} className="border-t border-slate-800">
                      <td className="py-1 text-slate-300">{c.label}</td>
                      <td className={`num text-right ${tone(c.predicted)}`}>{signedUsd(c.predicted)}</td>
                      <td className={`num text-right ${tone(c.actual)}`}>{signedUsd(c.actual)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-2 rounded-lg bg-slate-800/40 p-2 text-xs text-slate-300">
                報酬を持ち続けた場合 <span className={`num ${tone(data.hold_net_day ?? null)}`}>{signedUsd(data.hold_net_day ?? null)}</span> ／
                すぐ売った場合 <span className={`num ${tone(data.sell_net_day ?? null)}`}>{signedUsd(data.sell_net_day ?? null)}</span>（1日あたり）
                {data.closer && <div className="mt-1 text-slate-200">見込みに近いのは「{data.closer === "hold" ? "持ち続けた場合" : "すぐ売った場合"}」です。</div>}
              </div>
            </>
          )}
          {data.events && data.events.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {data.events.map((e) => <Badge key={e.level} tone="slate">{e.level_ja} {e.n}件</Badge>)}
            </div>
          )}
        </>
      )}
      {data.mode === "paper" && !ask && (
        running ? (
          <button disabled={busy} onClick={() => setAsk("stop")}
            className="mt-3 w-full rounded-xl bg-slate-700 py-3 text-sm font-bold text-white disabled:opacity-50">評価をやめる</button>
        ) : (
          <button disabled={busy} onClick={() => setAsk("start")}
            className="mt-3 w-full rounded-xl bg-emerald-700 py-3 text-sm font-bold text-white disabled:opacity-50">
            {data.state === "not_started" ? `${days}日間の評価を始める` : `もう一度${days}日間の評価を始める`}</button>
        )
      )}
      {ask && (
        <div className="mt-3 rounded-xl border border-slate-600 bg-slate-900 p-3 text-sm">
          <p className="text-slate-100">{ask === "start"
            ? `今から${days}日間の評価を始めます。パソコンが止まっている時間は「推定」になり、比べる対象から外れます。よろしいですか？`
            : "評価をやめます（ここまでの記録は残ります）。よろしいですか？"}</p>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <button disabled={busy} onClick={() => run(ask)} className="rounded-lg bg-emerald-700 py-2 font-bold text-white disabled:opacity-50">はい</button>
            <button onClick={() => setAsk(null)} className="rounded-lg bg-slate-700 py-2 text-white">やめる</button>
          </div>
        </div>
      )}
      {msg && <p className="mt-2 text-sm text-slate-200">{msg}</p>}
      {data.note && <Note>{data.note}</Note>}
    </Card>
  );
}
