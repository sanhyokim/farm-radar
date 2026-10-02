import { useState } from "react";
import { putApi, useApi, type AppSettings, type OppCase, type OppVariant, type Opportunity, type Safety } from "../api";
import { usd } from "../format";
import { Icon } from "../icons";
import { Line, Pill, Term } from "../ui";

// 「探す」と詳しい画面で使う部品（N2c。N2b の「機会」から移した）

export type Chain = "all" | "robinhood" | "base";
export const CHAINS: { key: Chain; label: string }[] = [
  { key: "all", label: "すべて" }, { key: "robinhood", label: "Robinhood" }, { key: "base", label: "Base" },
];
export const KIND: Record<Opportunity["kind"], string> = {
  pool_range: "幅に配るプール", pool_full: "全体に配るプール", hold: "預けるだけ", other: "そのほか",
};

export const apr = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "" : "−"}${Math.abs(v).toFixed(1)}%`);
export const days = (v: number | null | undefined) =>
  v == null ? "—" : v < 1 ? `${Math.max(1, Math.round(v * 24))}時間` : `${v.toFixed(v < 10 ? 1 : 0)}日`;

const SAFETY_TONE = { low: "g", mid: "y", high: "r", very_high: "r" } as const;

/** 危なさ（N4a。点が多いほど危ない。仮）。色は印だけ（数字は白。オーナー依頼 14） */
export function SafetyPill({ s, short = false }: { s: Safety | null | undefined; short?: boolean }) {
  if (!s) return <Pill>危なさ —</Pill>;
  return (
    <Pill tone={SAFETY_TONE[s.level]} icon="shield">
      {short ? s.label : `危なさ ${s.label}`} {Math.round(s.score)}点{s.provisional ? "（仮）" : ""}
    </Pill>
  );
}

/** 値段の記録がないボーナスのコインの印（オーナー依頼 2026-10-02: 一覧の行と詳しい画面に目立つ印） */
export const isGuess = (o: Opportunity) => o.flags.some((f) => f.code === "RWD_GUESS");
export const GuessPill = () => <Pill tone="y" icon="alert">値下がり未計算（仮の値で計算）</Pill>;

/** 会場を契約の住所で見分けられていない印（オーナー依頼 2026-10-02 17:07 JST。N4a で住所の見分けを始めた） */
export const isUncertainVenue = (o: Opportunity) => !!o.safety?.uncertain_match;
export const VenueMatchPill = () => <Pill tone="y" icon="alert">仮・会場の見分けが不確か</Pill>;
/** おすすめ（狙い以上で会場の見分けが確か）。見分けが不確かな行はおすすめに入れない（オーナー 2026-10-02 23:15 JST） */
export const isRecommended = (o: Opportunity) => !o.excluded && !!o.recommended;

/** 狙い利回りを変える（設定。リスク上限とは別のもの） */
export function TargetEdit({ current, onSaved }: { current: number; onSaved: () => void }) {
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

/** 保険なし・保険あり の並び（控えめの年利と、ふつうの年利） */
export function Side({ title, c, k, empty }: { title: string; c: { normal: OppCase; cautious: OppCase }; k: keyof OppCase; empty?: string }) {
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

/** とても小さい割合も読めるように（0.0123% など） */
function sharePct(x: number): string {
  const p = x * 100;
  return `${p >= 1 ? p.toFixed(1) : p >= 0.01 ? p.toFixed(3) : p.toPrecision(2)}%`;
}

/** 計算の内訳（控えめ・1日あたり） */
export function Breakdown({ v }: { v: OppVariant }) {
  const minus = (x: number) => (x > 0 ? `−${usd(x, 3)}` : usd(0, 3));
  return (
    <div className="flex flex-col gap-2">
      <Line k="機会に置く" v={usd(v.split.pool, 0)} />
      {v.split.hedge_margin > 0 && <Line k={<Term k="保険の預け金">保険の預け金</Term>} v={usd(v.split.hedge_margin, 0)} note="Lighter に預ける（自動で計算）" />}
      <Line k="予備" v={usd(v.split.reserve, 0)} note="ガス代の分（チェーンごとの決まった額）" />
      <Line k="受け取る分" v={usd(v.income, 3)} note={`ボーナスの取り分（預けるだけの型は、預け先の利息も）${v.points ? "。ポイントは0として数えた" : ""}`} />
      {v.in_range_ratio != null && <Line k="幅の中にいる時間" v={`${(v.in_range_ratio * 100).toFixed(0)}%`} note={v.range_pct != null ? `幅 ±${v.range_pct.toFixed(1)}%` : undefined} />}
      {v.sigma_pct != null && <Line k="1日の値動き" v={`${v.sigma_pct.toFixed(2)}%`} note={v.range_pct != null ? "なめらかな動き。控えめは7日と30日の大きい方" : "控えめは7日と30日の大きい方"} />}
      {v.jumps_per_day != null && v.jumps_per_day > 0 && (
        <Line k="幅を飛び越える飛び" v={`1日 ${v.jumps_per_day.toFixed(2)}回`} note="値段が止まっていたあと（株の週末など）。そのたびに置き直しと損を数える" />
      )}
      {v.liquidity_share != null && (
        <Line k={<Term k="流動性の割合">流動性の取り分</Term>} v={sharePct(v.liquidity_share)}
          note={`幅 ±${(v.range_pct ?? 0).toFixed(1)}% に置いたとき（チェーンの記録）`} />
      )}
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
