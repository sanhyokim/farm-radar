import { Link, useSearchParams } from "react-router-dom";
import { useApi, type OpportunitiesResp, type Opportunity } from "../api";
import { bigUsd, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Note, PageHead, Segmented, Term, useWide } from "../ui";
import { CHAINS, GuessPill, SafetyPill, TargetEdit, apr, days, isGuess, type Chain } from "./opp";

/**
 * 探す（N2c。SPEC 13.1 の追加の決定 2）: 「機会」と「プール」をまとめた、入れる先の1行ずつの表。
 * 並べる項目: 残る年利（控えめ）・1日に残る額・安全度（仮）・残りの日数・チェーンと会場。狙い利回り以上の行は色を変える。
 * 行を押すと詳しい画面。読み取りと計算だけで、お金は動かさない。
 */
export default function Explore() {
  const wide = useWide();
  const [sp, setSp] = useSearchParams();
  const amount = sp.get("amount") ?? "1000";
  const chain = (sp.get("chain") as Chain | null) ?? "all";
  const showExcluded = sp.get("excluded") === "1";
  const set = (k: string, v: string | null) => {
    const n = new URLSearchParams(sp);
    if (v == null) n.delete(k); else n.set(k, v);
    setSp(n, { replace: true });
  };
  const q = `amount=${amount}${chain === "all" ? "" : `&chain=${chain}`}${showExcluded ? "&show_excluded=true" : ""}&limit=300`;
  const { data, error, reload } = useApi<OpportunitiesResp>(`/api/opportunities?${q}`, 5 * 60_000);
  const head = <PageHead title="探す" at={data?.computed_at}
    right={<span className="cap">{data ? `${data.counts.listed}件` : ""}</span>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const c = data.counts;
  const amounts = data.amounts.map((a) => ({ key: String(a), label: usd(a, 0) }));
  const listed = data.items.filter((o) => !o.excluded);
  const excluded = data.items.filter((o) => o.excluded);
  const back = `?${sp.toString()}`;

  return (
    <>
      {head}
      <Card>
        <p className="sec">
          入れる額 {usd(data.amount, 0)} で、<Term k="控えめの見込み">控えめの見込み</Term>が
          <Term k="狙い利回り">狙い利回り</Term>（年{+data.target_apr_pct.toFixed(2)}%）以上なのは <span className="bold">{c.above_target}件</span>
          （色の付いた行）。
        </p>
        <TargetEdit current={data.target_apr_pct} onSaved={reload} />
      </Card>

      <div className={wide ? "grid grid-cols-2 gap-4" : "flex flex-col gap-2"}>
        <div className="flex flex-col gap-2">
          <span className="cap">入れる額</span>
          <Segmented options={amounts} value={amount} onChange={(v) => set("amount", v)} />
        </div>
        <div className="flex flex-col gap-2">
          <span className="cap">チェーン</span>
          <Segmented options={CHAINS} value={chain} onChange={(v) => set("chain", v === "all" ? null : v)} />
        </div>
      </div>

      <section className="card flex flex-col overflow-hidden">
        {wide && <HeadRow />}
        {listed.length === 0 ? <p className="sec p-6">この条件で一覧に出せる入れる先はありません。</p>
          : listed.map((o) => <Row key={`${o.source}:${o.key}`} o={o} wide={wide} back={back} />)}
      </section>

      <button type="button" className="ghost self-start" aria-pressed={showExcluded} onClick={() => set("excluded", showExcluded ? null : "1")}>
        <Icon name="filter" size={16} />{showExcluded ? "外したものを隠す" : `外したものも見る（${c.excluded}件）`}
      </button>
      {showExcluded && excluded.length > 0 && (
        <section className="card flex flex-col overflow-hidden">
          {excluded.map((o) => <Row key={`${o.source}:${o.key}`} o={o} wide={wide} back={back} />)}
        </section>
      )}

      <Folds>
        <Fold title="この表の見方">
          <Note>
            年利は「<Term k="本当に残る利回り">本当に残る利回り</Term>」の控えめの見込みです。表示の年利から、自分のお金で取り分が薄まる分や、
            値動き・保険・入る／出る費用を引いて、総額（{usd(data.amount, 0)}）あたりで出しています。保険あり・なしの良い方です。
          </Note>
          <Note>
            安全度は、N4 で「危なさの点数」ができるまでの仮の3段階です。会場の情報と、今ある印（外す・注意・保険で守れない値動き・
            残りの日数）から出しています。行を押すと理由が見られます。
          </Note>
        </Fold>
        <Fold title={`計算できなかったもの（${c.not_computable}件）`}>
          {data.not_computable.length === 0 ? <span className="cap">ありません。</span> : data.not_computable.map(([r, n]) => (
            <Line key={r} k={r} v={`${n}件`} />
          ))}
          <Note>値段の記録がないコインは、値動きが分からないので計算しません（あとで記録が増えると計算できるようになります）。</Note>
        </Fold>
        <Fold title="計算のきまり（仮の数字）">
          <Line k="控えめの見込み" v={`ほかの人のお金 × ${data.settings.cautious_tvl_multiple}`} />
          <Line k="1つの入れる先に入れてよい上限" v={`預かり額の ${(data.settings.max_pool_share * 100).toFixed(0)}%`} />
          <Line k="小さすぎて外す預かり額" v={`${bigUsd(data.settings.min_tvl_usd)} 未満`} note="始まったばかりのプールは外さず「新しい」の印" />
          <Line k="いる日数（入る・出る費用を割る）" v={`${data.settings.stay_days}日`} note="配る期間が短ければ、その残りの日数" />
          <Line k="幅に配るプールの幅" v={`±${data.settings.merkl_range_pct}%`} />
          <Line k={<Term k="保険の預け金">保険の預け金</Term>} v={`${data.settings.hedge_withstand_rise_pct}% 上がっても耐える額`} />
          <Note>これらは仮の数字で、試し（N5）で決め直します。手数料の収入は分からないので数えていません（安全側）。</Note>
        </Fold>
      </Folds>
    </>
  );
}

const HIT_BG = "rgba(52, 211, 153, 0.08)";      // 狙い利回り以上の行（色は印として使う）

function HeadRow() {
  return (
    <div className="cap grid gap-4 px-6 py-3" style={{ gridTemplateColumns: "minmax(0,1fr) 112px 112px 128px 88px 20px", borderBottom: "1px solid var(--line-soft)" }}>
      <span>入れる先（チェーン · 会場）</span>
      <span className="text-right">残る年利（控えめ）</span>
      <span className="text-right">1日に残る額</span>
      <span>安全度</span>
      <span className="text-right">残りの日数</span>
      <span />
    </div>
  );
}

function Row({ o, wide, back }: { o: Opportunity; wide: boolean; back: string }) {
  const b = o.best;
  const hit = !o.excluded && !!o.above_target;
  const where = [o.chain_name, o.venue_name ?? o.venue].filter(Boolean).join(" · ");
  const to = `/explore/${encodeURIComponent(o.key)}${back}`;
  const style = { background: hit ? HIT_BG : undefined, borderTop: "1px solid var(--line-soft)",
    boxShadow: hit ? "inset 3px 0 0 var(--g)" : undefined };
  const guess = isGuess(o);
  if (wide) {
    return (
      <Link to={to} className="grid items-center gap-4 px-6 py-4 hover:bg-white/[0.02]"
        style={{ ...style, gridTemplateColumns: "minmax(0,1fr) 112px 112px 128px 88px 20px" }}>
        <span className="flex min-w-0 flex-col gap-1">
          <span className="bold truncate">{o.name ?? o.key}</span>
          <span className="cap truncate">{where}</span>
          {(guess || o.excluded) && <span className="flex flex-wrap gap-2">{guess && <GuessPill />}{o.excluded && <ExcludedNote o={o} />}</span>}
        </span>
        <span className="t20 num text-right">{apr(b?.apr_pct)}</span>
        <span className="num text-right">{usd(b?.net_after_move ?? null)}</span>
        <span><SafetyPill s={o.safety} short /></span>
        <span className="num text-right">{days(o.days_left)}</span>
        <span className="flex text-sec"><Icon name="right" size={16} /></span>
      </Link>
    );
  }
  return (
    <Link to={to} className="flex flex-col gap-2 px-4 py-4 hover:bg-white/[0.02]" style={style}>
      <div className="flex items-start gap-4">
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="bold truncate">{o.name ?? o.key}</span>
          <span className="cap truncate">{where}</span>
        </span>
        <span className="flex shrink-0 flex-col text-right">
          <span className="t20 num">{apr(b?.apr_pct)}</span>
          <span className="cap num">1日 {usd(b?.net_after_move ?? null)}</span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <SafetyPill s={o.safety} />
        <span className="cap">残り {days(o.days_left)}</span>
        {guess && <GuessPill />}
        {o.excluded && <ExcludedNote o={o} />}
      </div>
    </Link>
  );
}

function ExcludedNote({ o }: { o: Opportunity }) {
  const f = o.flags.find((x) => x.level === "exclude");
  return <span className="cap" style={{ color: "var(--r)" }}>外した: {f?.text ?? ""}</span>;
}
