// 何枚かの画面で使う部品（画面の見直し。2026-09-30 オーナー依頼 7〜33）
import { Link } from "react-router-dom";
import type { EvalLight, EpochFlip, PoolRow, Signal, Todo } from "../api";
import { VERDICT, bigUsd, faintPct, hoursJa, jstDay, plainPct, untilText, verdictOf, type Verdict } from "../format";
import { Icon, type IconName } from "../icons";
import { Dot, Item, Pill, Ring, Spark, Term } from "../ui";

// --- 今日やること（オーナー依頼 17） ------------------------------------------------------------------
const TODO_ICON: Record<Todo["level"], { icon: IconName; color: string }> = {
  danger: { icon: "alert", color: "var(--r)" },
  attention: { icon: "alert", color: "var(--y)" },
  info: { icon: "info", color: "var(--sec)" },
};

export function TodoCard({ items }: { items: Todo[] }) {
  const need = items.filter((t) => t.level !== "info").length;
  const danger = items.some((t) => t.level === "danger");
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="label flex items-center gap-2"><Icon name="checkc" size={16} /><span>今日やること</span></div>
      <p className="t20">{need === 0 ? "今日やることはありません" : danger ? `すぐに確かめることが${need}つあります` : `確かめることが${need}つあります`}</p>
      {items.length > 0 && (
        <div className="flex flex-col gap-2">
          {items.map((t, i) => {
            const flip = t.level === "info" && t.title.includes("切り替え");
            const st = flip ? { icon: "clock" as IconName, color: "var(--y)" } : TODO_ICON[t.level];
            return <Item key={i} icon={st.icon} color={st.color} title={t.title} sub={t.action} to={t.to} />;
          })}
        </div>
      )}
    </section>
  );
}

// --- 判定の数（小さく1行に。オーナー依頼 19） ---------------------------------------------------------
export function countVerdicts(rows: Pick<PoolRow, "signal" | "net_daily_pct">[]): Record<Verdict, number> {
  const c: Record<Verdict, number> = { good: 0, watch: 0, danger: 0, none: 0 };
  rows.forEach((r) => { c[verdictOf(r)] += 1; });
  return c;
}

export function fromSignalCounts(counts: Record<Signal, number>): Record<Verdict, number> {
  return { good: counts.green ?? 0, watch: counts.yellow ?? 0, danger: counts.red ?? 0, none: 0 };
}

export function CountsInline({ c }: { c: Record<Verdict, number> }) {
  const keys: Verdict[] = ["good", "watch", "danger", ...(c.none > 0 ? ["none" as Verdict] : [])];
  return (
    <span className="cap flex flex-wrap items-center gap-4">
      {keys.map((k) => <span key={k} className="flex items-center gap-2"><Dot v={k} />{VERDICT[k].label} {c[k]}</span>)}
    </span>
  );
}

// --- 木曜の切り替え（1つの画面に1回だけ出す。オーナー依頼 15） ----------------------------------------------
export function FlipNotice({ f, text }: { f: EpochFlip; text?: string }) {
  return (
    <div className="card flex items-start gap-4 px-6 py-4">
      <span className="flex pt-0.5" style={{ color: "var(--y)" }}><Icon name="clock" /></span>
      <div>
        <div>{jstDay(f.at)} にボーナスが<Term k="エポック">切り替わります</Term>（{untilText(f.at)}）</div>
        <div className="cap">{text ?? "来週はボーナスが減ったり、なくなったりすることがあります"}</div>
      </div>
    </div>
  );
}

