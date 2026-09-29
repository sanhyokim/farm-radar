import {
  Area, AreaChart, Bar, BarChart, Cell, ComposedChart, Line, LineChart, ReferenceArea, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Bar as HourBar, Breakdown, HistoryPoint, Point, RangeRow } from "./api";
import { jstHour, usd } from "./format";

const GRID = "#334155";
const TICK = { fill: "#94a3b8", fontSize: 10 };
const TIP = { contentStyle: { background: "#0f172a", border: "1px solid #334155", fontSize: 12 }, labelStyle: { color: "#cbd5e1" } };
const POS = "#34d399";
const NEG = "#fb7185";
const US = "rgba(56,189,248,0.10)";

export function Sparkline({ data, color = "#38bdf8" }: { data: Point[]; color?: string }) {
  if (!data.length) return <div className="h-10 text-xs text-slate-500">データなし</div>;
  return (
    <div className="h-10 w-full">
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 2, bottom: 2, left: 0, right: 0 }}>
          <YAxis hide domain={["auto", "auto"]} />
          <Area type="monotone" dataKey="v" stroke={color} fill={color} fillOpacity={0.12} strokeWidth={1.5} dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

const ORDER: [keyof Breakdown, string][] = [
  ["income", "収入"], ["gamma", "ガンマ"], ["other", "その他"], ["hedge", "ヘッジ"],
  ["haircut", "値下がり"], ["direction", "方向"],
];

/** 損益の分解（ウォーターフォール図）: 収入から各項目を足し引きして純損益へ。 */
export function Waterfall({ b }: { b: Breakdown }) {
  let run = 0;
  const rows = ORDER.map(([k, name]) => {
    const v = b[k] as number;
    const start = run;
    run += v;
    return { name, base: Math.min(start, run), size: Math.abs(v), v };
  });
  rows.push({ name: "純損益", base: Math.min(0, b.net), size: Math.abs(b.net), v: b.net });
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer>
        <BarChart data={rows} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          <XAxis dataKey="name" tick={TICK} interval={0} axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} width={44} />
          <Tooltip {...TIP} formatter={(_v, _n, p) => [usd(p.payload.v), "金額（1日）"]} />
          <ReferenceLine y={0} stroke={GRID} />
          <Bar dataKey="base" stackId="a" fill="transparent" isAnimationActive={false} />
          <Bar dataKey="size" stackId="a" isAnimationActive={false}>
            {rows.map((r, i) => (
              <Cell key={i} fill={i === rows.length - 1 ? (r.v >= 0 ? "#10b981" : "#e11d48") : r.v >= 0 ? POS : NEG} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** レンジ幅ごとの純日利（最適点を強調）。 */
export function RangeNet({ rows, best }: { rows: RangeRow[]; best: number | null }) {
  const data = rows.map((r) => ({ r: `±${r.r_pct}%`, v: r.net_pct, best: r.r_pct === best }));
  return (
    <div className="h-44 w-full">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          <XAxis dataKey="r" tick={TICK} interval={0} axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}%`} width={44} domain={[(min: number) => Math.max(min, -10), "auto"]} allowDataOverflow />
          <Tooltip {...TIP} formatter={(v: number) => [`${v.toFixed(2)}%`, "純日利（総資産あたり）"]} />
          <ReferenceLine y={0} stroke={GRID} />
          <Bar dataKey="v" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.best ? "#fbbf24" : d.v >= 0 ? "#475569" : "#7f1d1d"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** 米国市場が開いている時間を背景色にする（連続した時間をまとめる）。 */
function usAreas(points: { x: string; us_open: boolean }[]) {
  const out: { x1: string; x2: string }[] = [];
  let start: string | null = null;
  points.forEach((p, i) => {
    if (p.us_open && start == null) start = p.x;
    const next = points[i + 1];
    if (p.us_open && (!next || !next.us_open) && start != null) {
      out.push({ x1: start, x2: p.x });
      start = null;
    }
  });
  return out;
}

/** 1時間ごとの純損益（予測）の棒グラフ + 24時間移動平均 + 米国市場時間（7.7章 3.）。 */
// 0 の線がいつも見えるように、縦軸に 0 を含める
function yDomain(values: (number | null | undefined)[]): [number, number] {
  const v = values.filter((x): x is number => typeof x === "number" && isFinite(x));
  const lo = Math.min(0, ...v);
  const hi = Math.max(0, ...v);
  const pad = (hi - lo || 1) * 0.1;
  return [lo < 0 ? Math.floor(lo - pad) : 0, hi > 0 ? Math.ceil(hi + pad) : 0];
}

export function HourlyBars({ bars }: { bars: HourBar[] }) {
  const data = bars.map((b) => ({ ...b, x: b.jst }));
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          {usAreas(data).map((a, i) => (
            <ReferenceArea key={i} x1={a.x1} x2={a.x2} fill={US} strokeOpacity={0} ifOverflow="extendDomain" />
          ))}
          <XAxis dataKey="x" tick={TICK} interval={11} axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} width={44}
            domain={yDomain(data.flatMap((d) => [d.net_usd, d.ma24_usd]))} />
          <Tooltip {...TIP} formatter={(v: number, n: string) => [usd(v), n === "ma24_usd" ? "24時間平均" : "その1時間"]} />
          <ReferenceLine y={0} stroke={GRID} />
          <Bar dataKey="net_usd" isAnimationActive={false}>
            {data.map((d, i) => <Cell key={i} fill={(d.net_usd ?? 0) >= 0 ? POS : NEG} />)}
          </Bar>
          <Line dataKey="ma24_usd" stroke="#fbbf24" dot={false} strokeWidth={1.5} isAnimationActive={false} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export function AssetsLine({ series, capital }: { series: { jst: string; value: number }[]; capital: number }) {
  if (!series.length) return <div className="text-xs text-slate-500">データなし</div>;
  return (
    <div className="h-32 w-full">
      <ResponsiveContainer>
        <LineChart data={series} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          <XAxis dataKey="jst" tick={TICK} interval="preserveStartEnd" axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={52} tickFormatter={(v) => `$${Math.round(v)}`} />
          <Tooltip {...TIP} formatter={(v: number) => [usd(v), "総資産（推定）"]} />
          <ReferenceLine y={capital} stroke="#64748b" strokeDasharray="3 3" />
          <Line dataKey="value" stroke="#38bdf8" dot={false} strokeWidth={1.8} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** 時系列の小さなグラフ（株ペアは米国市場時間を背景色で）。 */
export function Series({ history, field, fmt, stock, color = "#38bdf8" }: {
  history: HistoryPoint[]; field: keyof HistoryPoint; fmt: (v: number) => string; stock: boolean; color?: string;
}) {
  const data = history.map((h) => ({ x: jstHour(h.ts), v: h[field] as number | null, us_open: h.us_open }));
  if (!data.some((d) => d.v != null)) return <div className="text-xs text-slate-500">データなし（次の計算から記録します）</div>;
  return (
    <div className="h-28 w-full">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 6, right: 4, left: -8, bottom: 0 }}>
          {stock && usAreas(data).map((a, i) => (
            <ReferenceArea key={i} x1={a.x1} x2={a.x2} fill={US} strokeOpacity={0} ifOverflow="extendDomain" />
          ))}
          <XAxis dataKey="x" tick={TICK} interval="preserveStartEnd" axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={52} tickFormatter={fmt} />
          <Tooltip {...TIP} formatter={(v: number) => [fmt(v), ""]} />
          <Line dataKey="v" stroke={color} dot={false} strokeWidth={1.6} isAnimationActive={false} connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
