import { useState } from "react";
import { putApi, useApi, type AppSettings, type OppCase, type OppVariant, type OpportunitiesResp, type Opportunity } from "../api";
import { bigUsd, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Note, PageHead, Pill, Segmented, Term } from "../ui";

/**
 * 機会の一覧（N2b。SPEC 13.4、設計案 3章）。自分のお金を入れたあとに「本当に残る利回り」を、金額ごとに、
 * 保険あり・保険なしで並べる。読み取りと計算だけで、お金は動かさない。
 */

type Chain = "all" | "robinhood" | "base";
const CHAINS: { key: Chain; label: string }[] = [
  { key: "all", label: "すべて" }, { key: "robinhood", label: "Robinhood" }, { key: "base", label: "Base" },
];
const KIND: Record<Opportunity["kind"], string> = {
  pool_range: "幅に配るプール", pool_full: "全体に配るプール", hold: "預けるだけ", other: "そのほか",
};

const apr = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "" : "−"}${Math.abs(v).toFixed(1)}%`);
const days = (v: number | null | undefined) => (v == null ? "—" : v < 1 ? `${Math.max(1, Math.round(v * 24))}時間` : `${v.toFixed(v < 10 ? 1 : 0)}日`);

export default function Opportunities() {
  const [amount, setAmount] = useState("1000");
  const [chain, setChain] = useState<Chain>("all");
  const [showExcluded, setShowExcluded] = useState(false);
  const q = `amount=${amount}${chain === "all" ? "" : `&chain=${chain}`}${showExcluded ? "&show_excluded=true" : ""}&limit=200`;
  const { data, error, reload } = useApi<OpportunitiesResp>(`/api/opportunities?${q}`, 5 * 60_000);
  const head = <PageHead title="機会" at={data?.computed_at}
    right={<span className="cap">{data ? `${data.counts.listed}件` : ""}</span>} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const c = data.counts;
  const amounts = data.amounts.map((a) => ({ key: String(a), label: usd(a, 0) }));
  const items = data.items;
  const listed = items.filter((o) => !o.excluded);
  const excluded = items.filter((o) => o.excluded);

  return (
    <>
      {head}
      <Card title={<span className="flex items-center gap-2"><Icon name="flag" size={16} />今の機会</span>}>
        <p className="sec">
          入れる額 {usd(data.amount, 0)} で、<Term k="控えめの見込み">控えめの見込み</Term>が
          <Term k="狙い利回り">狙い利回り</Term>（年{+data.target_apr_pct.toFixed(2)}%）以上なのは <span className="bold">{c.above_target}件</span>。
          一覧に出したのは {c.listed}件です。
        </p>
        <TargetEdit current={data.target_apr_pct} onSaved={reload} />
        <Note>
          数字は「<Term k="本当に残る利回り">本当に残る利回り</Term>」です。表示の年利から、自分のお金で取り分が薄まる分や、
          値動き・保険・入る／出る費用を引いています。総額（{usd(data.amount, 0)}）あたりの年利です。
          もとの数字は、このアプリが保存している一覧（Merkl・Lighter・コインの値段）です。
        </Note>
      </Card>

      <div className="flex flex-col gap-2">
        <span className="cap">入れる額</span>
        <Segmented options={amounts} value={amount} onChange={setAmount} />
        <span className="cap">チェーン</span>
        <Segmented options={CHAINS} value={chain} onChange={setChain} />
      </div>

      {listed.length === 0 ? (
        <Card><p className="sec">この条件で一覧に出せる機会はありません。</p></Card>
      ) : listed.map((o) => <OppCard key={`${o.source}:${o.key}`} o={o} />)}

      <button type="button" className="ghost self-start" aria-pressed={showExcluded} onClick={() => setShowExcluded(!showExcluded)}>
        <Icon name="filter" size={16} />{showExcluded ? "外したものを隠す" : `外したものも見る（${c.excluded}件）`}
      </button>
      {showExcluded && excluded.map((o) => <OppCard key={`${o.source}:${o.key}`} o={o} />)}

      <Folds>
        <Fold title={`計算できなかったもの（${c.not_computable}件）`}>
          {data.not_computable.length === 0 ? <span className="cap">ありません。</span> : data.not_computable.map(([r, n]) => (
            <Line key={r} k={r} v={`${n}件`} />
          ))}
          <Note>値段の記録がないコインは、値動きが分からないので計算しません（あとで記録が増えると計算できるようになります）。</Note>
        </Fold>
        <Fold title="計算のきまり（仮の数字）">
          <Line k="控えめの見込み" v={`ほかの人のお金 × ${data.settings.cautious_tvl_multiple}`} />
          <Line k="1つの機会に入れてよい上限" v={`預かり額の ${(data.settings.max_pool_share * 100).toFixed(0)}%`} />
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

/** 狙い利回りを変える（設定。リスク上限とは別のもの） */
function TargetEdit({ current, onSaved }: { current: number; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(String(current));
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { data: settings } = useApi<AppSettings>("/api/settings", 0);
  const save = async () => {
    setBusy(true);
    try {
      const r = await putApi<AppSettings>("/api/settings", { target_apr_pct: Number(value) });
      setMsg(`狙い利回りを年${r.target_apr_pct}% にしました。`);
      setOpen(false);
      onSaved();
    } catch (e) {
      setMsg(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <div className="flex flex-col gap-2">
        <button type="button" className="ghost self-start" onClick={() => { setValue(String(current)); setOpen(true); setMsg(null); }}>
          <Icon name="flag" size={16} />狙い利回りを変える
        </button>
        {msg && <span className="cap">{msg}</span>}
      </div>
    );
  }
  return (
    <div className="inset flex flex-col gap-4 p-4">
      <label className="flex items-center gap-2">
        <span className="sec">年</span>
        <input type="number" inputMode="decimal" min={1} max={1000} step={1} value={value}
          onChange={(e) => setValue(e.target.value)} aria-label="狙い利回り（年%）"
          className="card num w-28 px-4 py-2 text-right" style={{ borderRadius: 12, color: "var(--text)" }} />
        <span className="sec">%</span>
      </label>
      <span className="cap">
        はじめの値は年{settings?.default_target_apr_pct ?? 30}%。練習の自動の出入りと、一覧の並べ替えに使います。
        本番で入るかどうかは毎回自分で決めます。
      </span>
      <div className="flex gap-2">
        <button type="button" className="btn" disabled={busy} onClick={save}>保存する</button>
        <button type="button" className="ghost" disabled={busy} onClick={() => setOpen(false)}>やめる</button>
      </div>
      {msg && <span className="cap" style={{ color: "var(--y)" }}>{msg}</span>}
    </div>
  );
}

function OppCard({ o }: { o: Opportunity }) {
  const b = o.best;
  const amountKey = Object.keys(o.calc)[0];
  const row = amountKey ? o.calc[amountKey] : null;
  const warn = o.flags.filter((f) => f.level === "warn");
  const excl = o.flags.filter((f) => f.level === "exclude");
  const info = o.flags.filter((f) => f.level === "info");
  const noHedgeMarket = o.flags.some((f) => f.code === "NO_HEDGE");
  // 値段の記録がないボーナスのコイン: 控えめの見込みは仮の値下がりで計算（オーナー依頼 2026-10-02）
  const guess = o.flags.some((f) => f.code === "RWD_GUESS");
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col">
          <span className="bold" style={{ overflowWrap: "anywhere" }}>{o.name ?? o.key}</span>
          <span className="cap">{[o.chain_name, o.venue_name ?? o.venue, KIND[o.kind]].filter(Boolean).join(" · ")}</span>
        </div>
        {o.excluded ? <Pill tone="r" icon="alert">外した</Pill>
          : o.above_target ? <Pill tone="g" icon="check">狙い以上</Pill>
          : <Pill>狙いより低い</Pill>}
      </div>

      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col">
          <span className="cap"><Term k="控えめの見込み">控えめの見込み</Term>（{b?.hedge ? "保険あり" : "保険なし"}・年利）</span>
          <span className="t32 num">{apr(b?.apr_pct)}</span>
          {guess && <span className="pt-1"><Pill tone="y" icon="alert">値下がり未計算（仮の値で計算）</Pill></span>}
        </div>
        <div className="flex flex-col items-end">
          <span className="cap">1日に残る額</span>
          <span className="num">{usd(b?.net_after_move ?? null)}</span>
        </div>
      </div>

      {row && (
        <div className="grid grid-cols-2 gap-2">
          <Side title="保険なし" c={row} k="no_hedge" />
          <Side title="保険あり" c={row} k="hedge"
            empty={noHedgeMarket ? "保険の売り場（Lighter）がない" : "値動きしないコインだけなので、保険はいらない"} />
        </div>
      )}

      <div className="flex flex-col gap-1">
        <Line k="表示のボーナスの年利" v={apr(o.shown_apr_pct)} note="割り引く前。預け先の利息は入っていない" />
        <Line k={<Term k="TVL">預かり額</Term>} v={bigUsd(o.tvl_usd)} />
        <Line k="配る期間の残り" v={days(o.days_left)} />
        {b && <Line k={<Term k="入る・出る費用">入る・出る費用</Term>} v={usd(b.move_cost)}
          note={b.payback_days == null ? "残る額がマイナスなので取り返せない" : `${days(b.payback_days)}で取り返す`} />}
        {o.cap_usd != null && (
          <Line k="入れてよい上限" v={<span style={{ color: o.over_cap ? "var(--y)" : undefined }}>{bigUsd(o.cap_usd)}</span>}
            note={o.over_cap ? "入れる額が上限より多い（自分のお金で大きく薄まる）" : undefined} />
        )}
      </div>

      {o.unprotected.length > 0 && (
        <div className="inset flex flex-col gap-1 p-4">
          <span className="cap flex items-center gap-2" style={{ color: "var(--y)" }}>
            <Icon name="shield" size={16} /><Term k="保険で守れない値動き">保険で守れない値動き</Term>
          </span>
          {o.unprotected.map((u) => <span key={u} className="cap">・{u}</span>)}
        </div>
      )}
      {[...excl, ...warn].map((f) => (
        <span key={f.code + f.text} className="cap flex items-start gap-2" style={{ color: f.level === "exclude" ? "var(--r)" : "var(--y)" }}>
          <Icon name="alert" size={16} /><span>{f.text}</span>
        </span>
      ))}
      {o.new_pool && <Pill tone="y" icon="info">始まったばかり</Pill>}

      {o.url && (
        <a href={o.url} target="_blank" rel="noreferrer" className="cap flex items-center gap-2 text-sec">
          <Icon name="link" size={16} />会場のページを開く（見るだけ。入れるかどうかは自分で決める）
        </a>
      )}
      <div className="-mx-6 -mb-6 flex flex-col">
        {b && (
          <Fold title="くわしい内訳（控えめ・1日あたり）">
            <Breakdown v={b} />
          </Fold>
        )}
        {info.length > 0 && (
          <Fold title={`印（${info.length}）`}>
            {info.map((f) => <span key={f.code} className="cap flex items-start gap-2"><Icon name="info" size={16} /><span>{f.text}</span></span>)}
          </Fold>
        )}
      </div>
    </section>
  );
}

function Side({ title, c, k, empty }: { title: string; c: { normal: OppCase; cautious: OppCase }; k: keyof OppCase; empty?: string }) {
  const cau = c.cautious[k], nor = c.normal[k];
  return (
    <div className="inset flex flex-col gap-1 p-4">
      <span className="cap">{title}</span>
      {cau == null ? <span className="cap">{empty ?? "計算できない"}</span> : (
        <>
          <span className="bold num">{apr(cau.apr_pct)}</span>
          <span className="cap num">ふつう {apr(nor?.apr_pct)}</span>
          {cau.split.hedge_margin > 0 && (
            <span className="cap num">置く {usd(cau.split.pool, 0)} · 預け金 {usd(cau.split.hedge_margin, 0)}</span>
          )}
        </>
      )}
    </div>
  );
}

function Breakdown({ v }: { v: OppVariant }) {
  const minus = (x: number) => (x > 0 ? `−${usd(x, 3)}` : usd(0, 3));
  return (
    <div className="flex flex-col gap-2">
      <Line k="機会に置く" v={usd(v.split.pool, 0)} />
      {v.split.hedge_margin > 0 && <Line k={<Term k="保険の預け金">保険の預け金</Term>} v={usd(v.split.hedge_margin, 0)} note="Lighter に預ける（自動で計算）" />}
      <Line k="予備" v={usd(v.split.reserve, 0)} />
      <Line k="受け取る分" v={usd(v.income, 3)} note={`ボーナスの取り分（預けるだけの型は、預け先の利息も）${v.points ? "。ポイントは0として数えた" : ""}`} />
      {v.in_range_ratio != null && <Line k="幅の中にいる時間" v={`${(v.in_range_ratio * 100).toFixed(0)}%`} note={v.range_pct != null ? `幅 ±${v.range_pct.toFixed(1)}%` : undefined} />}
      <Line k={<Term k="ガンマ損失">値動きの目減り</Term>} v={minus(v.gamma)} />
      <Line k={<Term k="置き直し">置き直し</Term>} v={minus(v.rebalance)} />
      <Line k="保険の費用" v={minus(v.hedge_cost)} />
      <Line k={<Term k="報酬トークン値下がり">ボーナスのコインの値下がり</Term>} v={minus(v.haircut)} />
      <Line k={<Term k="方向">保険を掛けていない値動きの損</Term>} v={minus(v.direction)} />
      <Line k="1日に残る額（入る・出る前）" v={usd(v.net, 3)} />
      <Line k="入る・出る費用 ÷ いる日数" v={minus(v.move_cost / v.stay_days)} note={`${usd(v.move_cost)} を ${days(v.stay_days)}で割る`} />
      <Line k="1日に残る額" v={usd(v.net_after_move, 3)} strong />
    </div>
  );
}
