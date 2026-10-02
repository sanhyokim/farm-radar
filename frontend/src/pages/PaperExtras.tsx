import { useState } from "react";
import { postApi, useApi, type EvalLight, type EvalVerdict, type Evaluation, type Hedges, type HedgeStatus, type Outlook, type PaperCalendar, type RefTrack, type TimelineItem } from "../api";
import { jst, pct, signedUsd, tone, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Line, Loading, Note, PageHead, Pill, Segmented, Term } from "../ui";
import { EvalProgress } from "./parts";

const TYPE_STYLE: Record<string, { tone: "y" | "n" | "r" | "g"; label?: string; bar: string }> = {
  review: { tone: "n", label: "定時レビュー", bar: "var(--cap)" },
  open: { tone: "n", label: "開始", bar: "var(--text)" },
  close: { tone: "n", label: "終了", bar: "var(--sec)" },
  caution: { tone: "y", bar: "var(--y)" },
  rebalance: { tone: "n", bar: "var(--sec)" },
  exit: { tone: "r", bar: "var(--r)" },
  emergency: { tone: "r", bar: "var(--r)" },
  info: { tone: "n", bar: "var(--cap)" },
};

/** タイムライン（SPEC 7.4章）: 定時レビュー・見張りの記録・開始・終了を、時刻の順に */
export function TimelineList({ items }: { items: TimelineItem[] }) {
  return (
    <div className="flex flex-col gap-2">
      {items.map((it) => {
        const st = TYPE_STYLE[it.type === "event" ? it.level : it.type] ?? TYPE_STYLE.info;
        return (
          <div key={it.key} className="inset flex flex-col gap-2 p-4" style={{ borderLeft: `3px solid ${st.bar}` }}>
            <div className="flex items-center justify-between gap-2">
              <Pill tone={st.tone}>{it.type === "event" ? it.title : st.label ?? it.title}</Pill>
              <span className="cap shrink-0">{jst(it.ts)}</span>
            </div>
            <div className="whitespace-pre-line">{it.body}</div>
          </div>
        );
      })}
    </div>
  );
}

type Kind = "" | "review" | "event" | "open,close";

export function TimelinePage() {
  const [kind, setKind] = useState<Kind>("");
  const { data, error } = useApi<{ items: TimelineItem[] }>(`/api/paper/timeline${kind ? `?kind=${kind}` : ""}`);
  return (
    <>
      <PageHead title="タイムライン" back={{ to: "/practice", label: "練習" }} />
      <Segmented<Kind> value={kind} onChange={setKind}
        options={[{ key: "", label: "すべて" }, { key: "review", label: "定時レビュー" }, { key: "event", label: "見張り" }, { key: "open,close", label: "開始・終了" }]} />
      {!data ? <Loading error={error} /> : data.items.length ? <TimelineList items={data.items} /> : (
        <Card><p className="cap">まだ記録はありません。</p></Card>
      )}
      <Note>定時レビューは30分ごとに、建玉の様子をルールで文にしたものです（AI は使っていません）。</Note>
    </>
  );
}

const WEEK = ["月", "火", "水", "木", "金", "土", "日"];

