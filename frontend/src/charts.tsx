import {
  Bar, BarChart, Cell, ComposedChart, Line, LineChart, ReferenceArea, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Bar as HourBar, HistoryPoint, RangeRow } from "./api";
import { jstHour, usd } from "./format";

// グラフも色は文字の濃淡だけ（オーナー依頼 7・14。緑と赤は判定の点だけに使う）
const GRID = "rgba(255,255,255,0.08)";
const TICK = { fill: "#7d889a", fontSize: 11 };
const TIP = {
  contentStyle: { background: "rgba(16,20,28,0.95)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 12, fontSize: 12 },
  labelStyle: { color: "#a1abbb" }, itemStyle: { color: "#f2f5f9" },
};
const POS = "#a1abbb";
const NEG = "#4b5563";
const US = "rgba(255,255,255,0.05)";

/** レンジ幅ごとの純日利（最適点だけ白）。 */
export function RangeNet({ rows, best }: { rows: RangeRow[]; best: number | null }) {
  const data = rows.map((r) => ({ r: `±${r.r_pct}%`, v: r.net_pct, best: r.r_pct === best }));
  return (
    <div className="h-44 w-full">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          <XAxis dataKey="r" tick={TICK} interval={0} axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}%`} width={44} domain={[(min: number) => Math.max(min, -10), "auto"]} allowDataOverflow />
          <Tooltip {...TIP} cursor={{ fill: "rgba(255,255,255,0.04)" }} formatter={(v: number) => [`${v.toFixed(2)}%`, "純日利（総資産あたり）"]} />
          <ReferenceLine y={0} stroke={GRID} />
          <Bar dataKey="v" isAnimationActive={false} radius={[4, 4, 4, 4]}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.best ? "#f2f5f9" : d.v >= 0 ? POS : NEG} />
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

// 0 の線がいつも見えるように、縦軸に 0 を含める
function yDomain(values: (number | null | undefined)[]): [number, number] {
  const v = values.filter((x): x is number => typeof x === "number" && isFinite(x));
  const lo = Math.min(0, ...v);
  const hi = Math.max(0, ...v);
  const pad = (hi - lo || 1) * 0.1;
  return [lo < 0 ? Math.floor(lo - pad) : 0, hi > 0 ? Math.ceil(hi + pad) : 0];
}

/** 1時間ごとの純損益の棒グラフ + 24時間移動平均 + 米国市場時間（7.7章 3.）。 */
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
          <Tooltip {...TIP} cursor={{ fill: "rgba(255,255,255,0.04)" }} formatter={(v: number, n: string) => [usd(v), n === "ma24_usd" ? "24時間平均" : "その1時間"]} />
          <ReferenceLine y={0} stroke={GRID} />
          <Bar dataKey="net_usd" isAnimationActive={false} radius={[3, 3, 3, 3]}>
            {data.map((d, i) => <Cell key={i} fill={(d.net_usd ?? 0) >= 0 ? POS : NEG} />)}
          </Bar>
          <Line dataKey="ma24_usd" stroke="#f2f5f9" dot={false} strokeWidth={1.5} isAnimationActive={false} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** ホームの「1時間ごとの損益」（細い棒だけの軽い形。0 の線を引き、米国市場の時間は下の細い帯で示す） */
export function HourStrip({ bars, height = 80 }: { bars: HourBar[]; height?: number }) {
  const vals = bars.map((b) => b.net_usd ?? 0);
  const max = Math.max(1e-9, ...vals.map(Math.abs));
  const hasNeg = vals.some((v) => v < 0);
  const hasPos = vals.some((v) => v > 0);
  const zero = !hasNeg ? height : !hasPos ? 0 : height * (Math.max(0, ...vals) / (Math.max(0, ...vals) - Math.min(0, ...vals)));
  const scale = !hasNeg || !hasPos ? height / max : height / (Math.max(0, ...vals) - Math.min(0, ...vals));
  const cols = { gridTemplateColumns: `repeat(${bars.length}, minmax(0, 1fr))` };
  return (
    <div className="flex flex-col gap-2">
      <div className="relative" style={{ height }}>
        <div className="absolute inset-x-0 h-px bg-white/10" style={{ top: Math.min(height - 1, zero) }} />
        <div className="absolute inset-0 grid" style={cols}>
          {bars.map((b) => {
            const v = b.net_usd;
            const h = v == null ? 0 : Math.max(2, Math.abs(v) * scale);
            return (
              <div key={b.ts} title={`${b.jst} ${v == null ? "記録なし" : usd(v)}`} className="relative">
                {v != null && (
                  <div className="absolute left-1/2 w-3/5 max-w-2 -translate-x-1/2 rounded-sm"
                    style={{ height: h, background: v >= 0 ? "var(--sec)" : "#4b5563", ...(v >= 0 ? { bottom: height - zero } : { top: zero }) }} />
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div className="grid h-1 overflow-hidden rounded-full bg-white/[0.04]" style={cols}>
        {bars.map((b) => <div key={b.ts} style={{ background: b.us_open ? "rgba(255,255,255,0.28)" : undefined }} />)}
      </div>
    </div>
  );
}

export function AssetsLine({ series, capital }: { series: { jst: string; value: number }[]; capital: number }) {
  if (!series.length) return <div className="cap">データなし</div>;
  return (
    <div className="h-32 w-full">
      <ResponsiveContainer>
        <LineChart data={series} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
          <XAxis dataKey="jst" tick={TICK} interval="preserveStartEnd" axisLine={{ stroke: GRID }} tickLine={false} />
          <YAxis tick={TICK} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={52} tickFormatter={(v) => `$${Math.round(v)}`} />
          <Tooltip {...TIP} formatter={(v: number) => [usd(v), "評価額"]} />
          <ReferenceLine y={capital} stroke="#7d889a" strokeDasharray="3 3" />
          <Line dataKey="value" stroke="#f2f5f9" dot={false} strokeWidth={1.75} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** 時系列の小さなグラフ（株ペアは米国市場時間を背景色で）。 */
export function Series({ history, field, fmt, stock }: {
  history: HistoryPoint[]; field: keyof HistoryPoint; fmt: (v: number) => string; stock: boolean;
}) {
  const data = history.map((h) => ({ x: jstHour(h.ts), v: h[field] as number | null, us_open: h.us_open }));
  if (!data.some((d) => d.v != null)) return <div className="cap">データなし（次の計算から記録します）</div>;
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
          <Line dataKey="v" stroke="#a1abbb" dot={false} strokeWidth={1.6} isAnimationActive={false} connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