// --- プールの1行（スマホの一覧・ホームの参考） ----------------------------------------------------------
export function PoolLine({ p, practicing, showVenue = true, compact = false }: {
  p: PoolRow; practicing?: boolean; showVenue?: boolean; compact?: boolean;
}) {
  const v = verdictOf(p);
  const pills: string[] = [];
  if (p.hedge_info) pills.push(p.hedge_info.has ? "保険あり" : "保険なし");
  if (p.slippage_pct != null) pills.push(`両替 ${p.slippage_pct.toFixed(2)}%`);
  if (practicing) pills.push("練習中");
  const meta = v === "none" ? "データ不足" : `大きさ ${bigUsd(p.tvl_usd)}`;
  return (
    <Link to={`/pools/${encodeURIComponent(p.pool_id)}`} className="flex flex-col gap-2 p-4 hover:bg-white/[0.02]">
      <div className="flex items-center gap-4">
        <span className="flex w-4 shrink-0 justify-center"><Dot v={v} /></span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="bold truncate">{p.pair}</span>
          <span className="cap truncate">{showVenue ? `${p.venue_name ?? p.venue_id} · ` : ""}{compact ? pills[0] ?? meta : meta}</span>
        </span>
        <span className="w-14 shrink-0">{p.spark && p.spark.length > 1 ? <Spark values={p.spark} height={24} area={false} /> : null}</span>
        <span className="flex w-16 shrink-0 flex-col text-right">
          <span className={`bold num ${v === "none" || faintPct(p.net_daily_pct) ? "text-cap" : ""}`}>{v === "none" ? "—" : plainPct(p.net_daily_pct)}</span>
          {!compact && <span className="cap">純日利</span>}
        </span>
      </div>
      {!compact && pills.length > 0 && (
        <div className="flex flex-wrap gap-2 pl-8">{pills.map((x) => <Pill key={x}>{x}</Pill>)}</div>
      )}
      {!compact && p.reward_held && (
        <div className="cap flex items-start gap-2 pl-8 text-sec"><Icon name="alert" size={16} color="var(--y)" /><span>{p.reward_held}</span></div>
      )}
      {!compact && p.reward_estimate_note && <div className="cap pl-8">ボーナスは{p.reward_estimate_note}</div>}
      {!compact && p.emission_end && !p.emission_end.ended && (
        <div className="cap pl-8" style={p.emission_end.soon ? { color: "var(--y)" } : undefined}>
          配布終了まで{untilText(p.emission_end.at)}（{jstDay(p.emission_end.at)}）
        </div>
      )}
    </Link>
  );
}

// --- 評価（2週間）の進み具合（オーナー依頼 32） ----------------------------------------------------------
export function EvalProgress({ e, strip = true, ring = 88 }: { e: EvalLight; strip?: boolean; ring?: number }) {
  const running = e.state === "running";
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-6">
        <Ring frac={running ? e.day / e.days : 1} size={ring} big={running ? `${e.day}日目` : e.state === "finished" ? "終了" : "中止"} small={`/ ${e.days}日`} />
        <div className="flex flex-col gap-2">
          <div>
            <div className="bold num">{e.coverage_pct == null ? "—" : `${e.coverage_pct.toFixed(1)}%`}</div>
            <div className="cap">データの集まり具合（{e.min_coverage_pct}%以上が必要）</div>
          </div>
          <div>
            <div className="bold num">{e.ok_days}日</div>
            <div className="cap">予測に近かった日（{e.need_days}日以上で合格）</div>
          </div>
        </div>
      </div>
      {strip && <DayStrip e={e} />}
      <div className="cap">
        {running ? `あと${hoursJa(e.left_hours)}（${jstDay(e.ends_at)} まで）` : `${jstDay(e.ends_at)} に終わりました`}
      </div>
    </div>
  );
}

function DayStrip({ e }: { e: EvalLight }) {
  const cell = (m: EvalLight["marks"][number]) => {
    const base = "flex h-8 items-center justify-center rounded-lg text-[12px] leading-4";
    if (m.state === "ok") return { cls: `${base} text-bg`, style: { background: "var(--g)" } };
    if (m.state === "ng") return { cls: `${base} text-ink`, style: { background: "rgba(255,255,255,0.12)" } };
    if (m.state === "running") return { cls: `${base} text-ink`, style: { border: "2px solid var(--sec)" } };
    return { cls: `${base} text-cap`, style: { background: "rgba(255,255,255,0.05)" } };
  };
  return (
    <>
      <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${e.marks.length}, minmax(0, 1fr))` }}>
        {e.marks.map((m) => {
          const c = cell(m);
          return (
            <div key={m.day} className={c.cls} style={c.style} title={`${m.day}日目${m.flip ? "（切り替えのあった日）" : ""}`}>
              {m.day}{m.flip && <span className="sr-only">切り替え</span>}
            </div>
          );
        })}
      </div>
      <div className="cap flex flex-wrap items-center gap-4">
        <span className="flex items-center gap-2"><span className="h-3 w-3 rounded" style={{ border: "2px solid var(--sec)" }} />今日</span>
        <span className="flex items-center gap-2"><span className="h-3 w-3 rounded" style={{ background: "var(--g)" }} />予測に近かった日</span>
        <span className="flex items-center gap-2"><span className="h-3 w-3 rounded" style={{ background: "rgba(255,255,255,0.12)" }} />外れた日</span>
      </div>
    </>
  );
}
