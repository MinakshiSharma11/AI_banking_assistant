import { useState } from "react";
import { api, type Cell, type Turn } from "../api";
import { dirOf, formatValue, titleCase } from "../format";
import AutoChart from "./AutoChart";

const PAGE = 50;

function ResultTable({ turn }: { turn: Turn }) {
  const [all, setAll] = useState(false);
  const r = turn.result!;
  const rows = all ? r.rows : r.rows.slice(0, PAGE);
  const lastCol = r.columns.length - 1;
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-100 text-left text-xs uppercase tracking-wide text-slate-600">
          <tr>{r.columns.map((c) => <th key={c} className="px-3 py-2 font-medium">{titleCase(c)}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row, i) => (
            <tr key={i} className="hover:bg-slate-50">
              {row.map((cell, j) => (
                <td key={j} className={`px-3 py-1.5 ${typeof cell === "number" ? "text-right tabular-nums" : ""}`}>
                  {formatValue(cell, j === lastCol ? r.unit : "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {(r.rows.length > PAGE || r.truncated) && (
        <div className="flex items-center justify-between bg-slate-50 px-3 py-1.5 text-xs text-slate-500">
          <span>{r.row_count.toLocaleString()} rows{r.truncated ? " (display limit reached)" : ""}</span>
          {r.rows.length > PAGE && (
            <button className="font-medium text-teal-700 hover:underline" onClick={() => setAll(!all)}>
              {all ? "Show fewer" : `Show all ${r.rows.length}`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Totals({ turn }: { turn: Turn }) {
  const r = turn.result!;
  const cols = r.totals_columns ?? [];
  const last = cols.length - 1;
  return (
    <div className="rounded-lg border border-teal-100 bg-teal-50/60 px-3 py-2">
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-teal-800">Total for the period</div>
      <div className="flex flex-wrap gap-x-6 gap-y-1">
        {r.totals!.map((row, i) => (
          <div key={i} className="text-sm">
            {last > 0 && <span className="text-slate-600">{row.slice(0, last).join(" / ")}: </span>}
            <span className="font-semibold tabular-nums text-slate-900">{formatValue(row[last], r.unit)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Downloads({ messageId }: { messageId: string }) {
  const [busy, setBusy] = useState<"xlsx" | "pdf" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const get = async (f: "xlsx" | "pdf") => {
    setBusy(f);
    setErr(null);
    try {
      await api.download(messageId, f);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Download failed");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="mt-2 flex items-center gap-2 text-xs">
      <span className="text-slate-500">Download:</span>
      {(["xlsx", "pdf"] as const).map((f) => (
        <button key={f} disabled={busy !== null} onClick={() => get(f)}
          className="rounded border border-slate-300 px-2 py-0.5 font-medium text-slate-700 hover:border-teal-600 hover:text-teal-700 disabled:opacity-50">
          {busy === f ? "Preparing…" : f === "xlsx" ? "Excel" : "PDF"}
        </button>
      ))}
      {err && <span className="text-red-600">{err}</span>}
    </div>
  );
}

const PARAM_LABELS: Record<string, string> = {
  t_from: "Period from (inclusive)",
  t_to: "Period to (exclusive)",
  user_branches: "Your branches (row security)",
};

function showParam(v: Cell | Cell[]): string {
  return Array.isArray(v) ? v.map(String).join(", ") : v === null ? "NULL" : String(v);
}

function SqlView({ turn }: { turn: Turn }) {
  const [filled, setFilled] = useState(false);
  const [copied, setCopied] = useState(false);
  const params = Object.entries(turn.sqlParams ?? {});
  const text = filled && turn.sqlWithValues ? turn.sqlWithValues : turn.sql!;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked: the text can still be selected */
    }
  };
  return (
    <div className="mt-2 space-y-2">
      {params.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <div className="bg-slate-100 px-3 py-1 font-medium text-slate-600">Values used</div>
          <table className="min-w-full">
            <tbody className="divide-y divide-slate-100">
              {params.map(([k, v]) => (
                <tr key={k}>
                  <td className="whitespace-nowrap px-3 py-1 font-mono text-slate-500">:{k}</td>
                  <td className="whitespace-nowrap px-3 py-1 text-slate-600">{PARAM_LABELS[k] ?? ""}</td>
                  <td className="px-3 py-1 font-mono text-slate-900">{showParam(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex items-center gap-3">
        {turn.sqlWithValues && (
          <label className="flex cursor-pointer items-center gap-1 text-slate-600">
            <input type="checkbox" checked={filled} onChange={(e) => setFilled(e.target.checked)} />
            Show with values filled in
          </label>
        )}
        <button onClick={copy} className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-slate-600 hover:border-teal-600 hover:text-teal-700">
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-lg bg-slate-900 p-3 text-[12px] leading-snug text-slate-100">{text}</pre>
      {filled && (
        <p className="text-slate-500">Reading copy. The query itself runs with bind parameters, as shown when this is unticked.</p>
      )}
    </div>
  );
}

const STAGES = ["understanding", "querying", "answering"];

export default function ResultCard({ turn, onOption, onFeedback }: {
  turn: Turn;
  onOption: (text: string) => void;
  onFeedback: (rating: 1 | -1) => void;
}) {
  const busy = !turn.done && !turn.error;
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div dir={dirOf(turn.question)} className="max-w-[80%] rounded-2xl rounded-br-sm bg-teal-700 px-4 py-2 text-white">
          {turn.question}
        </div>
      </div>

      <div className="rounded-2xl rounded-bl-sm border border-slate-200 bg-white p-4 shadow-sm">
        {busy && (
          <div className="flex items-center gap-3 text-sm text-slate-500">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-teal-600 border-t-transparent" />
            {STAGES.map((s) => (
              <span key={s} className={s === turn.stage ? "font-medium text-teal-700" : STAGES.indexOf(s) < STAGES.indexOf(turn.stage ?? "") ? "text-slate-400 line-through" : "text-slate-300"}>
                {titleCase(s)}
              </span>
            ))}
          </div>
        )}

        {turn.error && (
          <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{turn.error.message}</div>
        )}

        {turn.clarify && (
          <div dir={dirOf(turn.clarify.question)}>
            <p className="mb-2 text-slate-800">{turn.clarify.question}</p>
            <div className="flex flex-wrap gap-2">
              {turn.clarify.options.map((o) => (
                <button key={o} onClick={() => onOption(o)}
                  className="rounded-full border border-teal-600 px-3 py-1 text-sm text-teal-700 hover:bg-teal-50">
                  {o}
                </button>
              ))}
            </div>
          </div>
        )}

        {turn.answer && (
          <p dir={dirOf(turn.answer)} className="text-[15px] leading-relaxed text-slate-900">{turn.answer}</p>
        )}

        {turn.result?.totals && turn.result.totals.length > 0 && (
          <div className="mt-3"><Totals turn={turn} /></div>
        )}

        {turn.result && turn.chart && turn.result.rows.length > 0 && (
          <div className="mt-3"><AutoChart chart={turn.chart} result={turn.result} /></div>
        )}

        {turn.result && turn.result.rows.length > 0 && turn.chart?.type !== "kpi" && (
          <div className="mt-3"><ResultTable turn={turn} /></div>
        )}

        {turn.done?.status === "ok" && turn.result && turn.result.rows.length > 0 && (
          <Downloads messageId={turn.done.message_id} />
        )}

        {turn.definition && (
          <p className="mt-3 border-t border-slate-100 pt-2 text-xs text-slate-500">
            <span className="font-medium text-slate-600">Definition used: </span>{turn.definition}
          </p>
        )}

        {turn.sql && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer select-none font-medium text-slate-600 hover:text-teal-700">Show SQL</summary>
            <SqlView turn={turn} />
          </details>
        )}

        {turn.done && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-600">
              {turn.done.provider} · {turn.done.model}
            </span>
            <span>{(turn.done.latency_ms / 1000).toFixed(1)} s</span>
            <span className="rounded bg-slate-100 px-2 py-0.5">{turn.done.route}</span>
            {turn.done.grounded === false && (
              <span className="rounded bg-amber-50 px-2 py-0.5 text-amber-700" title="The model's wording did not match the data, so a templated answer is shown">
                templated answer
              </span>
            )}
            {turn.done.route !== "clarify" && (
              <span className="ml-auto flex gap-1">
                {([1, -1] as const).map((r) => (
                  <button key={r} disabled={turn.feedback !== undefined} onClick={() => onFeedback(r)}
                    aria-label={r === 1 ? "Helpful" : "Not helpful"}
                    className={`rounded px-2 py-0.5 text-sm ${turn.feedback === r ? "bg-teal-100 text-teal-800" : "hover:bg-slate-100"} disabled:cursor-default`}>
                    {r === 1 ? "👍" : "👎"}
                  </button>
                ))}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
