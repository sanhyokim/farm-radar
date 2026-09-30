import { useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { postApi, useApi, type Breakdown, type Check, type ManualBonus, type Paper, type PoolDetail as Detail, type RangeRow, type SwapCosts } from "../api";
import { AssetsLine, HourlyBars, RangeNet, Series } from "../charts";
import { bigUsd, faintPct, jst, jstDay, pct, plainPct, rangeText, ratioPct, signedUsd, untilText, usd, verdictOf, type Verdict } from "../format";
import { Icon, type IconName } from "../icons";
import { Fold, Folds, Item, Line, Loading, Note, PageHead, Pill, Spark, Term, VerdictPill, useWide } from "../ui";

// プールの詳しい画面（SPEC 7.3章。2026-09-30 オーナー依頼 12・13・15・22〜24）
// 結論1行 → いちばん大事な理由 → 両替のずれと保険 → 危険と注意 → なぜこの判定か → 詳しく見る
export default function PoolDetail() {
  const { id = "" } = useParams();
  const wide = useWide();
  const { data, error } = useApi<Detail>(`/api/pools/${encodeURIComponent(id)}`);
  if (!data) return <><PageHead title="プール" back={{ to: "/pools", label: "プール" }} /><Loading error={error} /></>;
  const s = data.score;
  const head = (
    <PageHead title={s.pair} at={s.ts} back={{ to: "/pools", label: "プール" }}
      right={<span className="cap hidden text-right lg:block">{data.venue?.name ?? s.venue_name ?? s.venue_id}{s.is_stock_pair ? " · 株ペア" : ""}</span>} />
  );
  const sub = <div className="cap -mt-2 lg:hidden">{data.venue?.name ?? s.venue_name ?? s.venue_id}{s.is_stock_pair ? " · 株ペア" : ""}{feeText(data)}</div>;
  if (wide) {
    return (
      <>
        {head}
        <div className="grid grid-cols-12 items-start gap-6">
          <div className="col-span-7 flex flex-col gap-6">
            <HeroCard d={data} /><KeyReason d={data} /><StatTiles d={data} three /><WhyCard checks={s.checks ?? []} />
            {s.details.inputs?.manual_bonus && <ManualBonusCard m={s.details.inputs.manual_bonus} estimate={data.venue?.reward_estimate_note} />}
          </div>
          <div className="col-span-5 flex flex-col gap-6">
            <WarnCard d={data} /><DetailFolds d={data} /><ActionCard d={data} />
          </div>
        </div>
      </>
    );
  }
  return (
    <>
      {head}
      {sub}
      <HeroCard d={data} />
      <KeyReason d={data} />
      <StatTiles d={data} />
      <WarnCard d={data} />
      <WhyCard checks={s.checks ?? []} />
      {s.details.inputs?.manual_bonus && <ManualBonusCard m={s.details.inputs.manual_bonus} estimate={data.venue?.reward_estimate_note} />}
      <DetailFolds d={data} />
      <ActionBar d={data} />
    </>
  );
}

const feeText = (d: Detail) => {
  const fee = d.score.details.inputs?.fee;
  return fee == null ? "" : ` · 取引の手数料 ${(fee * 100).toFixed(2)}%`;
};

/** 結論の1行（判定の色と数字から） */
export function conclusion(v: Verdict, net: number | null, hasMajor: boolean): string {
  if (v === "none") return "データが足りないため、判定できません。";
  if (v === "good") return "費用を引いても、稼げる見込みです。";
  if (v === "watch") return (net ?? 0) > 0 ? "稼げる見込みはありますが、気をつける点があります。" : "今は稼げる見込みが小さいです。";
  if (hasMajor && (net ?? 0) > 0) return "数字はプラスですが、危険な点があるので見送りです。";
  return "費用を引くと、マイナスの見込みです。";
}

/** いちばん大きな数字 = 費用とガンマを引いたあとの純日利（オーナー依頼 22） */
export function HeroCard({ d, compact = false }: { d: Detail; compact?: boolean }) {
  const s = d.score;
  const v = verdictOf(s);
  const b = d.daily.breakdown;
  const major = (s.warnings ?? []).some((w) => w.level === "major");
  const hist = d.history.map((h) => h.net_daily_pct).filter((x): x is number => x != null);
  return (
    <section className={`card flex flex-col gap-4 ${compact ? "" : "p-6"}`} style={compact ? { background: "none", border: 0 } : undefined}>
      <div className="flex flex-wrap items-center gap-2"><VerdictPill v={v} />{compact && <span className="cap">{conclusion(v, s.net_daily_pct, major)}</span>}</div>
      {!compact && <p>{conclusion(v, s.net_daily_pct, major)}</p>}
      <div className="flex flex-col gap-2">
        <span className="label">純日利（費用と<Term k="ガンマ損失">ガンマ</Term>を引いたあと）</span>
        <span className={`hero num ${v === "none" || faintPct(s.net_daily_pct) ? "text-cap" : ""}`}>{v === "none" ? "—" : plainPct(s.net_daily_pct)}</span>
        {b && <span className="sec">{usd(d.capital.total, 0)} なら 1日 約 {signedUsd(b.net)}</span>}
      </div>
      {hist.length > 1 && (
        <>
          <Spark values={hist} height={compact ? 40 : 48} />
          <div className="cap flex justify-between"><span>7日前</span><span>今</span></div>
        </>
      )}
    </section>
  );
}

export function KeyReason({ d, inset = false }: { d: Detail; inset?: boolean }) {
  const text = d.score.key_reason ?? d.score.reason_parts?.main;
  if (!text) return null;
  return (
    <section className={`${inset ? "inset p-4" : "card p-6"} flex items-start gap-4`}>
      <span className="flex pt-1 text-sec"><Icon name="info" size={inset ? 16 : 20} /></span>
      <div className="flex flex-col gap-2">{!inset && <span className="label">いちばん大事な理由</span>}<p>{text}</p></div>
    </section>
  );
}

/** 両替のずれ（$550）・保険・プールの大きさ（オーナー依頼 24） */
export function StatTiles({ d, three = false, inset = false }: { d: Detail; three?: boolean; inset?: boolean }) {
  const s = d.score;
  const w = d.swap;
  const tiles: { icon: IconName; label: ReactNode; value: string; sub: string }[] = [
    { icon: "swap", label: <Term k="スリッページ">両替のずれ</Term>, value: s.slippage_pct == null ? "—" : `${s.slippage_pct.toFixed(2)}%`,
      sub: w && s.slippage_pct != null ? `$${w.trade_usd.toFixed(0)} を両替すると 約 ${usd(w.trade_usd * s.slippage_pct / 100)}` : "見積もれません" },
    { icon: "shield", label: <Term k="保険（ヘッジ）">保険</Term>, value: s.hedge_info?.has ? "あり" : "なし",
      sub: s.hedge_info?.has ? `${s.hedge_info.venues.join("・")} で値動きを打ち消す` : "値動きの損をそのまま受ける" },
  ];
  if (three) tiles.push({ icon: "pools", label: <Term k="TVL">プールの大きさ</Term>, value: bigUsd(s.tvl_usd), sub: "ほかの人の預け入れを含む" });
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${tiles.length}, minmax(0, 1fr))` }}>
      {tiles.map((t, i) => (
        <section key={i} className={`${inset ? "inset p-4" : "card px-4 py-6"} flex min-w-0 flex-col gap-2`}>
          <span className="label flex items-center gap-2"><Icon name={t.icon} size={16} /><span>{t.label}</span></span>
          <span className={inset ? "bold" : "t20"}>{t.value}</span>
          <span className="cap">{t.sub}</span>
        </section>
      ))}
    </div>
  );
}

/** 危険と注意（それぞれ1回だけ。危険を上に。オーナー依頼 15） */
export function warnItems(d: Detail): { level: "danger" | "attention"; icon: IconName; title: string; sub?: string }[] {
  const s = d.score;
  const out: { level: "danger" | "attention"; icon: IconName; title: string; sub?: string }[] = [];
  for (const w of s.warnings ?? []) {
    if (w.code === "RWD") continue;      // 下の reward_held で1回だけ出す
    out.push(w.level === "major"
      ? { level: "danger", icon: "alert", title: w.message_ja, sub: "危険な警告です。このため判定は危険になります" }
      : { level: "attention", icon: "alert", title: w.message_ja, sub: "軽い警告です。このため最高でも様子見になります" });
  }
  if (s.reward_held) out.push({ level: "attention", icon: "alert", title: s.reward_held });
  if (s.emission_end && !s.emission_end.ended && s.emission_end.soon) {
    out.push({ level: "attention", icon: "clock", title: `配布終了まで${untilText(s.emission_end.at)}（${jstDay(s.emission_end.at)}）` });
  }
  if (s.epoch_flip) {
    out.push({ level: "attention", icon: "clock", title: `${jstDay(s.epoch_flip.at)} に切り替え（${untilText(s.epoch_flip.at)}）`,
      sub: "来週はボーナスがなくなることもあります" });
  }
  return out.sort((a, b) => (a.level === b.level ? 0 : a.level === "danger" ? -1 : 1));
}

export function WarnCard({ d }: { d: Detail }) {
  const items = warnItems(d);
  const nd = items.filter((x) => x.level === "danger").length;
  return (
    <section className="card flex flex-col gap-2 px-4 pt-6 pb-4">
      <div className="flex items-center gap-2 px-2 pb-2">
        <span className="label flex-1">危険と注意</span>
        <Pill tone={nd ? "r" : "n"}>危険 {nd}</Pill><Pill tone={items.length - nd ? "y" : "n"}>注意 {items.length - nd}</Pill>
      </div>
      {items.length === 0 && <p className="cap px-2">ありません。</p>}
      {items.map((x, i) => (
        <Item key={i} icon={x.icon} color={x.level === "danger" ? "var(--r)" : "var(--y)"} title={x.title} sub={x.sub} />
      ))}
      {d.score.epoch_flip?.why_ja && <p className="cap px-2 pt-2">{d.score.epoch_flip.why_ja}</p>}
    </section>
  );
}

const CHECK_ICON: Record<Check["state"], { icon: IconName; color: string; bg: string }> = {
  ok: { icon: "check", color: "var(--sec)", bg: "rgba(255,255,255,0.08)" },
  info: { icon: "info", color: "var(--sec)", bg: "rgba(255,255,255,0.08)" },
  warn: { icon: "alert", color: "var(--y)", bg: "rgba(251,191,36,0.14)" },
  bad: { icon: "alert", color: "var(--r)", bg: "rgba(248,113,113,0.14)" },
};

/** なぜこの判定か（短い項目で。オーナー依頼 23） */
export function WhyList({ checks }: { checks: Check[] }) {
  return (
    <ul className="flex flex-col gap-4">
      {checks.map((c, i) => {
        const st = CHECK_ICON[c.state];
        return (
          <li key={i} className="flex items-center gap-4">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full" style={{ background: st.bg, color: st.color }}>
              <Icon name={st.icon} size={16} />
            </span>
            <span>{c.text}</span>
          </li>
        );
      })}
    </ul>
  );
}

function WhyCard({ checks }: { checks: Check[] }) {
  if (!checks.length) return null;
  return (
    <section className="card flex flex-col gap-4 p-6">
      <h2 className="label">なぜこの判定か</h2>
      <WhyList checks={checks} />
    </section>
  );
}

const BARS: [keyof Breakdown, ReactNode][] = [
  ["income", "収入（ボーナスと手数料）"],
  ["direction", <Term key="d" k="方向">方向（値動き）</Term>],
  ["gamma", <Term key="g" k="ガンマ損失">ガンマ</Term>],
  ["hedge", <Term key="h" k="ヘッジ">ヘッジ（保険の費用）</Term>],
  ["haircut", <Term key="c" k="報酬トークン値下がり">ボーナスのコインの値下がり</Term>],
  ["other", <Term key="o" k="置き直し">その他（置き直しなど）</Term>],
];

/** 1日の見込み・損益を細い棒で（6つの区分） */
export function BreakdownBars({ b, capital }: { b: Breakdown; capital?: number }) {
  const max = Math.max(1e-9, ...BARS.map(([k]) => Math.abs(b[k] as number)));
  return (
    <div className="flex flex-col gap-4">
      {BARS.map(([k, label]) => {
        const v = b[k] as number;
        return (
          <div key={k} className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-4"><span className="sec">{label}</span><span className="bold num">{signedUsd(v)}</span></div>
            <div className="h-1 rounded-full bg-white/[0.06]">
              <div className="h-1 rounded-full" style={{ width: `${(Math.abs(v) / max) * 100}%`, background: v > 0 ? "var(--text)" : "var(--cap)" }} />
            </div>
          </div>
        );
      })}
      <div className="flex items-center justify-between gap-4 border-t border-white/[0.06] pt-4">
        <span className="bold">純損益</span>
        <span className="bold num">{signedUsd(b.net)}{capital ? `（${pct(b.net / capital * 100)}）` : ""}</span>
      </div>
      <div className="cap flex justify-between gap-4"><span><Term k="本業の稼ぎ">本業の稼ぎ</Term>（収入 − ガンマ）</span><span className="num">{signedUsd(b.core)}</span></div>
      <div className="cap flex flex-wrap gap-x-4 gap-y-1">
        <span><Term k="ヘッジのずれ" />: {signedUsd(b.hedge_gap)}</span>
        <span><Term k="運の要素" />: {ratioPct(b.luck_ratio)}{b.lucky ? "（運に左右された日）" : ""}</span>
      </div>
    </div>
  );
}

/** 前の名前のまま（練習の画面から使う） */
export const BreakdownTable = ({ b }: { b: Breakdown; compact?: boolean }) => <BreakdownBars b={b} />;

/** 詳しく見る（たたんだ項目） */
export function DetailFolds({ d }: { d: Detail }) {
  const s = d.score;
  const b = d.daily.breakdown;
  const ranges = s.details.ranges ?? [];
  const inp = s.details.inputs ?? {};
  const best = ranges.find((r) => r.r_pct === s.best_r);
  const stock = !!s.is_stock_pair;
  return (
    <Folds>
      {b && (
        <Fold title={<span className="bold text-ink">1日の見込み（{usd(d.capital.total, 0)}あたり）</span>} open>
          <BreakdownBars b={b} capital={d.capital.total} />
          {d.venue?.reward_estimate_note && <Note>収入（ボーナス）は{d.venue.reward_estimate_note}です。</Note>}
          <Note>{d.daily.judge_basis}。総資産 {usd(d.capital.total, 0)}（うち LP {usd(d.capital.lp, 0)}）で計算。<Term k="建玉あたり日利">建玉あたり</Term>では {pct(d.daily.net_daily_pct_lp)}。</Note>
        </Fold>
      )}
      <Fold title="計算の式と補足">
        {s.reason_parts?.formula && <p className="num">{s.reason_parts.formula}</p>}
        {(s.reason_parts?.notes ?? []).map((n, i) => <p key={i} className="sec">{n}</p>)}
        {b && (
          <div className="grid grid-cols-2 gap-2">
            <div className="inset p-4"><div className="cap"><Term k="表示APY と 純APY">表示APY</Term></div><div className="bold num">{pct(d.daily.apy_display, 0)}</div></div>
            <div className="inset p-4"><div className="cap">純APY</div><div className="bold num">{pct(d.daily.apy_net, 0)}</div></div>
          </div>
        )}
        <Note>APY は{d.daily.apy_note}です。</Note>
        {d.sell_now && <SellNow sn={d.sell_now} />}
      </Fold>
      <Fold title="1時間ごとのグラフ（48時間）">
        <HourlyBars bars={d.hourly.bars} />
        <div className="cap flex flex-wrap gap-x-4 gap-y-1">
          <span className="flex items-center gap-2"><span className="h-2 w-4 rounded bg-white/10" /><Term k="米国市場時間" /></span>
          <span className="flex items-center gap-2"><span className="h-0.5 w-4 bg-ink" />24時間移動平均</span>
        </div>
        {d.hourly.best && d.hourly.worst && (
          <div className="flex flex-col gap-2">
            <Line k="いちばん稼いだ時間" v={`${d.hourly.best.jst} ${signedUsd(d.hourly.best.net_usd)}`} />
            <Line k="いちばん損した時間" v={`${d.hourly.worst.jst} ${signedUsd(d.hourly.worst.net_usd)}`} />
          </div>
        )}
        <Note>その時間のスコアの「1日の純損益 ÷ 24」を1時間分の見込みとしています。</Note>
      </Fold>
      <Fold title={<span><Term k="範囲（レンジ）">レンジ</Term>と置き直し</span>}>
        {s.range_prices && <div className="inset p-4">最適レンジ ±{s.best_r}% ＝ <span className="num">{rangeText(s.range_prices)}</span></div>}
        <RangeBar price={d.price?.price ?? null} ranges={ranges} best={s.best_r} />
        <div className="cap">レンジ幅ごとの純日利（総資産あたり。白が最適）</div>
        <RangeNet rows={ranges} best={s.best_r} />
        <Line k={<Term k="レンジ内の時間の割合">レンジ内の時間（判定用）</Term>} v={ratioPct(s.in_range_ratio)} note="置き直す前提" />
        <Line k="レンジ内の時間（参考）" v={ratioPct(s.in_range_ratio_hold)} note="置きっぱなしの場合" />
        <Line k="1日の置き直しの回数" v={best ? best.rebalances_per_day.toFixed(1) : "—"} />
      </Fold>
      {d.swap && <Fold title="両替のずれの内訳"><SwapBody w={d.swap} /></Fold>}
      <Fold title="今日の平均と、始めてからの1日平均">
        <div className="label">今日（日本時間 0時から）の平均</div>
        {d.today ? <BreakdownBars b={d.today} /> : <p className="cap">今日の計算はまだありません。</p>}
        <div className="label pt-2">戦略開始以降の1日平均</div>
        {d.since_start ? <><BreakdownBars b={d.since_start} /><Note>{d.since_start.days}日分（{jst(d.since_start.since ?? null)} から）。1日だけの数字は運に左右されやすいので、平均の方を重視してください。</Note></> : <p className="cap">まだデータがありません。</p>}
      </Fold>
      <Fold title="総資産（このプールに48時間いたら）">
        <div className="flex items-baseline justify-between gap-4">
          <span className="t20 num">{usd(d.assets.value)}</span>
          <span className="num sec">{signedUsd(d.assets.change_usd)}（{pct(d.assets.change_pct)}）</span>
        </div>
        <AssetsLine series={d.assets.series} capital={d.assets.capital} />
        <Line k={<Term k="建玉">建玉（LP）</Term>} v={usd(d.assets.parts.lp)} />
        <Line k="財布（予備）" v={usd(d.assets.parts.wallet)} />
        <Line k="証拠金（ヘッジ用）" v={usd(d.assets.parts.margin)} />
        <Line k="未回収報酬" v={usd(d.assets.parts.unclaimed)} />
        <Note>原資 {usd(d.assets.capital, 0)} から、この48時間の予測どおりに増減した場合の推定です。</Note>
      </Fold>
      <Fold title="推移（7日）">
        <div className="cap">純日利（総資産あたり）</div>
        <Series history={d.history} field="net_daily_pct" fmt={(v) => `${v.toFixed(1)}%`} stock={stock} />
        <div className="cap">プールが1日に出すボーナス</div>
        <Series history={d.history} field="reward_usd_day" fmt={(v) => bigUsd(v)} stock={stock} />
        <div className="cap"><Term k="レンジ内の流動性">レンジ内のステーク分</Term>（最適レンジ換算）</div>
        <Series history={d.history} field="staked_inrange_usd" fmt={(v) => bigUsd(v)} stock={stock} />
        <div className="cap">報酬トークンの価格</div>
        <Series history={d.history} field="reward_token_usd" fmt={(v) => `$${v.toPrecision(2)}`} stock={stock} />
        {stock && <Note>少し明るい背景は、米国市場が開いている時間です。</Note>}
      </Fold>
      <Fold title="出典とくわしい数字">
        <Line k={<Term k="値動き（σ）">ペアの値動き（1日）</Term>} v={ratioPct(s.sigma_pair, 1)} />
        <Line k={<Term k="TVL" />} v={bigUsd(s.tvl_usd)} />
        <Line k="24時間の取引量" v={bigUsd(inp.volume_24h_usd)} note={inp.volume_used_usd != null && inp.volume_used_usd !== inp.volume_24h_usd ? `計算は ${bigUsd(inp.volume_used_usd)} まで` : undefined} />
        <Line k="プールが1日に出すボーナス" v={bigUsd(inp.reward_usd_day)} />
        <Line k="レンジ内の流動性（最適レンジ換算）" v={bigUsd(best?.pool_inrange_usd)} note={best?.staked_inrange_usd != null && s.mode !== "rewards" ? `ステーク分 ${bigUsd(best.staked_inrange_usd)}` : undefined} />
        <Line k="自分の取り分（$550 を入れた場合）" v={ratioPct(best?.share_staked, 1)} />
        <Line k="手数料率" v={inp.fee == null ? "—" : ratioPct(inp.fee, 2)} note={s.mode === "rewards" ? "LP には入らない（投票者へ）" : undefined} />
        <Line k="ガス代（1回）" v={usd(inp.gas_usd_per_tx, 4)} />
        {s.vol_source === "external" && <Note>値動きは外部データ（GeckoTerminal）で補っています。</Note>}
        <p className="cap break-all">{s.address}</p>
        <Link to="/venues" className="cap flex items-center gap-1 text-sec">会場の4つの条件を見る<Icon name="right" size={16} /></Link>
      </Fold>
    </Folds>
  );
}

/** 運営が手で足したボーナス。いつものボーナスと分けて見せ、判定には入れない（2026-09-30 オーナー条件4） */
function ManualBonusCard({ m, estimate }: { m: ManualBonus; estimate?: string | null }) {
  const sym = m.symbol ?? "";
  const amt = (v: number | null) => (v == null ? "—" : `${Math.round(v).toLocaleString("en-US")} ${sym}`);
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-2">
        <h2 className="label"><Term k="手で足したボーナス">運営が手で足したボーナス</Term></h2><Pill tone="y">判定に入れない</Pill>
      </div>
      <Line k="今週、手で足された分" v={amt(m.amount)} note={m.usd != null ? `約 ${usd(m.usd, 0)}` : undefined} />
      <Line k="いつものボーナス（今週）" v={amt(m.regular)} note="判定はこちらだけで計算" />
      {m.your_extra_usd_day != null && (
        <Line k="もし入れたら、1日の見込み" v={signedUsd(m.your_extra_usd_day)} note={`参考。${estimate ?? "7日に均等に割った場合"}`} />
      )}
      <Note>運営が、いつもの配り方とは別に、手でボーナスを足しています。来週も続く保証がないため、判定の計算には入れていません。</Note>
    </section>
  );
}

/** 練習のボタン（スマホは下に浮かべる。評価中・観察だけ・練習中は説明に変わる） */
function useAction(d: Detail) {
  const { data } = useApi<Paper>("/api/paper", 0);
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const s = d.score;
  const practicing = data?.open.find((p) => p.pool_id === s.pool_id) ?? null;
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await postApi<{ position_id: number }>("/api/paper/positions", { pool_id: s.pool_id });
      nav(`/practice/${r.position_id}`);
    } catch (e) {
      setErr(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  let text: string;
  let button: ReactNode = null;
  if (d.venue && !d.venue.practice) text = d.venue.practice_note;
  else if (!data) text = "読み込み中…";
  else if (practicing) {
    text = "このプールはすでに練習中です";
    button = <Link to={`/practice/${practicing.id}`} className="btn">建玉を見る</Link>;
  } else if (data.evaluation_block) text = data.evaluation_block.message;
  else if (!data.enabled) text = `今は見るだけのモードです。${data.how_to_enable}`;
  else if (data.stopped) text = "練習は停止中です。練習の画面で再開できます。";
  else {
    text = s.signal === "red" ? "練習用です（判定は危険）。お金は動きません" : "お金は動きません。本物のデータで記録します";
    button = <button type="button" className="btn" disabled={busy} onClick={go}>{busy ? "作っています…" : `${usd(data.capital, 0)} を試す`}</button>;
  }
  return { text, button, err };
}

function ActionBar({ d }: { d: Detail }) {
  const a = useAction(d);
  return (
    <div className="glass fixed inset-x-4 z-40 mx-auto flex max-w-lg items-center justify-between gap-4 rounded-[32px] py-2 pr-2 pl-6"
      style={{ bottom: "calc(env(safe-area-inset-bottom) + 16px)", minHeight: 64 }}>
      <span className="cap flex items-center gap-2 text-sec"><Icon name="flask" size={16} /><span>{a.err ?? a.text}</span></span>
      {a.button}
    </div>
  );
}

export function ActionCard({ d }: { d: Detail }) {
  const a = useAction(d);
  return (
    <section className="card flex items-center justify-between gap-4 py-4 pr-4 pl-6">
      <span className="cap flex items-center gap-2 text-sec"><Icon name="flask" size={16} /><span>{a.err ?? a.text}</span></span>
      {a.button}
    </section>
  );
}

function SellNow({ sn }: { sn: NonNullable<Detail["sell_now"]> }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="label"><Term k="すぐ売る前提">参考: 報酬をすぐ売る前提</Term></div>
      <div className="grid grid-cols-2 gap-2">
        <div className="inset p-4">
          <div className="cap">持ち続ける前提（判定）</div>
          <div className="bold num">{pct(sn.hold_net_daily_pct)}</div>
          <div className="cap num">値下がり {signedUsd(sn.hold_haircut == null ? null : -sn.hold_haircut)}/日</div>
        </div>
        <div className="inset p-4">
          <div className="cap">{sn.hours}時間で売る前提（参考）</div>
          <div className="bold num">{pct(sn.net_daily_pct)}</div>
          <div className="cap num">値下がり {signedUsd(-sn.haircut)}/日 · ±{sn.best_r}%</div>
        </div>
      </div>
      <Note>{sn.note}</Note>
    </div>
  );
}

function RangeBar({ price, ranges, best }: { price: number | null; ranges: RangeRow[]; best: number | null }) {
  if (!ranges.length) return <p className="cap">判定できなかったため、レンジはありません。</p>;
  const max = Math.max(...ranges.map((r) => r.r_pct));
  const x = (p: number) => 50 + (p / max) * 46;
  return (
    <div className="flex flex-col gap-2">
      <svg viewBox="0 0 100 34" className="w-full">
        {ranges.map((r, i) => {
          const y = 4 + i * (26 / ranges.length);
          const on = r.r_pct === best;
          return <rect key={r.r_pct} x={x(-r.r_pct)} y={y} width={x(r.r_pct) - x(-r.r_pct)} height={26 / ranges.length - 1}
            rx={1} fill={on ? "#f2f5f9" : "rgba(255,255,255,0.12)"} />;
        })}
        <line x1={50} x2={50} y1={1} y2={32} stroke="#a1abbb" strokeWidth={0.6} />
      </svg>
      <div className="cap flex justify-between"><span>−{max}%</span><span>今の価格 {price == null ? "—" : price.toPrecision(5)}</span><span>+{max}%</span></div>
      <Note>縦の線が今の価格、帯が各レンジ（上から ±{ranges[0].r_pct}% 〜 ±{max}%）。白い帯が最適レンジ（±{best}%）。</Note>
    </div>
  );
}

/** 両替のずれと、それが費用にいくら含まれるか（2026-09-29 オーナー追加） */
function SwapBody({ w }: { w: SwapCosts }) {
  return (
    <>
      <p>${w.trade_usd.toFixed(0)} を両替したときの値段のずれ <span className="bold num">{w.slippage_pct.toFixed(2)}%</span>
        <span className="cap">（{w.source === "fallback" ? "初期値" : "プールの流動性から計算"}）</span></p>
      <div className="grid grid-cols-2 gap-2">
        <div className="inset flex flex-col gap-1 p-4">
          <div className="bold num">始めた費用 {usd(w.open_total)}</div>
          <Line k="うち 両替のずれ" v={usd(w.slippage)} />
          <Line k="両替の手数料" v={usd(w.swap_fee)} />
          <Line k="ガス代（2回）" v={usd(w.gas)} />
          <Line k="ヘッジの手数料" v={usd(w.hedge_fee)} />
        </div>
        <div className="inset flex flex-col gap-1 p-4">
          <div className="bold num">置き直し1回 {usd(w.rebalance_total)}</div>
          <Line k="うち 両替のずれ" v={usd(w.slippage)} />
          <Line k="両替の手数料" v={usd(w.swap_fee)} />
          <Line k="ガス代（2回）" v={usd(w.gas)} />
        </div>
      </div>
      <Note>両替するのは LP に置く額の半分（{usd(w.swap_usd, 0)}）。ずれは ${w.trade_usd.toFixed(0)} で見積もっているので、少し多めです。</Note>
    </>
  );
}
