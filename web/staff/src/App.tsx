import { useCallback, useEffect, useRef, useState } from "react";
import { api, chat, setToken, getToken, type Cell, type DemoUser, type Me, type Turn } from "./api";
import Handbook from "./components/Handbook";
import ResultCard from "./components/ResultCard";
import { ROLE_LABEL, dirOf } from "./format";

const STARTERS = [
  "Accounts opened by branch last quarter",
  "Active sessions right now",
  "Failed logins by reason this week",
  "Mobile vs web transaction value by month",
];

function Login({ onDone }: { onDone: () => void }) {
  const [users, setUsers] = useState<DemoUser[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api.users().then(setUsers).catch(() => setError("The gateway is not reachable. Is `make api` running?"));
  }, []);
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-lg">
        <h1 className="text-2xl font-semibold text-slate-900">Bank Data Assistant</h1>
        <p className="mt-1 text-sm text-slate-500">Mock SSO: choose a demo staff user. Their role and branches decide what they can see.</p>
        {error && <p className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        <div className="mt-6 grid gap-3">
          {users.map((u) => (
            <button key={u.username}
              onClick={() => api.login(u.username).then(onDone).catch((e) => setError(e.message))}
              className="rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-teal-500 hover:shadow">
              <div className="font-medium text-slate-900">{u.display_name}</div>
              <div className="mt-0.5 text-xs text-slate-500">
                {u.username} · {ROLE_LABEL[u.role] ?? u.role} · {u.branches.length === 12 ? "all branches" : `branches ${u.branches.join(", ")}`}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [view, setView] = useState<"chat" | "handbook">("chat");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  const loadMe = useCallback(() => {
    if (!getToken()) {
      setChecked(true);
      return;
    }
    api.me().then(setMe).catch(() => setToken(null)).finally(() => setChecked(true));
  }, []);
  useEffect(loadMe, [loadMe]);
  // Braces matter: newer browsers return a Promise from scrollIntoView, and React would
  // call a returned value as the effect's cleanup ("n is not a function", blank page).
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const patch = (id: string, change: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...change } : t)));

  const send = async (text: string) => {
    const question = text.trim();
    if (!question || sending) return;
    const id = crypto.randomUUID();
    setTurns((ts) => [...ts, { id, question }]);
    setInput("");
    setSending(true);
    try {
      await chat(question, conversationId, (type, data) => {
        const d = data as never;
        switch (type) {
          case "conversation": setConversationId((data as { conversation_id: string }).conversation_id); break;
          case "status": patch(id, { stage: (data as { stage: string }).stage }); break;
          case "clarify": patch(id, { clarify: d }); break;
          case "result": patch(id, { result: d }); break;
          case "chart": patch(id, { chart: d }); break;
          case "answer_delta": setTurns((ts) => ts.map((t) => t.id === id ? { ...t, answer: (t.answer ?? "") + (data as { text: string }).text } : t)); break;
          case "definition": patch(id, { definition: (data as { text: string }).text }); break;
          case "sql": {
            const d = data as { text: string; params?: Record<string, Cell | Cell[]>; text_with_values?: string };
            patch(id, { sql: d.text, sqlParams: d.params, sqlWithValues: d.text_with_values });
            break;
          }
          case "done": patch(id, { done: d }); break;
          case "error": patch(id, { error: d }); break;
        }
      });
    } catch {
      patch(id, { error: { code: "network", message: "Connection to the gateway was lost." } });
    } finally {
      setSending(false);
    }
  };

  const feedback = (turn: Turn, rating: 1 | -1) => {
    if (!turn.done) return;
    patch(turn.id, { feedback: rating });
    api.feedback(turn.done.message_id, rating).catch(() => patch(turn.id, { feedback: undefined }));
  };

  if (!checked) return null;
  if (!me) return <Login onDone={loadMe} />;

  return (
    <div className="flex h-full flex-col print:block print:h-auto">
      <header className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-5 py-3 print:hidden">
        <div className="font-semibold text-slate-900">Bank Data Assistant</div>
        <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
          {me.display_name} · {ROLE_LABEL[me.role] ?? me.role}
        </span>
        <span className="text-xs text-slate-500">
          {me.branches.length === 12 ? "All branches" : `Branches ${me.branches.join(", ")}`}
        </span>
        <span className="ml-auto rounded bg-teal-50 px-2 py-0.5 text-xs font-medium text-teal-800" title="LLM answering questions">
          {me.llm.provider} · {me.llm.model}
        </span>
        <button className={`text-xs hover:text-teal-700 ${view === "handbook" ? "font-semibold text-teal-700" : "text-slate-600"}`}
          onClick={() => setView(view === "handbook" ? "chat" : "handbook")}>
          {view === "handbook" ? "Back to chat" : "Handbook"}
        </button>
        <button className="text-xs text-slate-600 hover:text-teal-700" onClick={() => { setTurns([]); setConversationId(null); setView("chat"); }}>
          New chat
        </button>
        <button className="text-xs text-slate-600 hover:text-red-600" onClick={() => { setToken(null); setMe(null); setTurns([]); setConversationId(null); }}>
          Sign out
        </button>
      </header>

      {view === "handbook" ? <Handbook /> : <>
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-6 px-4 py-6">
          {turns.length === 0 && (
            <div className="pt-10 text-center">
              <p className="text-slate-600">Ask about accounts, transactions, digital banking or Atumverse workflows, in English or Arabic.</p>
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {STARTERS.map((s) => (
                  <button key={s} onClick={() => send(s)}
                    className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:border-teal-500 hover:text-teal-700">
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {turns.map((t) => (
            <ResultCard key={t.id} turn={t} onOption={send} onFeedback={(r) => feedback(t, r)} />
          ))}
          <div ref={bottom} />
        </div>
      </main>

      <form className="border-t border-slate-200 bg-white px-4 py-3" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <div className="mx-auto flex max-w-3xl gap-2">
          <input value={input} onChange={(e) => setInput(e.target.value)} dir={dirOf(input)} maxLength={2000}
            placeholder="Ask a question… / اطرح سؤالاً…"
            className="flex-1 rounded-xl border border-slate-300 px-4 py-2.5 outline-none focus:border-teal-600 focus:ring-2 focus:ring-teal-100" />
          <button type="submit" disabled={sending || !input.trim()}
            className="rounded-xl bg-teal-700 px-5 py-2.5 font-medium text-white hover:bg-teal-800 disabled:opacity-40">
            Ask
          </button>
        </div>
      </form>
      </>}
    </div>
  );
}
