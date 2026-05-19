import { useCallback, useEffect, useState } from "react";

const API_BASE = (import.meta.env.VITE_GATEWAY_URL as string) || "http://localhost:8000";

// ────────────────────────────────────────────────────────────────────────────
// Types — mirror the FastAPI response shapes. Keep flat; no codegen for now.
// ────────────────────────────────────────────────────────────────────────────

type AgentStatus = "complete" | "awaiting_caregiver";

interface ToolCall { name: string; args: Record<string, unknown>; }
interface SerialMessage { type: string; content: string | null; name?: string; tool_call_id?: string; }
interface PendingQuestion { question: string; context: Record<string, unknown>; }

interface AgentTrace {
  status: AgentStatus;
  request_id: string;
  thread_id: string;
  final_message: string | null;
  awaiting: PendingQuestion | null;
  tool_calls: ToolCall[];
  messages: SerialMessage[];
}

interface AuditRow {
  id: string;
  action: string;
  actor: string;
  payload: Record<string, unknown>;
  latency_ms: number | null;
  cost_usd: number | null;
  created_at: string;
}

interface DraftRow {
  id: string;
  theme: string;
  content: Record<string, unknown>;
  source_transcript: string;
  status: string;
  validator_confidence: number | null;
  created_at: string;
}

interface FlagRow { id: string; reason: string; severity: string; resolved: boolean; created_at: string; }
interface FollowupRow { id: string; action: string; due_at: string; status: string; created_at: string; }

interface AuditDetail {
  request_id: string;
  audit: AuditRow[];
  drafts: DraftRow[];
  flags: FlagRow[];
  followups: FollowupRow[];
}

interface Resident { id: string; full_name: string; room_number: string; date_of_birth: string; }

interface StoredRun {
  thread_id: string;
  request_id: string;
  transcript: string;
  status: AgentStatus;
  created_at: string;
}

const STORAGE_KEY = "dexter-mini.runs";
const EXAMPLES: { label: string; text: string }[] = [
  { label: "Vitals + nutrition",   text: "Margarethe Müller in room 12. BP 130 over 82, pulse 72. Ate breakfast 100%." },
  { label: "Ambiguous name",       text: "Just finished with Müller. BP 128 over 78. Walker stable." },
  { label: "Compound w/ mobility", text: "Müller, room 12. BP 132/85, pulse 74. Ate lunch 80%. Walked to the window with walker, no falls." },
  { label: "Incident",             text: "Müller had a small fall in the bathroom. No injury but flag for review please." },
  { label: "Lookup (no docs)",     text: "Who is in room 14?" },
];

// ────────────────────────────────────────────────────────────────────────────
// API
// ────────────────────────────────────────────────────────────────────────────

async function postJSON<T>(path: string, body: unknown, requestId?: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(requestId ? { "X-Request-Id": requestId } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

// ────────────────────────────────────────────────────────────────────────────
// Hooks
// ────────────────────────────────────────────────────────────────────────────

function useStoredRuns(): [StoredRun[], (r: StoredRun) => void, () => void] {
  const [runs, setRuns] = useState<StoredRun[]>(() => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); }
    catch { return []; }
  });
  const upsert = useCallback((run: StoredRun) => {
    setRuns(prev => {
      const without = prev.filter(r => r.thread_id !== run.thread_id);
      const next = [run, ...without].slice(0, 20);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }, []);
  const clear = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setRuns([]);
  }, []);
  return [runs, upsert, clear];
}

// ────────────────────────────────────────────────────────────────────────────
// App
// ────────────────────────────────────────────────────────────────────────────

