import { Children, isValidElement, useEffect, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type HandbookDoc } from "../api";

// The developer handbook (docs/HANDBOOK.md), served by GET /handbook with diagrams inlined.

function slug(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}\s-]/gu, "").trim().replace(/\s+/g, "-");
}

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

interface TocItem { level: 2 | 3; text: string; id: string }

function tableOfContents(markdown: string): TocItem[] {
  const items: TocItem[] = [];
  let fenced = false;
  for (const line of markdown.split("\n")) {
    if (line.startsWith("```")) fenced = !fenced;
    if (fenced) continue;
    const m = /^(##|###) (.+)$/.exec(line);
    if (m) {
      const text = m[2].replace(/[`*_]/g, "").replace(/\\/g, "");
      items.push({ level: m[1].length as 2 | 3, text, id: slug(text) });
    }
  }
  return items;
}

// Inline diagrams arrive as data: URIs, which react-markdown strips by default.
const urlTransform = (url: string) =>
  url.startsWith("data:image/png;base64,") ? url : defaultUrlTransform(url);

const heading = (Tag: "h1" | "h2" | "h3" | "h4", cls: string) =>
  function H({ children }: { children?: ReactNode }) {
    return <Tag id={slug(textOf(children))} className={`scroll-mt-4 ${cls}`}>{children}</Tag>;
  };

const components: Components = {
  h1: heading("h1", "mb-2 text-2xl font-semibold text-slate-900"),
  h2: heading("h2", "mt-10 border-b border-slate-200 pb-1.5 text-xl font-semibold text-slate-900 print:break-before-page"),
  h3: heading("h3", "mt-7 text-lg font-semibold text-slate-900"),
  h4: heading("h4", "mt-5 font-semibold text-slate-900"),
  p: ({ children }) => <p className="my-3 leading-relaxed text-slate-800">{children}</p>,
  a: ({ href, children }) => (
    <a href={href} className="text-teal-700 underline" target={href?.startsWith("#") ? undefined : "_blank"} rel="noreferrer">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="my-3 list-disc space-y-1 pl-6 text-slate-800">{children}</ul>,
  ol: ({ children }) => <ol className="my-3 list-decimal space-y-1 pl-6 text-slate-800">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  table: ({ children }) => (
    <div className="my-4 overflow-x-auto rounded-lg border border-slate-200 print:overflow-visible">
      <table className="min-w-full text-[13px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-slate-100 text-left text-slate-700">{children}</thead>,
  th: ({ children }) => <th className="border-b border-slate-200 px-3 py-2 align-top font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-t border-slate-100 px-3 py-2 align-top text-slate-800">{children}</td>,
  pre: ({ children }) => (
    <pre className="my-4 overflow-x-auto rounded-lg bg-slate-900 p-3 text-[12.5px] leading-snug text-slate-100 print:whitespace-pre-wrap">
      {children}
    </pre>
  ),
  code: ({ className, children }) => {
    const block = Children.toArray(children).some((c) => typeof c === "string" && c.includes("\n")) || !!className;
    return block
      ? <code className={className}>{children}</code>
      : <code className="rounded bg-slate-100 px-1 py-0.5 text-[0.9em] text-slate-900">{children}</code>;
  },
  img: ({ src, alt }) => (
    <img src={src} alt={alt ?? ""} className="my-4 w-full rounded-lg border border-slate-200 print:break-inside-avoid" />
  ),
  blockquote: ({ children }) => <blockquote className="my-3 border-l-4 border-slate-200 pl-4 text-slate-600">{children}</blockquote>,
  input: ({ checked }) => <input type="checkbox" checked={!!checked} readOnly className="mr-2 align-middle" />,
};

export default function Handbook() {
  const [doc, setDoc] = useState<HandbookDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"md" | "html" | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    api.handbook().then(setDoc).catch((e: Error) => setError(e.message));
  }, []);

  const toc = useMemo(() => (doc ? tableOfContents(doc.markdown) : []), [doc]);
  const shown = filter ? toc.filter((t) => t.text.toLowerCase().includes(filter.toLowerCase())) : toc;

  const download = async (format: "md" | "html") => {
    setBusy(format);
    try {
      await api.downloadHandbook(format);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setBusy(null);
    }
  };

  if (error) return <div className="m-6 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">Handbook: {error}</div>;
  if (!doc) return <div className="m-6 text-sm text-slate-500">Loading the handbook…</div>;

  return (
    <div className="flex min-h-0 flex-1 print:block">
      <nav className="hidden w-72 shrink-0 overflow-y-auto border-r border-slate-200 bg-white p-4 lg:block print:hidden">
        <div className="mb-3 space-y-2">
          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Download</div>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => download("md")} disabled={busy !== null}
              className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-teal-600 hover:text-teal-700 disabled:opacity-50">
              {busy === "md" ? "Preparing…" : "Markdown"}
            </button>
            <button onClick={() => download("html")} disabled={busy !== null}
              className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-teal-600 hover:text-teal-700 disabled:opacity-50">
              {busy === "html" ? "Preparing…" : "HTML"}
            </button>
            <button onClick={() => window.print()}
              className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-teal-600 hover:text-teal-700"
              title="Opens the print dialog: choose Save as PDF">
              PDF
            </button>
          </div>
          <div className="text-[11px] text-slate-500">Updated {doc.updated.replace("T", " ")}</div>
        </div>
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter sections…"
          className="mb-3 w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm outline-none focus:border-teal-600" />
        <ul className="space-y-0.5 text-sm">
          {shown.map((t) => (
            <li key={t.id + t.text}>
              <a href={`#${t.id}`}
                className={`block rounded px-2 py-1 hover:bg-slate-100 ${t.level === 2 ? "font-medium text-slate-800" : "pl-5 text-slate-600"}`}>
                {t.text}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <article className="min-w-0 flex-1 overflow-y-auto bg-white print:overflow-visible">
        <div className="mx-auto max-w-4xl px-6 py-8 print:max-w-none print:px-0">
          <div className="mb-4 flex flex-wrap gap-2 lg:hidden print:hidden">
            <button onClick={() => download("md")} className="rounded border border-slate-300 px-2.5 py-1 text-xs">Download Markdown</button>
            <button onClick={() => download("html")} className="rounded border border-slate-300 px-2.5 py-1 text-xs">Download HTML</button>
            <button onClick={() => window.print()} className="rounded border border-slate-300 px-2.5 py-1 text-xs">PDF</button>
          </div>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={urlTransform}>
            {doc.markdown.replace(/<!--[\s\S]*?-->/g, "")}
          </ReactMarkdown>
        </div>
      </article>
    </div>
  );
}