/** 損益カレンダー（日本時間の1日ごとの純損益。濃さで大きさ） */
export function CalendarBody() {
  const [month, setMonth] = useState<string | null>(null);
  const { data, error } = useApi<PaperCalendar>(`/api/paper/calendar${month ? `?month=${month}` : ""}`, 0);
  if (!data) return <Loading error={error} rows={1} />;
  const byDay = Object.fromEntries(data.days.map((d) => [d.day, d]));
  const max = Math.max(1e-9, ...data.days.map((d) => Math.abs(d.net)));
  const cells: (number | null)[] = [...Array(data.first_weekday).fill(null),
    ...Array.from({ length: data.days_in_month }, (_, i) => i + 1)];
  const [y, m] = data.month.split("-");
  return (
    <>
      <div className="flex items-center justify-between">
        <button disabled={!data.prev} onClick={() => setMonth(data.prev)} className="ghost"><Icon name="left" size={16} />前の月</button>
        <span className="bold">{y}年{Number(m)}月 <span className="num sec">{signedUsd(data.total)}</span></span>
        <button disabled={!data.next} onClick={() => setMonth(data.next)} className="ghost">次の月<Icon name="right" size={16} /></button>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center">
        {WEEK.map((w) => <div key={w} className="cap">{w}</div>)}
        {cells.map((d, i) => {
          if (d === null) return <div key={`e${i}`} />;
          const key = `${data.month}-${String(d).padStart(2, "0")}`;
          const x = byDay[key];
          const a = x ? 0.08 + 0.3 * Math.min(1, Math.abs(x.net) / max) : 0;
          return (
            <div key={key} style={{ background: x ? `rgba(255,255,255,${a})` : "rgba(255,255,255,0.03)", outline: key === data.today ? "2px solid var(--sec)" : undefined }}
              className="flex min-h-12 flex-col rounded-lg p-1">
              <div className="cap">{d}</div>
              {x && <div className="num text-[11px] leading-4">{x.net >= 0 ? "+" : "−"}{Math.abs(x.net) >= 100 ? Math.abs(x.net).toFixed(0) : Math.abs(x.net).toFixed(1)}{x.estimated ? "*" : ""}</div>}
            </div>
          );
        })}
      </div>
      <Note>練習の建玉の純損益（ドル）を日本時間の1日ごとに合計しています。* はパソコンが止まっていた時間を含む推定の日です。始めた日は開く時の費用も入ります。</Note>
    </>
  );
}

/** 資産の見通し（SPEC 7.4章。必ず「推定」。24時間未満は出さない） */
export function OutlookBody({ o }: { o: Outlook }) {
  if (o.short) {
    return (
      <div className="inset flex flex-col items-start gap-2 p-4">
        <Pill>データ不足</Pill>
        <p className="sec">始めてから{o.min_hours}時間たったら出します（今は{o.hours < 1 ? "1時間未満" : `${o.hours.toFixed(1)}時間`}）。</p>
        <Note>{o.note}</Note>
      </div>
    );
  }
  return (
    <>
      <div className="grid grid-cols-2 gap-2">
        <div className="inset p-4"><div className="cap">今の評価額</div><div className="bold num">{usd(o.value_now)}</div></div>
        <div className="inset p-4">
          <div className="cap">始めてからの1日平均（費用込み）</div>
          <div className="bold num">{signedUsd(o.daily_usd)}</div>
          <div className="cap num">{pct(o.daily_pct)} · 下限 {signedUsd(o.daily_low_usd)}</div>
        </div>
      </div>
      <table className="tbl">
        <thead><tr><th>いつ</th><th className="r">このペースなら</th><th className="r">控えめな下限</th></tr></thead>
        <tbody>
          {o.rows.map((r) => (
            <tr key={r.label}><td className="sec">{r.label}</td><td className="num r">{usd(r.value)}</td><td className="num r sec">{usd(r.low)}</td></tr>
          ))}
        </tbody>
      </table>
      <Note>推定です。{o.note}</Note>
    </>
  );
}

export function OutlookCard({ o, title = "資産の見通し" }: { o: Outlook; title?: string }) {
  return <Card title={title} right={<Pill tone="y">推定</Pill>}><OutlookBody o={o} /></Card>;
}

/** 台帳の月次CSV（SPEC 12.4章） */
export function CsvBody({ months }: { months: string[] }) {
  if (!months.length) return <p className="cap">まだ記録がありません。</p>;
  return (
    <>
      <div className="flex flex-wrap gap-2">
        {months.map((m) => (
          <a key={m} href={`/api/paper/ledger.csv?month=${m}`} download className="chip"><Icon name="download" size={16} />{m.replace("-", "年")}月</a>
        ))}
      </div>
      <Note>練習の取引の記録（日時・種類・数量・ドルと円の金額・円のレートの日付）です。Excel で開けます。税務の形の確認用で、本物の取引ではありません。</Note>
    </>
  );
}

const EVAL_STATE: Record<Evaluation["state"], [string, "n" | "g" | "y"]> = {
  not_started: ["まだ始めていません", "n"], running: ["評価中", "n"],
  stopped: ["途中でやめました", "y"], finished: ["期間が終わりました", "n"],
  interrupted: ["中断しました", "y"],
};

