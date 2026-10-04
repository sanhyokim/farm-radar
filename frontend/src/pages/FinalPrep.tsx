import { useApi } from "../api";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Pill } from "../ui";

// N5 最終判断の準備（2026-10-04 18:20 JST オーナーの指示書）。仮の数字10項目の 今の値・状態・根拠・記録量・変更案の準備状況。
// ここでは決めない。おすすめも出さない（案とおすすめは指示役が出す）。表の文字はサーバーが作る（final_prep.py）。

type Code = "ready" | "wait" | "lack";
type St = { code: Code; label: string; why: string };
type Tbl = { title: string; head: string[]; rows: string[][]; note: string | null; mark: number[] };
type Missing = { what: string; new_recording: boolean; past_public: boolean | null; how: string };
type Item = { no: number; key: string; title: string; now: string; state: St; basis: string[]; amount: string; prep: string; tables: Tbl[]; missing: Missing | null };

interface FinalPrepData {
  items: Item[];
  counts: Record<Code, number>;
  states: Record<Code, string>;
  rebalance: { state: St; table: Tbl; days: number };
  merkl: { state: St; table: Tbl; tables: Tbl[]; out_classes: Record<string, number>; pairs: number; days: number; missing: Missing | null };
  n6: { title: string; why: string }[];
  backtest_days: number; ready_on: string | null; backtest_present: boolean; computed_at: string | null;
  notes: string[];
}

const TONE = { ready: "g", wait: "n", lack: "y" } as const;
const ICON = { ready: "check", wait: "clock", lack: "alert" } as const;

const Badge = ({ s }: { s: St }) => <Pill tone={TONE[s.code]} icon={ICON[s.code]}>{s.label}</Pill>;

function Table({ t }: { t: Tbl }) {
  const wide = t.head.length > 4;
  return (
    <div className="flex flex-col gap-2">
      <span className="label">{t.title}</span>
      {t.rows.length === 0 ? <span className="cap">まだ数えられる記録がありません。</span> : (
        <div className="scroll-x">
          <table className="tbl" style={wide ? { minWidth: `${t.head.length * 92}px` } : undefined}>
            <thead><tr>{t.head.map((h, i) => <th key={h} className={i ? "r pl-3" : ""}>{h}</th>)}</tr></thead>
            <tbody>
              {t.rows.map((r, j) => (
                <tr key={j} style={t.mark.includes(j) ? { background: "rgba(96,165,250,0.08)" } : undefined}>
                  {r.map((c, i) => <td key={i} className={i ? "num r pl-3" : "sec"} style={i ? { whiteSpace: "nowrap" } : undefined}>{c}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {t.mark.length > 0 && <span className="cap">色のついた行が今の設定です。</span>}
      {t.note && <span className="cap">{t.note}</span>}
    </div>
  );
}

function MissingBox({ m }: { m: Missing }) {
  return (
    <div className="flex items-start gap-2 rounded-2xl p-4" style={{ background: "rgba(251,191,36,0.08)" }}>
      <Icon name="info" size={16} color="var(--y)" />
      <div className="flex flex-col gap-1">
        <span className="sec"><span className="bold">足りないもの: </span>{m.what}</span>
        <span className="cap">今から新しく記録を始める必要: {m.new_recording ? "あり" : "なし"}・過去の公開記録で足りるか: {m.past_public == null ? "分からない" : m.past_public ? "足りる見込み" : "足りない"}</span>
        <span className="cap">{m.how}</span>
      </div>
    </div>
  );
}

const Field = ({ k, v }: { k: string; v: string }) => (
  <div className="flex flex-col gap-1"><span className="label">{k}</span><span className="sec">{v}</span></div>
);

function ItemFold({ x }: { x: Item }) {
  return (
    <Fold title={<span className="flex items-center justify-between gap-2"><span>{x.no}. {x.title}</span><Badge s={x.state} /></span>}>
      <Field k="今の値" v={x.now} />
      <p className="cap">{x.state.why}</p>
      {x.missing && <MissingBox m={x.missing} />}
      {x.tables.map((t) => <Table key={t.title} t={t} />)}
      <div className="flex flex-col gap-1">
        <span className="label">根拠</span>
        <ul className="flex list-disc flex-col gap-1 pl-5 cap">{x.basis.map((b) => <li key={b}>{b}</li>)}</ul>
      </div>
      <Field k="記録量" v={x.amount} />
      <Field k="変更案の準備状況" v={x.prep} />
    </Fold>
  );
}

export default function FinalPrep() {
  const { data, error } = useApi<FinalPrepData>("/api/trial/final", 600_000);
  if (!data) return <Card title="N5 最終判断の準備"><Loading error={error} rows={2} /></Card>;
  return (
    <div className="flex flex-col gap-4 lg:gap-6">
      <Card title="N5 最終判断の準備">
        <div className="grid grid-cols-3 gap-2 text-center">
          {(["ready", "wait", "lack"] as const).map((c) => (
            <div key={c} className="inset flex flex-col items-center gap-1 p-4">
              <Pill tone={TONE[c]} icon={ICON[c]}>{data.states[c]}</Pill>
              <span className="t20 num">{data.counts[c] ?? 0}</span>
            </div>
          ))}
        </div>
        <Line k="さかのぼりの記録" v={`${data.backtest_days} 日`} note={data.ready_on ? `7日分そろうのは ${data.ready_on} ごろ（今の版の写しを取り直したとき）` : "まだありません"} />
        <ul className="flex list-disc flex-col gap-1 pl-5 cap">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </Card>
      <Folds>
        <div className="px-6 pt-6 pb-2"><span className="label">仮の数字（10項目）</span></div>
        {data.items.map((x) => <ItemFold key={x.key} x={x} />)}
      </Folds>
      <Folds>
        <div className="px-6 pt-6 pb-2"><span className="label">7日待ち・分母・N6 で決めるもの</span></div>
        <Fold title={<span className="flex items-center justify-between gap-2"><span>置き直しの式（7日で自動）</span><Badge s={data.rebalance.state} /></span>}>
          <p className="cap">{data.rebalance.state.why}</p>
          <Table t={data.rebalance.table} />
        </Fold>
        <Fold title={<span className="flex items-center justify-between gap-2"><span>Merkl の分母（A 全員 / B 幅の中だけ）</span><Badge s={data.merkl.state} /></span>}>
          <p className="cap">{data.merkl.state.why}</p>
          <Table t={data.merkl.table} />
          {Object.keys(data.merkl.out_classes ?? {}).length > 0 && (
            <p className="cap">幅の外の扱い: {Object.entries(data.merkl.out_classes).map(([k, v]) => `${k} ${v} 件`).join("・")}（「幅の外にも配る」「判断できない」は B を当てはめず、比べから外しています）</p>
          )}
          {(data.merkl.tables ?? []).map((t) => <Table key={t.title} t={t} />)}
          {data.merkl.missing && <MissingBox m={data.merkl.missing} />}
          <p className="cap">分母はここでは決めません。探すの見込みは A のままです。</p>
        </Fold>
        <Fold title="N6 で判断するもの">
          {data.n6.map((n) => <Line key={n.title} k={n.title} v={<Pill tone="n">N6で判断</Pill>} note={n.why} />)}
        </Fold>
      </Folds>
    </div>
  );
}
