// Gateway client. All calls go through /api (proxied by Vite to the gateway).

export type Cell = string | number | boolean | null;

export interface DemoUser {
  username: string;
  display_name: string;
  role: string;
  branches: string[];
}

export interface Me extends DemoUser {
  llm: { provider: string; model: string };
}

export interface ResultEvent {
  columns: string[];
  rows: Cell[][];
  row_count: number;
  truncated: boolean;
  unit: string;
  label: string;
  /** Per-series totals over all periods of a trend (computed by the server). */
  totals?: Cell[][];
  totals_columns?: string[];
}

export interface HandbookDoc {
  title: string;
  updated: string;
  markdown: string;
}

export interface ChartEvent {
  type: "kpi" | "line" | "bar" | "table";
  x?: string;
  y?: string;
  series?: string | null;
}

export interface DoneEvent {
  message_id: string;
  route: string;
  status: string;
  provider: string;
  model: string;
  latency_ms: number;
  grounded: boolean | null;
}

export interface Turn {
  id: string;
  question: string;
  stage?: string;
  clarify?: { question: string; options: string[] };
  result?: ResultEvent;
  chart?: ChartEvent;
  answer?: string;
  definition?: string;
  sql?: string;
  /** Bind values the SQL ran with, and a reading copy with them filled in. */
  sqlParams?: Record<string, Cell | Cell[]>;
  sqlWithValues?: string;
  done?: DoneEvent;
  error?: { code: string; message: string };
  feedback?: 1 | -1;
}

const TOKEN_KEY = "bcai.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the session just won't survive a reload */
  }
}

/** Fetch with the bearer token and hand the response to the browser as a download. */
async function saveFile(path: string, fallbackName: string): Promise<void> {
  const token = getToken();
  const res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? body.message ?? res.statusText);
  }
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.message ?? res.statusText), { status: res.status });
  }
  return res.json() as Promise<T>;
}

export const api = {
  users: () => call<DemoUser[]>("/auth/users"),
  login: async (username: string) => {
    const r = await call<{ access_token: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username }),
    });
    setToken(r.access_token);
  },
  me: () => call<Me>("/me"),
  /** Download the shown result as Excel or PDF (built and audited by the gateway). */
  download: (message_id: string, format: "xlsx" | "pdf") =>
    saveFile(`/api/export/${message_id}?format=${format}`, `data-export.${format}`),
  /** The developer handbook (docs/HANDBOOK.md, diagrams inlined). */
  handbook: () => call<HandbookDoc>("/handbook"),
  downloadHandbook: (format: "md" | "html") =>
    saveFile(`/api/handbook/download?format=${format}`, `bank-data-agent-handbook.${format}`),
  feedback: (message_id: string, rating: 1 | -1) =>
    call<{ ok: boolean }>("/feedback", { method: "POST", body: JSON.stringify({ message_id, rating }) }),
};

/** POST /chat and parse the SSE stream (EventSource cannot POST). */
export async function chat(
  message: string,
  conversationId: string | null,
  onEvent: (type: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const token = getToken();
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token ?? ""}` },
    body: JSON.stringify({ message, ...(conversationId ? { conversation_id: conversationId } : {}) }),
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    onEvent("error", { code: body.code ?? String(res.status), message: body.message ?? res.statusText });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let type = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(type, JSON.parse(data));
    }
  }
}
