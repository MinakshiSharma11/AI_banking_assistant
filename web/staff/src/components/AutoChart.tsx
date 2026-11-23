import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartEvent, ResultEvent } from "../api";
import { formatValue, titleCase } from "../format";

const PALETTE = ["#0f766e", "#2563eb", "#d97706", "#7c3aed", "#dc2626", "#0891b2", "#65a30d"];

/** Renders the chart type the server chose. The LLM never picks the chart. */
export default function AutoChart({ chart, result }: { chart: ChartEvent; result: ResultEvent }) {
  const { columns, rows, unit, label } = result;
  if (chart.type === "kpi") {
    return (
      <div className="rounded-xl border border-teal-100 bg-teal-50 px-5 py-4">
        <div className="text-xs font-medium uppercase tracking-wide text-teal-700">{label || titleCase(columns[0])}</div>
        <div className="mt-1 text-4xl font-semibold text-slate-900">
          {formatValue(rows[0][0], unit)}
          {unit && !["ratio", "KWD"].includes(unit) && <span className="ml-2 text-base font-normal text-slate-500">{unit}</span>}
          {unit === "KWD" && <span className="ml-2 text-base font-normal text-slate-500">KWD</span>}
        </div>
      </div>
    );
  }
  if (chart.type === "table" || !chart.x || !chart.y) return null;
  const xi = columns.indexOf(chart.x);
  const yi = columns.indexOf(chart.y);
  const tick = (v: number) => formatValue(v, unit);

  if (chart.type === "line") {
    const si = chart.series ? columns.indexOf(chart.series) : -1;
    const byX = new Map<string, Record<string, string | number>>();
    const series = new Set<string>();
    for (const r of rows) {
      const x = String(r[xi]);
      const key = si >= 0 ? String(r[si]) : label || "value";
      series.add(key);
      const point = byX.get(x) ?? { x };
      point[key] = Number(r[yi]);
      byX.set(x, point);
    }
    const data = [...byX.values()].sort((a, b) => String(a.x).localeCompare(String(b.x)));
    return (
      <div className="h-64">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} />
            <YAxis tickFormatter={tick} tick={{ fontSize: 11 }} width={70} />
            <Tooltip formatter={(v: number) => formatValue(v, unit)} />
            {series.size > 1 && <Legend />}
            {[...series].map((s, i) => (
              <Line key={s} dataKey={s} stroke={PALETTE[i % PALETTE.length]} strokeWidth={2} dot={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  }
  const data = rows.map((r) => ({ x: String(r[xi]), y: Number(r[yi]) }));
  return (
    <div className="h-64">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
          <XAxis dataKey="x" tick={{ fontSize: 11 }} interval={0} angle={data.length > 6 ? -30 : 0}
            textAnchor={data.length > 6 ? "end" : "middle"} height={data.length > 6 ? 70 : 30} />
          <YAxis tickFormatter={tick} tick={{ fontSize: 11 }} width={70} />
          <Tooltip formatter={(v: number) => formatValue(v, unit)} />
          <Bar dataKey="y" name={label || chart.y} fill={PALETTE[0]} radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