export default function App() {
  const [transcript, setTranscript] = useState("");
  const [trace, setTrace] = useState<AgentTrace | null>(null);
  const [audit, setAudit] = useState<AuditDetail | null>(null);
  const [reply, setReply] = useState("");
  const [residents, setResidents] = useState<Resident[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, addRun, clearRuns] = useStoredRuns();

  useEffect(() => {
    getJSON<{ residents: Resident[] }>("/residents")
      .then(r => setResidents(r.residents))
      .catch(() => setResidents([]));
  }, []);

  const refreshAudit = useCallback(async (requestId: string) => {
    try { setAudit(await getJSON<AuditDetail>(`/audit/${requestId}`)); }
    catch (e) { console.warn("audit fetch failed", e); }
  }, []);

  const runAgent = useCallback(async () => {
    if (!transcript.trim()) return;
    setRunning(true); setError(null); setReply(""); setAudit(null);
    const rid = `web-${Math.random().toString(36).slice(2, 10)}`;
    try {
      const t = await postJSON<AgentTrace>("/agent/run", { transcript }, rid);
      setTrace(t);
      addRun({
        thread_id: t.thread_id,
        request_id: t.request_id,
        transcript,
        status: t.status,
        created_at: new Date().toISOString(),
      });
      await refreshAudit(t.request_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [transcript, addRun, refreshAudit]);

  const resumeAgent = useCallback(async () => {
    if (!trace || !reply.trim()) return;
    setRunning(true); setError(null);
    const rid = `web-${Math.random().toString(36).slice(2, 10)}`;
    try {
      const t = await postJSON<AgentTrace>(
        "/agent/resume",
        { thread_id: trace.thread_id, reply },
        rid,
      );
      setTrace(t);
      addRun({
        thread_id: t.thread_id,
        request_id: t.request_id,
        transcript: trace.messages.find(m => m.type === "HumanMessage")?.content ?? "",
        status: t.status,
        created_at: new Date().toISOString(),
      });
      setReply("");
      await refreshAudit(t.request_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [trace, reply, addRun, refreshAudit]);

  return (
    <div className="h-full flex flex-col">
      <Header residentCount={residents.length} />
      <div className="flex-1 grid grid-cols-[360px_1fr] min-h-0">
        <LeftRail
          transcript={transcript}
          setTranscript={setTranscript}
          runAgent={runAgent}
          running={running}
          runs={runs}
          residents={residents}
          clearRuns={clearRuns}
        />
        <main className="overflow-y-auto p-6 space-y-4">
          {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
          {!trace && <Placeholder />}
          {trace && (
            <>
              <StatusBlock trace={trace} />
              {trace.status === "awaiting_caregiver" && trace.awaiting && (
                <PauseCard
                  awaiting={trace.awaiting}
                  reply={reply}
                  setReply={setReply}
                  onResume={resumeAgent}
                  running={running}
                />
              )}
              {trace.final_message && <FinalAnswer text={trace.final_message} />}
              <Trajectory toolCalls={trace.tool_calls} />
              {audit && <DBWrites audit={audit} />}
              <Conversation messages={trace.messages} />
            </>
          )}
        </main>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Layout pieces
// ────────────────────────────────────────────────────────────────────────────

function Header({ residentCount }: { residentCount: number }) {
  return (
    <header className="px-6 py-3 bg-white border-b border-slate-200 flex items-center gap-3">
      <span className="text-xl">⚕</span>
      <span className="font-semibold">dexter-mini</span>
      <span className="text-slate-400 text-sm">· caregiver console</span>
      <span className="ml-auto text-xs text-slate-500 font-mono">
        {API_BASE} · {residentCount} residents
      </span>
    </header>
  );
}

function LeftRail({
  transcript, setTranscript, runAgent, running, runs, residents, clearRuns,
}: {
  transcript: string;
  setTranscript: (s: string) => void;
  runAgent: () => void;
  running: boolean;
  runs: StoredRun[];
  residents: Resident[];
  clearRuns: () => void;
}) {
  return (
    <aside className="border-r border-slate-200 bg-white overflow-y-auto p-4 space-y-4">
      <section>
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Transcript</div>
        <textarea
          value={transcript}
          onChange={e => setTranscript(e.target.value)}
          rows={6}
          placeholder="Margarethe Müller. BP 130/82. Ate breakfast."
          className="w-full text-sm border border-slate-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400 font-mono"
        />
        <button
          onClick={runAgent}
          disabled={running || !transcript.trim()}
          className="mt-2 w-full bg-slate-900 text-white text-sm font-medium rounded-md py-2 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {running ? "Running…" : "Run agent"}
        </button>
      </section>

      <section>
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Try one of these</div>
        <ul className="space-y-1">
          {EXAMPLES.map(ex => (
            <li key={ex.label}>
              <button
                onClick={() => setTranscript(ex.text)}
                className="w-full text-left text-xs bg-slate-50 hover:bg-slate-100 border border-slate-200 rounded-md px-2 py-1.5"
              >
                <div className="font-medium text-slate-800">{ex.label}</div>
                <div className="text-slate-500 truncate">{ex.text}</div>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <div className="flex items-baseline justify-between mb-1">
          <div className="text-xs uppercase tracking-wide text-slate-500">Residents</div>
          <div className="text-[10px] text-slate-400">seeded</div>
        </div>
        <ul className="space-y-0.5 text-xs">
          {residents.map(r => (
            <li key={r.id} className="flex items-baseline gap-2 px-1 py-0.5">
              <span className="w-8 text-slate-400 font-mono">{r.room_number}</span>
              <span>{r.full_name}</span>
            </li>
          ))}
          {residents.length === 0 && <li className="text-slate-400">(none)</li>}
        </ul>
      </section>

      <section>
        <div className="flex items-baseline justify-between mb-1">
          <div className="text-xs uppercase tracking-wide text-slate-500">Recent runs</div>
          {runs.length > 0 && (
            <button onClick={clearRuns} className="text-[10px] text-slate-400 hover:text-slate-600">
              clear
            </button>
          )}
        </div>
        <ul className="space-y-1 text-xs">
          {runs.map(r => (
            <li key={r.thread_id + r.request_id} className="bg-slate-50 border border-slate-200 rounded-md p-2">
              <div className="flex items-center gap-2">
                <StatusDot status={r.status} />
                <span className="font-mono text-[10px] text-slate-500">{r.request_id}</span>
                <span className="ml-auto text-[10px] text-slate-400">{relTime(r.created_at)}</span>
              </div>
              <div className="text-slate-700 truncate mt-0.5">{r.transcript}</div>
            </li>
          ))}
          {runs.length === 0 && <li className="text-slate-400">(none yet)</li>}
        </ul>
      </section>
    </aside>
  );
}

function Placeholder() {
  return (
    <div className="h-full grid place-items-center">
      <div className="text-center text-slate-400 max-w-md">
        <div className="text-6xl mb-3">📝</div>
        <div className="text-lg">Paste a caregiver transcript on the left, hit Run.</div>
        <div className="text-sm mt-1">The agent will resolve the resident, draft SIS entries,
          validate them, and surface anything ambiguous.</div>
      </div>
    </div>
  );
}

function ErrorBanner({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div className="bg-red-50 border border-red-200 text-red-800 rounded-md p-3 text-sm flex gap-3">
      <span className="font-mono shrink-0">!</span>
      <span className="flex-1">{message}</span>
      <button onClick={onDismiss} className="text-red-600 hover:text-red-800">✕</button>
    </div>
  );
}

function StatusBlock({ trace }: { trace: AgentTrace }) {
  return (
    <div className="bg-white border border-slate-200 rounded-md p-3 flex items-center gap-3 text-sm">
      <StatusDot status={trace.status} />
      <span className="font-medium capitalize">{trace.status.replace("_", " ")}</span>
      <span className="text-slate-300">·</span>
      <span className="font-mono text-xs text-slate-500">thread {trace.thread_id}</span>
      <span className="ml-auto font-mono text-xs text-slate-400">{trace.request_id}</span>
    </div>
  );
}

function PauseCard({
  awaiting, reply, setReply, onResume, running,
}: {
  awaiting: PendingQuestion;
  reply: string;
  setReply: (s: string) => void;
  onResume: () => void;
  running: boolean;
}) {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-md p-4">
      <div className="text-xs uppercase tracking-wide text-amber-700 mb-1">Agent is asking</div>
      <div className="text-sm font-medium text-slate-900">{awaiting.question}</div>
      {Object.keys(awaiting.context).length > 0 && (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer">context</summary>
          <pre className="mt-1 bg-amber-100/60 rounded p-2 overflow-x-auto">
            {JSON.stringify(awaiting.context, null, 2)}
          </pre>
        </details>
      )}
      <div className="mt-3 flex gap-2">
        <input
          value={reply}
          onChange={e => setReply(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") onResume(); }}
          placeholder="Type your reply…"
          className="flex-1 text-sm border border-slate-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-amber-400"
        />
        <button
          onClick={onResume}
          disabled={running || !reply.trim()}
          className="bg-amber-600 text-white text-sm font-medium rounded-md px-4 hover:bg-amber-700 disabled:opacity-40"
        >
          {running ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}

function FinalAnswer({ text }: { text: string }) {
  return (
    <div className="bg-emerald-50 border border-emerald-200 rounded-md p-4">
      <div className="text-xs uppercase tracking-wide text-emerald-700 mb-1">Final answer</div>
      <div className="text-sm text-slate-900 whitespace-pre-wrap">{text}</div>
    </div>
  );
}

function Trajectory({ toolCalls }: { toolCalls: ToolCall[] }) {
  return (
    <section className="bg-white border border-slate-200 rounded-md">
      <header className="px-4 py-2 border-b border-slate-100 flex items-baseline">
        <h2 className="font-semibold text-sm">Trajectory</h2>
        <span className="ml-2 text-xs text-slate-500">{toolCalls.length} tool calls</span>
      </header>
      <ol className="divide-y divide-slate-100">
        {toolCalls.length === 0 && (
          <li className="px-4 py-2 text-sm text-slate-400">(no tool calls)</li>
        )}
        {toolCalls.map((tc, i) => (
          <li key={i} className="px-4 py-2">
            <div className="flex items-baseline gap-2 text-sm">
              <span className="text-slate-400 font-mono text-xs">{String(i + 1).padStart(2, "0")}</span>
              <span className="font-mono text-slate-900">{tc.name}</span>
            </div>
            <details className="mt-1">
              <summary className="text-xs text-slate-500 cursor-pointer hover:text-slate-700">args</summary>
              <pre className="mt-1 bg-slate-50 rounded p-2 text-xs overflow-x-auto">
                {JSON.stringify(tc.args, null, 2)}
              </pre>
            </details>
          </li>
        ))}
      </ol>
    </section>
  );
}

function DBWrites({ audit }: { audit: AuditDetail }) {
  const writes = audit.audit.filter(a => !a.action.startsWith("llm")); // actual side-effects only
  return (
    <section className="bg-white border border-slate-200 rounded-md">
      <header className="px-4 py-2 border-b border-slate-100 flex items-baseline">
        <h2 className="font-semibold text-sm">DB writes</h2>
        <span className="ml-2 text-xs text-slate-500">
          {audit.drafts.length} drafts · {audit.flags.length} flags · {audit.followups.length} follow-ups · {writes.length} audit rows
        </span>
      </header>
      <div className="p-4 space-y-3">
        {audit.drafts.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Drafts</div>
            <ul className="space-y-1">
              {audit.drafts.map(d => <DraftItem key={d.id} draft={d} />)}
            </ul>
          </div>
        )}
        {audit.flags.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Flags</div>
            <ul className="space-y-1 text-sm">
              {audit.flags.map(f => (
                <li key={f.id} className="px-3 py-2 bg-rose-50 border border-rose-200 rounded">
                  <span className="text-[10px] uppercase font-semibold text-rose-700 mr-2">{f.severity}</span>
                  {f.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
        {audit.followups.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Follow-ups</div>
            <ul className="space-y-1 text-sm">
              {audit.followups.map(f => (
                <li key={f.id} className="px-3 py-2 bg-sky-50 border border-sky-200 rounded flex justify-between">
                  <span>{f.action}</span>
                  <span className="text-xs text-slate-500 font-mono">{f.due_at}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <details>
          <summary className="text-xs text-slate-500 cursor-pointer hover:text-slate-700">
            audit_log ({writes.length} rows)
          </summary>
          <ul className="mt-2 space-y-1 text-xs">
            {writes.map(r => (
              <li key={r.id} className="font-mono text-slate-600">
                <span className="text-slate-400">{r.created_at.slice(11, 19)}</span>{" "}
                <span className="font-semibold text-slate-800">{r.action}</span>
                <span className="text-slate-400"> · {r.actor}</span>
              </li>
            ))}
          </ul>
        </details>
      </div>
    </section>
  );
}

function DraftItem({ draft }: { draft: DraftRow }) {
  const conf = draft.validator_confidence;
  const confColor =
    conf == null ? "text-slate-400" :
    conf >= 0.6 ? "text-emerald-700" :
    "text-rose-700";
  return (
    <li className="border border-slate-200 rounded-md">
      <div className="px-3 py-2 flex items-center gap-2 text-sm bg-slate-50">
        <span className="px-2 py-0.5 rounded-full bg-slate-200 text-xs uppercase font-medium">{draft.theme}</span>
        <span className={`text-xs ${confColor}`}>
          {conf == null ? "unvalidated" : `confidence ${(conf * 100).toFixed(0)}%`}
        </span>
        <span className="ml-auto text-[10px] uppercase font-mono text-slate-500">{draft.status}</span>
      </div>
      <pre className="px-3 py-2 text-xs overflow-x-auto whitespace-pre-wrap">
        {JSON.stringify(draft.content, null, 2)}
      </pre>
    </li>
  );
}

function Conversation({ messages }: { messages: SerialMessage[] }) {
  return (
    <section className="bg-white border border-slate-200 rounded-md">
      <header className="px-4 py-2 border-b border-slate-100">
        <details>
          <summary className="cursor-pointer flex items-baseline">
            <h2 className="font-semibold text-sm">Conversation</h2>
            <span className="ml-2 text-xs text-slate-500">{messages.length} messages (click to expand)</span>
          </summary>
          <ol className="mt-3 space-y-2">
            {messages.map((m, i) => (
              <li key={i} className="text-xs">
                <div className="text-slate-500 font-mono">{m.type}</div>
                <pre className="mt-0.5 bg-slate-50 rounded p-2 overflow-x-auto whitespace-pre-wrap">
                  {m.content ?? "(empty)"}
                </pre>
              </li>
            ))}
          </ol>
        </details>
      </header>
    </section>
  );
}

function StatusDot({ status }: { status: AgentStatus }) {
  const c = status === "complete" ? "bg-emerald-500" : "bg-amber-500";
  return <span className={`inline-block w-2 h-2 rounded-full ${c}`} />;
}

function relTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

// Suppress vite-env type warning for VITE_ vars.
declare global {
  interface ImportMetaEnv { readonly VITE_GATEWAY_URL?: string; }
  interface ImportMeta { readonly env: ImportMetaEnv; }
}