/** 評価のまとめ（full）から、進み具合の形（ホームと同じ）を作る */
export function toLight(ev: Evaluation): EvalLight | null {
  if (ev.state === "not_started" || !ev.criteria || !ev.started_at || !ev.ends_at) return null;
  const c = ev.criteria;
  const day = Math.min(c.days, Math.floor((ev.elapsed_hours ?? 0) / 24) + 1);
  const rows = ev.days ?? [];
  return {
    state: ev.state, started_at: ev.started_at, ends_at: ev.ends_at, day, days: c.days, left_hours: ev.left_hours ?? 0,
    closed_positions: ev.closed_positions, interrupted: ev.interrupted,
    coverage_pct: c.coverage_pct, min_coverage_pct: c.min_coverage_pct, coverage_ok: c.coverage_ok,
    ok_days: c.hold.ok_days, need_days: c.hold.need_days,
    marks: Array.from({ length: c.days }, (_, k) => {
      const r = rows[k];
      if (r) return { day: k + 1, state: r.done ? (r.hold_ok ? "ok" : "ng") : ev.state === "running" ? "running" : "none", flip: !!r.flip };
      return { day: k + 1, state: ev.state === "running" && k === day - 1 ? "running" : "none", flip: false };
    }),
  };
}

/** 2週間の評価（M5d。SPEC 11章）。予測（スコア）と実績（練習の記録）を比べる */
export function EvaluationCard() {
  const { data, error, reload } = useApi<Evaluation>("/api/paper/evaluation");
  const [ask, setAsk] = useState<"start" | "stop" | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  if (!data) return <Card title="評価（2週間）"><Loading error={error} rows={1} /></Card>;
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
  const light = toLight(data);
  const ref = data.reference;
  return (
    <section className="card flex flex-col overflow-hidden">
      <div className="flex flex-col gap-4 p-6">
        <div className="flex items-center justify-between gap-2">
          <span className="label flex items-center gap-2"><Icon name="flag" size={16} /><span>評価（2週間）の進み具合</span></span>
          <Pill tone={t}>{label}</Pill>
        </div>
        {data.state === "not_started" ? (
          <p className="sec">
            {days}日間、練習の記録と「始める前の見込み（スコア）」を比べて、見込みがどれくらい当たるかを確かめます。お金は動きません。
          </p>
        ) : light && <EvalProgress e={light} />}
        {ref && light && (
          <div className="inset flex flex-col gap-1 p-4">
            <span className="label">参考: その時間の見込みと比べると</span>
            <span><span className="bold num">{ref.ok_days}日</span> が近かった（合否には使いません）</span>
            {ref.differs_days.length > 0 && (
              <span className="cap">{data.criteria?.prediction === "weekly"
                ? `合否の見込みでは外れて、こちらでは近かった日: ${ref.differs_days.map((d) => `${d}日目`).join("・")}（その日のうちの見込みの変化で外れた可能性が高い日）`
                : `始めたときの見込みでは外れて、こちらでは近かった日: ${ref.differs_days.map((d) => `${d}日目`).join("・")}（外れた理由が切り替えだった可能性が高い日）`}</span>
            )}
            {ref.flip_days.length > 0 && <span className="cap">切り替えのあった日: {ref.flip_days.map((d) => `${d}日目`).join("・")}</span>}
          </div>
        )}
        {data.disclaimer && <p className="cap">{data.disclaimer}</p>}
        {msg && <p className="sec">{msg}</p>}
      </div>
      {data.state !== "not_started" && (
        <>
          <Fold title="見込みと実際（1日あたり）">
            {data.actual_net_day == null ? (
              <p className="cap">まだ比べられる記録がありません。練習の建玉を持つと、1時間ごとに記録がたまります。</p>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <div className="inset p-4"><div className="cap">見込み</div><div className="bold num">{signedUsd(data.predicted_net_day ?? null)}</div></div>
                  <div className="inset p-4"><div className="cap">実際</div><div className="bold num">{signedUsd(data.actual_net_day ?? null)}</div>
                    <div className="cap num">見込みとの差 {data.gap_pct == null ? "—" : pct(data.gap_pct)}</div></div>
                </div>
                <table className="tbl">
                  <thead><tr><th>内訳（1日あたり）</th><th className="r">見込み</th><th className="r">実際</th></tr></thead>
                  <tbody>
                    {(data.compare ?? []).map((c) => (
                      <tr key={c.key}><td className="sec">{c.label}</td>
                        <td className={`num r ${tone(c.predicted)}`}>{signedUsd(c.predicted)}</td>
                        <td className={`num r ${tone(c.actual)}`}>{signedUsd(c.actual)}</td></tr>
                    ))}
                  </tbody>
                </table>
                <Line k="報酬を持ち続けた場合" v={signedUsd(data.hold_net_day ?? null)} />
                <Line k="すぐ売った場合" v={signedUsd(data.sell_net_day ?? null)} />
                {data.closer && <p className="sec">見込みに近いのは「{data.closer === "hold" ? "持ち続けた場合" : "すぐ売った場合"}」です。</p>}
              </>
            )}
          </Fold>
          {data.criteria && <Fold title="合格の基準"><Criteria c={data.criteria} weekly={data.weekly} /></Fold>}
          {data.days && data.days.length > 0 && (
            <Fold title="1日ごとの記録"><DayTable days={data.days} weekly={data.criteria?.prediction === "weekly"} /></Fold>
          )}
          <Fold title="記録できた時間とデータの集まり具合">
            <Line k="始めた" v={jst(data.started_at ?? null)} />
            <Line k={running ? "残り" : "終わり"} v={running ? hoursJa(data.left_hours ?? 0) : jst(data.ends_at ?? null)} />
            <Line k="記録できた時間" v={hoursJa(data.observed_hours ?? 0)} />
            <Line k="止まっていた時間（推定）" v={hoursJa(data.estimated_hours ?? 0)} />
            {(data.coverage ?? []).map((c) => (
              <Line key={c.venue_id} k={`データの集まり具合（${c.venue_id}）`} v={`${c.ok}/${c.expected}回（${c.ratio === null ? "—" : `${(c.ratio * 100).toFixed(0)}%`}）`} />
            ))}
            {data.events && data.events.length > 0 && (
              <div className="flex flex-wrap gap-2">{data.events.map((e) => <Pill key={e.level}>{e.level_ja} {e.n}件</Pill>)}</div>
            )}
            {data.note && <Note>{data.note}</Note>}
            {ref && <Note>{ref.note}</Note>}
          </Fold>
        </>
      )}
      {data.reference_tracks && data.reference_tracks.tracks.length > 0 && (
        <Fold title={`参考の練習（合否に使いません）· ${data.reference_tracks.tracks.length}件`}>
          <RefTracks tracks={data.reference_tracks.tracks} note={data.reference_tracks.note} />
        </Fold>
      )}
      {data.mode === "paper" && (
        <div className="flex flex-col gap-2 border-t border-white/[0.06] p-6">
          {!ask && (running ? (
            <button disabled={busy} onClick={() => setAsk("stop")} className="ghost self-start"><Icon name="xc" size={16} />評価をやめる</button>
          ) : (
            <button disabled={busy} onClick={() => setAsk("start")} className="btn self-start">
              {data.state === "not_started" ? `${days}日間の評価を始める` : `もう一度${days}日間の評価を始める`}</button>
          ))}
          {ask && (
            <div className="inset flex flex-col gap-4 p-4">
              <p>{ask === "start"
                ? `今から${days}日間の評価を始めます。いま持っている練習（参考の練習を除く）で合否を出します。パソコンが止まっている時間は「推定」になり、比べる対象から外れます。評価の間は、合否に使う新しい練習は始められません（参考の練習は始められます）。合否に使う建玉が全部閉じたら、評価は「中断」になります。よろしいですか？`
                : "評価をやめます（ここまでの記録は残ります）。よろしいですか？"}</p>
              <div className="flex gap-2">
                <button disabled={busy} onClick={() => run(ask)} className="btn">はい</button>
                <button onClick={() => setAsk(null)} className="btn btn-quiet">やめる</button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

const hoursJa = (h: number) => (h >= 24 ? `${Math.floor(h / 24)}日${Math.round(h % 24)}時間` : `${h.toFixed(1)}時間`);

const RESULT: Record<EvalVerdict["result"], [string, "n" | "g" | "y" | "r"]> = {
  running: ["評価中", "n"], stopped: ["途中でやめた", "y"], interrupted: ["中断（合否なし）", "y"],
  pass: ["合格", "g"], fail: ["不合格", "r"],
};

/** 合格の基準（2026-09-29 オーナー決定）と、持ち続ける前提・すぐ売る前提それぞれの判定 */
function Criteria({ c, weekly }: { c: NonNullable<Evaluation["criteria"]>; weekly?: Evaluation["weekly"] }) {
  const col = (title: string, v: EvalVerdict) => (
    <div className="inset flex flex-col items-start gap-2 p-4">
      <div className="cap">{title}</div>
      <Pill tone={RESULT[v.result][1]}>{RESULT[v.result][0]}</Pill>
      <div className="bold num">{v.ok_days} / {v.need_days}日</div>
      <div className="cap">満たした日 / 合格に必要な日</div>
    </div>
  );
  return (
    <>
      <div className="cap">{c.done_days}/{c.days}日が終わりました</div>
      <div className="grid grid-cols-2 gap-2">{col("報酬を持ち続ける前提", c.hold)}{col("報酬をすぐ売る前提", c.sell)}</div>
      {c.prediction === "weekly" && (
        <div className="inset flex flex-col gap-1 p-4">
          <span className="label">比べる見込み: 週ごと</span>
          <span className="cap">{weekly?.note ?? "木曜の切り替えのあとは、その週の見込みと比べます。"}</span>
          {(weekly?.used ?? []).map((u) => (
            <span key={u.flip_at} className="cap">切り替え {jst(u.flip_at)} → その週の見込みは {jst(u.score_ts)} のスコアから</span>
          ))}
          {c.start_only && (
            <span className="cap">
              参考: 始めたときの見込みだけで比べると、持ち続ける前提 {c.start_only.hold.ok_days}/{c.start_only.hold.need_days}日、
              すぐ売る前提 {c.start_only.sell.ok_days}/{c.start_only.sell.need_days}日（合否には使いません）
            </span>
          )}
        </div>
      )}
      <Line k="データの集まり具合" v={`${c.coverage_pct === null ? "—" : `${c.coverage_pct.toFixed(1)}%`}${c.coverage_ok ? "" : "（足りません）"}`}
        note={`${c.min_coverage_pct}%以上が必要`} />
      <Note>
        基準: ① データの集まり具合が{c.min_coverage_pct}%以上 ② 1日（始めた時刻から24時間ずつ）の純損益の差が、予測の±{c.day_gap_pct}%以内か、
        総資産の{c.day_gap_capital_pct}%以内の日が、{c.days}日の{c.pass_days_pct}%以上。数字は config.yaml の evaluation で変えられます。
      </Note>
    </>
  );
}

function DayTable({ days, weekly }: { days: NonNullable<Evaluation["days"]>; weekly: boolean }) {
  const mark = (ok: boolean | null | undefined, v: number | null | undefined, done: boolean) =>
    v == null ? <span className="text-cap">—</span> : <span>{signedUsd(v)}{done && ok != null ? (ok ? " ○" : " ×") : ""}</span>;
  return (
    <>
      <div className="scroll-x">
        <table className={`tbl ${weekly ? "min-w-[520px]" : "min-w-[420px]"}`}>
          <thead><tr><th>日</th><th className="r">予測</th><th className="r">持ち続け</th><th className="r">すぐ売り</th>
            {weekly && <th className="r">始めの見込み</th>}<th className="r">参考</th></tr></thead>
          <tbody>
            {days.map((d) => (
              <tr key={d.day}>
                <td className="sec">{d.day}日目{d.done ? "" : "（途中）"}{d.flip ? " · 切り替え" : ""}</td>
                <td className="num r">{d.predicted === null ? "—" : signedUsd(d.predicted)}{weekly && (d.week_hours ?? 0) > 0 ? " 週" : ""}</td>
                <td className="num r">{mark(d.hold_ok, d.hold, d.done)}</td>
                <td className="num r">{mark(d.sell_ok, d.sell, d.done)}</td>
                {weekly && <td className="num r sec">{d.predicted_start == null ? "—" : signedUsd(d.predicted_start)}
                  {d.done && d.predicted_start != null ? (d.start_ok ? " ○" : " ×") : ""}</td>}
                <td className="num r sec">{mark(d.reference_ok, d.reference, d.done)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Note>
        ○ は予測に近かった日、× は外れた日。{weekly ? "「週」の印は、その日の予測に切り替えのあとの週の見込みを使ったこと。「始めの見込み」は、始めたときの見込みだけで比べた場合（持ち続ける前提。合否には使いません）。" : ""}
        「参考」は、その時間の最新の見込みと比べたものです（合否には使いません）。
      </Note>
    </>
  );
}

/** 参考の練習（2026-10-01 案B）: 建玉ごとの「見込みと実際」。合否に使わない */
function RefTracks({ tracks, note }: { tracks: RefTrack[]; note: string }) {
  return (
    <>
      {tracks.map((t) => (
        <div key={t.id} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="bold">{t.pair}</span>
            <Pill tone={t.status === "open" ? "n" : "y"}>{t.status === "open" ? "練習中" : "終了"}</Pill>
          </div>
          <Line k="満たした日" v={`${t.ok_days} / ${t.done_days}日`} note={`評価と同じ線なら ${t.need_days}日で合格`} />
          <Line k="見込み（1日あたり）" v={signedUsd(t.predicted_net_day)} />
          <Line k="実際（1日あたり）" v={signedUsd(t.actual_net_day)} />
          <Line k="始めた" v={jst(t.opened_at)} />
          {t.closed_at
            ? <Line k="終わった" v={`${jst(t.closed_at)}${t.close_reason_ja ? `（${t.close_reason_ja}）` : ""}`} />
            : <Line k="14日目の終わり" v={jst(t.ends_at)} />}
          {t.days.length > 0 && (
            <div className="scroll-x">
              <table className="tbl">
                <thead><tr><th>日</th><th className="r">見込み</th><th className="r">実際</th></tr></thead>
                <tbody>
                  {t.days.map((d) => (
                    <tr key={d.day}>
                      <td className="sec">{d.day}日目{d.done ? "" : "（途中）"}</td>
                      <td className="num r">{d.predicted == null ? "—" : signedUsd(d.predicted)}</td>
                      <td className="num r">{d.hold == null ? "—" : signedUsd(d.hold)}{d.done && d.hold != null ? (d.ok ? " ○" : " ×") : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
      <Note>{note}</Note>
    </>
  );
}

const HEDGE_TONE: Record<HedgeStatus["state"], "g" | "y" | "r" | "n"> = {
  ok: "n", short: "y", none: "r", error: "r", waiting: "n",
};

/** ヘッジ先の担保（SPEC 5.2.1章。読み取りだけ） */
export function HedgeVenuesBody() {
  const { data, error } = useApi<Hedges>("/api/hedges", 0);
  if (!data) return <Loading error={error} rows={1} />;
  return (
    <>
      {data.venues.map((v) => (
        <div key={v.hedge_id} className="inset flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="bold">{v.name}</span>
            <Pill tone={HEDGE_TONE[v.status.state]}>{v.status.state_ja}{v.status.paper ? "（練習）" : ""}</Pill>
          </div>
          <Line k="担保" v={usd(v.status.collateral_usd ?? null, 0)} />
          <Line k="必要な額" v={usd(v.need_usd, 0)} />
          <Line k="扱っている先物" v={String(v.markets)} />
          <Line k="取引手数料（最大）" v={v.max_taker_pct == null ? "—" : `${v.max_taker_pct}%`} />
          {v.status.note && <Note>{v.status.note}</Note>}
          {v.real && (
            <Note>本物の口座（{v.address}）: {v.real.state_ja}{v.real.collateral_usd != null && ` · 担保 ${usd(v.real.collateral_usd, 2)}`}
              {v.real.positions && v.real.positions.length > 0 && ` · 建玉 ${v.real.positions.length}件`}</Note>
          )}
        </div>
      ))}
      <Note>読み取りだけです（お金は動かしません）。担保の残高は、config.yaml の hedge_venues か .env にアドレスを書いたときだけ読みます。<Term k="保険（ヘッジ）">保険とは</Term></Note>
    </>
  );
}
