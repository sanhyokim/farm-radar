import { useState } from "react";
import { Link } from "react-router-dom";
import { postApi, useApi, type Paper, type PaperCard, type RiskEvent, type Watch } from "../api";
import { jst, pct, signedUsd, tone, usd } from "../format";
import { Badge, Card, Loading, Note, Term } from "../ui";
import { CalendarCard, CsvCard, EvaluationCard, HedgeVenuesCard, OutlookCard, TimelineList } from "./PaperExtras";

export default function Practice() {
  const { data, error, reload } = useApi<Paper>("/api/paper");
  if (!data) return <Loading error={error} />;
  const lim = data.limits;
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100"><Term k="練習">練習</Term>（ペーパートレード）</h1>

      <Card title="状態" right={data.enabled ? <Badge tone="emerald">練習モード</Badge> : <Badge tone="amber">見るだけモード</Badge>}>
        {data.enabled ? (
          <p className="text-sm leading-relaxed text-slate-300">
            お金は動きません。プール詳細の「このプールで {usd(data.capital, 0)} を試す」で<Term k="建玉" />を作ると、
            本物の値動きと報酬のデータで損益を毎時記録します。
          </p>
        ) : (
          <p className="text-sm leading-relaxed text-slate-300">練習するには、{data.how_to_enable}</p>
        )}
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-400">
          <div>1つの建玉の上限 <span className="num text-slate-200">{usd(lim.position_usd ?? null, 0)}</span></div>
          <div>1つの会場の上限 <span className="num text-slate-200">{usd(data.venue_cap_usd, 0)}</span></div>
          <div>いま使っている額 <span className="num text-slate-200">{usd(data.open_total_usd, 0)}</span></div>
          <div>1日の取引の上限 <span className="num text-slate-200">{lim.trades_per_day ?? "—"}件</span></div>
        </div>
        <Note>上限は config.yaml の limits で決まっています（変えられるのはオーナーだけ）。</Note>
      </Card>

      {data.enabled && <Controls data={data} reload={reload} />}

      {data.open.length === 0 && (
        <Card><p className="text-sm text-slate-400">練習中の建玉はありません。プール → 好きなプール →「試す」で始められます。</p></Card>
      )}
      {data.open.map((c) => <PositionCard key={c.id} c={c} />)}

      {data.outlook && <OutlookCard o={data.outlook} />}

      {data.enabled && (
        <Card title="タイムライン（新しい順）" right={<Link to="/practice/timeline" className="text-xs text-sky-300">すべて見る ›</Link>}>
          {data.timeline.length ? <TimelineList items={data.timeline} /> : (
            <p className="text-sm text-slate-400">まだ記録はありません。30分ごとの定時レビューと、見張りで起きたことがここに並びます。</p>
          )}
        </Card>
      )}

      {data.enabled && <EvaluationCard />}

      <HedgeVenuesCard />

      {data.enabled && <CalendarCard />}

      {data.enabled && (
        <Card title="見張りのルール">
          <div className="space-y-2">
            {data.risk.map((r) => (
              <div key={r.level} className="rounded-lg bg-slate-800/40 p-2 text-sm">
                <Badge tone={LEVEL_TONE[r.level]}>{r.level_ja}</Badge>
                <div className="mt-1 text-slate-200">{r.rule}</div>
                <div className="text-xs text-slate-400">→ {r.action}</div>
              </div>
            ))}
          </div>
          <Note>15分ごとの記録のたびに調べます（米国市場が開く30分前〜開いた1時間後は5分ごと。日本時間では夏 22:00〜23:30、冬 23:00〜翌0:30）。数字は config.yaml の risk で変えられます。</Note>
        </Card>
      )}

      {data.enabled && <WatchCard w={data.watch} />}

      {data.enabled && <CsvCard months={data.ledger_months} />}

      {data.closed.length > 0 && (
        <Card title="閉じた練習">
          <div className="space-y-2">
            {data.closed.map((c) => (
              <Link key={c.id} to={`/practice/${c.id}`} className="flex items-center justify-between rounded-lg bg-slate-800/40 p-2 text-sm">
                <span className="text-slate-200">{c.pair} <span className="text-xs text-slate-500">{jst(c.opened_at)}〜{jst(c.closed_at)}</span>
                  {c.close_reason_ja && <span className="ml-1 text-xs text-slate-400">（{c.close_reason_ja}）</span>}</span>
                <span className={`num ${tone(c.change_usd)}`}>{signedUsd(c.change_usd)}</span>
              </Link>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

export function PositionCard({ c, link = true }: { c: PaperCard; link?: boolean }) {
  const body = (
    <Card
      title={<span>{c.pair} <span className="text-xs font-normal text-slate-400">±{c.r_pct}%</span></span>}
      right={c.in_range ? <Badge tone="emerald">レンジ内</Badge> : <Badge tone="rose">レンジ外</Badge>}
    >
      {(c.red_label || c.rebalances > 0 || c.cautions.length > 0 || c.skipped.length > 0) && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {c.red_label && <Badge tone="rose">{c.red_label}</Badge>}
          {c.rebalances > 0 && <Badge tone="sky">置き直し {c.rebalances}回</Badge>}
          {c.cautions.map((x) => <Badge key={x} tone="amber">注意: {x}</Badge>)}
          {c.skipped.length > 0 && <Badge tone="amber">ガス代が高く見送り中</Badge>}
        </div>
      )}
      <MiniRange c={c} />
      <div className="mt-3 grid grid-cols-2 gap-2 text-center">
        <div className="rounded-xl bg-slate-800/60 p-2">
          <div className="text-xs text-slate-400">評価額</div>
          <div className="num text-xl font-bold text-slate-50">{usd(c.value)}</div>
          <div className={`num text-xs ${tone(c.change_usd)}`}>{signedUsd(c.change_usd)}（{pct(c.change_pct)}）</div>
        </div>
        <div className="rounded-xl bg-slate-800/60 p-2">
          <div className="text-xs text-slate-400">
            {c.reward_hours < 24 ? `もらった報酬（始めて${hoursJa(c.reward_hours)}ぶん）` : "直近24時間にもらった報酬"}
          </div>
          <div className={`num text-xl font-bold ${tone(c.reward_24h_usd)}`}>{signedUsd(c.reward_24h_usd)}</div>
          <div className="text-xs text-slate-500">入れた額 {usd(c.capital, 0)}</div>
        </div>
      </div>
      <div className="mt-2 flex items-center justify-between rounded-xl bg-slate-800/40 px-3 py-2 text-sm">
        <span className="text-slate-400"><Term k="置き直し">置き直し</Term>の回数と費用</span>
        <span className="num text-slate-200">{c.rebalances}回・合計 <span className={c.rebalance_cost > 0 ? "text-rose-300" : ""}>{usd(c.rebalance_cost)}</span></span>
      </div>
      {c.swap && (c.swap.slippage_pct_now != null || c.swap.open) && (
        <div className="mt-2 rounded-xl bg-slate-800/40 px-3 py-2 text-xs text-slate-300">
          <div className="flex justify-between">
            <span className="text-slate-400"><Term k="スリッページ">両替のずれ</Term>（$550 を両替した場合）</span>
            <span className="num">{c.swap.slippage_pct_now == null ? "—" : `${c.swap.slippage_pct_now.toFixed(2)}%`}</span>
          </div>
          {c.swap.open && (
            <div className="flex justify-between">
              <span className="text-slate-400">始めた費用のうち、ずれの分{c.swap.open.estimated ? "（今のプールで見積もり）" : ""}</span>
              <span className="num text-rose-300">{usd(c.swap.open.slippage)}</span>
            </div>
          )}
          <div className="flex justify-between">
            <span className="text-slate-400">置き直しの費用のうち、ずれの分（これまで）</span>
            <span className="num">{usd(c.swap.rebalance_slippage_total)}</span>
          </div>
          {c.swap.rebalance_slippage_next != null && (
            <div className="flex justify-between">
              <span className="text-slate-400">次に置き直すときのずれの見込み</span>
              <span className="num">{usd(c.swap.rebalance_slippage_next)}</span>
            </div>
          )}
        </div>
      )}
      {c.close_cost_usd != null && (
        <div className="mt-2 flex items-center justify-between rounded-xl bg-slate-800/40 px-3 py-2 text-sm">
          <span className="text-slate-400">閉じる費用（両替・ガス・ヘッジの手数料）</span>
          <span className="num text-rose-300">{usd(c.close_cost_usd)}</span>
        </div>
      )}
      <ActualRates c={c} />
      <Note>{jst(c.opened_at)} に開始（{hoursJa(c.hours)}たちました）。最後の計算 {jst(c.last_ts)}。</Note>
    </Card>
  );
  return link ? <Link to={`/practice/${c.id}`} className="block">{body}</Link> : body;
}

/** 実績の純日利（2026-09-29 オーナー指示: 6時間未満は参考・小さく、費用込みも並べ、取り返す見込み時間も出す） */
export function ActualRates({ c }: { c: PaperCard }) {
  const short = c.actual_state === "short";
  return (
    <div className="mt-2 rounded-xl bg-slate-800/40 p-2">
      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <div className="text-[10px] text-slate-400">予測の純日利</div>
          <div className={`num ${tone(c.predicted_daily_pct)}`}>{pct(c.predicted_daily_pct)}</div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">実績（費用を除く）</div>
          <div className={`num ${short ? "text-xs text-slate-500" : tone(c.actual_daily_pct)}`}>{pct(c.actual_daily_pct)}</div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">実績（始めた費用込み）</div>
          <div className={`num ${short ? "text-xs text-slate-500" : tone(c.actual_daily_pct_with_cost)}`}>{pct(c.actual_daily_pct_with_cost)}</div>
        </div>
      </div>
      {short && (
        <p className="mt-1 text-center text-[11px] text-slate-400">
          <span className="rounded-full bg-slate-700 px-2 py-0.5 text-slate-200">参考（データ不足）</span>{" "}
          始めて{c.actual_min_hours}時間たつまでは、実績は大きくぶれます
        </p>
      )}
      <div className="mt-2 text-xs text-slate-300">
        始めた費用（両替・ガス・ヘッジの手数料）{usd(c.open_cost_usd)} を取り返すまで:{" "}
        {c.open_cost_usd <= 0 ? "費用はかかっていません" :
          c.payback_left_hours === null ? <span className="text-rose-300">今のペースでは取り返せません（費用を除いた稼ぎがマイナス）</span> :
          c.payback_left_hours <= 0 ? <span className="text-emerald-300">取り返しました</span> :
          <span className="num">あと約{hoursJa(c.payback_left_hours)}（全部で約{hoursJa(c.payback_total_hours ?? 0)}）</span>}
      </div>
      <Note>費用込みの日利 = 始めてからの純損益の合計 ÷ たった時間（1日に直す）。取り返す時間は、今までの稼ぎのペースが続いた場合の見込みです。</Note>
    </div>
  );
}

export function hoursJa(h: number): string {
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}分`;
  if (h < 48) return `${h < 10 ? h.toFixed(1) : Math.round(h)}時間`;
  return `${(h / 24).toFixed(1)}日`;
}

const LEVEL_TONE: Record<RiskEvent["level"], "amber" | "sky" | "rose" | "slate"> = {
  caution: "amber", rebalance: "sky", exit: "rose", emergency: "rose", info: "slate",
};

export function EventList({ events }: { events: RiskEvent[] }) {
  return (
    <div className="space-y-2">
      {events.map((e) => (
        <div key={e.id} className="rounded-lg bg-slate-800/40 p-2 text-sm">
          <div className="flex items-center justify-between gap-2">
            <Badge tone={LEVEL_TONE[e.level]}>{e.level === "emergency" ? "🚨 " : ""}{e.level_ja}</Badge>
            <span className="text-xs text-slate-500">{jst(e.ts)}</span>
          </div>
          <div className="mt-1 leading-relaxed text-slate-200">{e.message}</div>
          {e.action !== "none" && <div className="text-xs text-slate-400">→ {e.action_ja}</div>}
        </div>
      ))}
    </div>
  );
}

/** 停止・全部閉じる・再開（SPEC 12.5章。全部閉じるは確認あり） */
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
    <Card title="操作" right={data.stopped ? <Badge tone="rose">停止中</Badge> : <Badge tone="emerald">動いています</Badge>}>
      {data.stopped && data.stopped_reason && (
        <p className="mb-2 text-sm text-rose-200">止めた理由: {data.stopped_reason}（{jst(data.stopped_since)}）</p>
      )}
      <div className="grid grid-cols-2 gap-2">
        {data.stopped ? (
          <button disabled={busy} onClick={() => run("/api/paper/resume")}
            className="rounded-xl bg-emerald-700 py-3 text-sm font-bold text-white disabled:opacity-50">再開する</button>
        ) : (
          <button disabled={busy} onClick={() => run("/api/paper/stop")}
            className="rounded-xl bg-slate-700 py-3 text-sm font-bold text-white disabled:opacity-50">停止する</button>
        )}
        <button disabled={busy || data.open.length === 0} onClick={() => setAsk(true)}
          className="rounded-xl bg-rose-800 py-3 text-sm font-bold text-white disabled:opacity-40">全部閉じる</button>
      </div>
      {ask && (
        <div className="mt-3 rounded-xl border border-rose-500/50 bg-rose-950/40 p-3 text-sm">
          <p className="text-rose-100">練習の建玉（{data.open.length}件）を全部閉じて、新しく始めるのも止めます。よろしいですか？（お金は動きません）</p>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <button disabled={busy} onClick={() => run("/api/paper/exit_all", { confirm: true })}
              className="rounded-lg bg-rose-700 py-2 font-bold text-white disabled:opacity-50">はい、全部閉じる</button>
            <button onClick={() => setAsk(false)} className="rounded-lg bg-slate-700 py-2 text-white">やめる</button>
          </div>
        </div>
      )}
      {msg && <p className="mt-2 text-sm text-slate-200">{msg}</p>}
      <Note>停止: 新しい建玉を作らない（持っている建玉の計算と見張りは続けます）。全部閉じる: 持っている建玉を全部閉じて、停止にします。</Note>
    </Card>
  );
}

function MiniRange({ c }: { c: PaperCard }) {
  const lo = c.lower, hi = c.upper;
  const span = hi - lo;
  const pad = span * 0.25;
  const min = lo - pad, max = hi + pad;
  const x = (p: number) => Math.min(100, Math.max(0, ((p - min) / (max - min)) * 100));
  return (
    <div>
      <div className="relative h-4 rounded bg-slate-800">
        <div className="absolute inset-y-0 rounded bg-amber-400/40" style={{ left: `${x(lo)}%`, width: `${x(hi) - x(lo)}%` }} />
        <div className="absolute inset-y-[-3px] w-0.5 bg-sky-400" style={{ left: `${x(c.price)}%` }} />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-slate-400">
        <span>下の端まで {edge(c.to_lower_pct)}</span>
        <span>上の端まで {edge(c.to_upper_pct)}</span>
      </div>
    </div>
  );
}

const edge = (v: number) => (v >= 0 ? `${v.toFixed(2)}%` : "外に出ています");

/** 会場プログラムと USDG の見張り（2026-09-29 オーナー決定。読めない項目は「未確認」） */
function WatchCard({ w }: { w: Watch }) {
  const unconf = w.contracts.filter((c) => c.unconfirmed.length > 0).length;
  const last = w.contracts.reduce<string | null>((m, c) => (c.checked_at && (!m || c.checked_at > m) ? c.checked_at : m), null);
  const u = w.usdg;
  return (
    <Card title="会場プログラムと USDG の見張り">
      <div className="space-y-1 text-sm text-slate-300">
        <div>
          USDG の外の値段（{u?.source === "geckoterminal" ? "GeckoTerminal" : "外部"}）:{" "}
          {u ? <span className={`num ${u.price < 0.98 ? "text-rose-300" : "text-slate-100"}`}>${u.price.toFixed(4)}</span> : <span className="text-slate-500">まだ取れていません</span>}
          {u && <span className="text-xs text-slate-500">（{jst(u.ts)}）</span>}
        </div>
        <div>
          見張っている相手 <span className="num text-slate-100">{w.contracts.length}</span> 件
          {last && <span className="text-xs text-slate-500">（最後の確認 {jst(last)}）</span>}
          {unconf > 0 && <>。そのうち <span className="num text-amber-300">{unconf}</span> 件に「未確認」の項目があります</>}
        </div>
      </div>
      {w.contracts.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-sky-300">相手ごとの内訳を見る</summary>
          <div className="mt-2 space-y-2">
            {w.contracts.map((c) => (
              <div key={c.address} className="rounded-lg bg-slate-800/40 p-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-slate-200">{c.label}</span>
                  {c.changed_at && <Badge tone="rose">変化 {jst(c.changed_at)}</Badge>}
                </div>
                {c.ok.length > 0 && <div className="mt-1 text-slate-400">読めている: {c.ok.join("・")}</div>}
                {c.unconfirmed.length > 0 && <div className="text-amber-300/90">未確認: {c.unconfirmed.join("・")}</div>}
              </div>
            ))}
          </div>
        </details>
      )}
      <Note>
        15分ごとに読み取りだけで確かめ、停止・持ち主の変更・プログラムの入れ替えがあれば、全部閉じて止めます。
        「未確認」は、そのプログラムにその項目を読む仕組みがなく、確かめられないという意味です。
        USDG は外の値段が2回続けて $0.98 を下回ったら、全部閉じて止めます。
      </Note>
    </Card>
  );
}
