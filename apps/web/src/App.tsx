import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = (import.meta.env.VITE_GATEWAY_URL as string) || "http://localhost:8000";

// ────────────────────────────────────────────────────────────────────────────
// Types — mirror the FastAPI response shapes. Keep flat; no codegen for now.
// ────────────────────────────────────────────────────────────────────────────

type AgentStatus = "complete" | "awaiting_caregiver";

interface ToolCall { name: string; args: Record<string, unknown>; output?: unknown }
interface CareGapItem {
  kind: string;
  severity: "info" | "watch" | "high";
  description: string;
  suggested_action?: string | null;
  evidence?: Record<string, unknown>;
}
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

async function postAudio<T>(path: string, blob: Blob): Promise<T> {
  const fd = new FormData();
  fd.append("audio", blob, "voice.webm");
  const r = await fetch(`${API_BASE}${path}`, { method: "POST", body: fd });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

// ────────────────────────────────────────────────────────────────────────────
// Hooks
// ────────────────────────────────────────────────────────────────────────────

type RecorderState = "idle" | "recording" | "transcribing";

function useVoiceRecorder(onTranscript: (s: string) => void) {
  const [state, setState] = useState<RecorderState>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef<number>(0);
  const tickRef = useRef<number | null>(null);

  const stop = useCallback(() => {
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") rec.stop();
    if (tickRef.current != null) {
      window.clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const rec = new MediaRecorder(stream, { mimeType: mime });
      chunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(chunksRef.current, { type: mime });
        if (blob.size === 0) { setState("idle"); return; }
        setState("transcribing");
        try {
          const res = await postAudio<{ transcript: string }>("/transcribe", blob);
          onTranscript(res.transcript);
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally {
          setState("idle");
          setElapsedMs(0);
        }
      };
      recorderRef.current = rec;
      startedAtRef.current = performance.now();
      tickRef.current = window.setInterval(() => {
        setElapsedMs(Math.floor(performance.now() - startedAtRef.current));
      }, 100);
      rec.start();
      setState("recording");
    } catch (err) {
      setError(err instanceof Error ? err.message : "mic permission denied");
      setState("idle");
    }
  }, [onTranscript]);

  return { state, elapsedMs, error, start, stop };
}

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
              <CareGaps toolCalls={trace.tool_calls} />
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
    <header className="px-6 py-3 bg-white/80 backdrop-blur border-b border-slate-200 flex items-center gap-3 shadow-sm">
      <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-emerald-500 to-emerald-700 grid place-items-center text-white shadow-md">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg>
      </div>
      <div className="flex flex-col leading-tight">
        <span className="font-semibold tracking-tight">dexter-mini</span>
        <span className="text-[11px] text-slate-500">caregiver console</span>
      </div>
      <div className="ml-auto flex items-center gap-3">
        <div className="hidden md:flex items-center gap-1.5 text-[11px] text-slate-500">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span className="font-mono">{API_BASE}</span>
        </div>
        <div className="px-2.5 py-1 rounded-full bg-slate-100 text-[11px] font-medium text-slate-700">
          {residentCount} residents
        </div>
      </div>
    </header>
  );
}

function VoiceControl({ onTranscript, disabled }: { onTranscript: (s: string) => void; disabled: boolean }) {
  const { state, elapsedMs, error, start, stop } = useVoiceRecorder(onTranscript);
  const seconds = (elapsedMs / 1000).toFixed(1);
  const cls =
    state === "recording" ? "bg-rose-50 border-rose-300 text-rose-700 hover:bg-rose-100"
    : state === "transcribing" ? "bg-slate-100 border-slate-300 text-slate-500 cursor-wait"
    : "bg-white border-slate-300 text-slate-700 hover:bg-emerald-50 hover:border-emerald-300 hover:text-emerald-700";
  return (
    <div className="flex flex-col items-end">
      <button
        type="button"
        onClick={state === "recording" ? stop : start}
        disabled={disabled || state === "transcribing"}
        className={`inline-flex items-center gap-1.5 text-[11px] font-medium border rounded-full px-2.5 py-1 transition-colors ${cls} disabled:opacity-40`}
      >
        {state === "recording" ? (
          <span className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
        ) : state === "transcribing" ? (
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" className="animate-spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
        ) : (
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
        )}
        <span>{state === "recording" ? `Recording ${seconds}s` : state === "transcribing" ? "Transcribing…" : "Voice"}</span>
      </button>
      {error && <span className="text-[10px] text-rose-600 mt-0.5">{error}</span>}
    </div>
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
    <aside className="border-r border-slate-200 bg-white/70 backdrop-blur overflow-y-auto p-4 space-y-5">
      <section>
        <div className="flex items-center justify-between mb-1.5">
          <SectionLabel>Transcript</SectionLabel>
          <VoiceControl onTranscript={t => setTranscript(t)} disabled={running} />
        </div>
        <textarea
          value={transcript}
          onChange={e => setTranscript(e.target.value)}
          rows={6}
          placeholder="Frau Müller, room 12. BP 128/78. Ate breakfast."
          className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500 bg-white shadow-sm transition-shadow"
        />
        <button
          onClick={runAgent}
          disabled={running || !transcript.trim()}
          style={{ background: 'linear-gradient(135deg, #059669 0%, #047857 100%)' }}
          className="mt-2.5 w-full text-white text-sm font-semibold rounded-lg py-2.5 shadow-md hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none transition-all inline-flex items-center justify-center gap-2"
        >
          {running ? (
            <>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" className="animate-spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
              Running…
            </>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3" /></svg>
              Run agent
            </>
          )}
        </button>
      </section>

      <section>
        <SectionLabel>Try one of these</SectionLabel>
        <ul className="space-y-1.5 mt-1.5">
          {EXAMPLES.map(ex => (
            <li key={ex.label}>
              <button
                onClick={() => setTranscript(ex.text)}
                className="w-full text-left text-xs bg-white hover:bg-emerald-50 border border-slate-200 hover:border-emerald-300 rounded-lg px-3 py-2 transition-colors shadow-sm hover:shadow-md group"
              >
                <div className="font-semibold text-slate-800 group-hover:text-emerald-800">{ex.label}</div>
                <div className="text-slate-500 truncate mt-0.5">{ex.text}</div>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <div className="flex items-center justify-between">
          <SectionLabel>Residents</SectionLabel>
          <span className="text-[10px] text-slate-400 font-medium">seeded</span>
        </div>
        <ul className="space-y-0.5 text-xs mt-1.5">
          {residents.map(r => (
            <li key={r.id} className="flex items-baseline gap-2 px-2 py-1 rounded hover:bg-slate-50">
              <span className="inline-block w-7 text-center text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{r.room_number}</span>
              <span className="text-slate-700">{r.full_name}</span>
            </li>
          ))}
          {residents.length === 0 && <li className="text-slate-400 italic px-2">(none)</li>}
        </ul>
      </section>

      <section>
        <div className="flex items-center justify-between">
          <SectionLabel>Recent runs</SectionLabel>
          {runs.length > 0 && (
            <button onClick={clearRuns} className="text-[10px] text-slate-400 hover:text-rose-600 font-medium">
              clear
            </button>
          )}
        </div>
        <ul className="space-y-1.5 text-xs mt-1.5">
          {runs.map(r => (
            <li key={r.thread_id + r.request_id} className="bg-white border border-slate-200 rounded-lg p-2.5 shadow-sm">
              <div className="flex items-center gap-2">
                <StatusDot status={r.status} />
                <span className="font-mono text-[10px] text-slate-500 truncate flex-1">{r.request_id}</span>
                <span className="text-[10px] text-slate-400">{relTime(r.created_at)}</span>
              </div>
              <div className="text-slate-700 truncate mt-1">{r.transcript}</div>
            </li>
          ))}
          {runs.length === 0 && <li className="text-slate-400 italic">(none yet)</li>}
        </ul>
      </section>
    </aside>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return <div className="text-[10px] uppercase tracking-widest font-semibold text-slate-500">{children}</div>;
}

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`bg-white border border-slate-200 rounded-xl shadow-sm ${className}`}>
      {children}
    </section>
  );
}

function Placeholder() {
  return (
    <div className="h-full grid place-items-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-emerald-100 to-emerald-200 grid place-items-center shadow-md">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#047857" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg>
        </div>
        <div className="text-lg font-semibold text-slate-800">Ready when you are.</div>
        <div className="text-sm text-slate-500 mt-2 leading-relaxed">
          Dictate or paste a caregiver note. The agent will resolve the resident,
          draft SIS entries, validate them, and surface care gaps you might have missed.
        </div>
      </div>
    </div>
  );
}

function ErrorBanner({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded-xl p-3 text-sm flex gap-3 shadow-sm">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span className="flex-1">{message}</span>
      <button onClick={onDismiss} className="text-rose-600 hover:text-rose-800 font-semibold">✕</button>
    </div>
  );
}

function StatusBlock({ trace }: { trace: AgentTrace }) {
  const isPaused = trace.status === "awaiting_caregiver";
  const pillCls = isPaused
    ? "bg-amber-50 text-amber-800 border-amber-200"
    : "bg-emerald-50 text-emerald-800 border-emerald-200";
  return (
    <Card className="px-4 py-3 flex items-center gap-3 text-sm">
      <div className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-full border ${pillCls} text-xs font-semibold`}>
        <StatusDot status={trace.status} />
        <span className="capitalize">{trace.status.replace("_", " ")}</span>
      </div>
      <span className="text-slate-400 hidden md:inline">·</span>
      <span className="font-mono text-xs text-slate-500 hidden md:inline">thread <span className="text-slate-700">{trace.thread_id}</span></span>
      <span className="ml-auto font-mono text-xs text-slate-400">{trace.request_id}</span>
    </Card>
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
    <div className="bg-gradient-to-br from-amber-50 to-orange-50 border border-amber-300 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-2">
        <div className="w-7 h-7 rounded-lg bg-amber-500 grid place-items-center text-white">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/><circle cx="12" cy="12" r="10"/></svg>
        </div>
        <div className="text-xs uppercase tracking-widest font-semibold text-amber-800">Agent is asking</div>
      </div>
      <div className="text-sm font-medium text-slate-900 leading-relaxed">{awaiting.question}</div>
      {Object.keys(awaiting.context).length > 0 && (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer text-amber-700 hover:text-amber-900 font-medium">context</summary>
          <pre className="mt-1.5 bg-white/60 rounded-md p-2.5 overflow-x-auto border border-amber-200">
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
          className="flex-1 text-sm border border-amber-300 bg-white rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-amber-400"
        />
        <button
          onClick={onResume}
          disabled={running || !reply.trim()}
          style={{ background: '#d97706' }}
          className="text-white text-sm font-semibold rounded-lg px-4 hover:opacity-90 disabled:opacity-40 shadow-sm"
        >
          {running ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}

function FinalAnswer({ text }: { text: string }) {
  return (
    <div className="bg-gradient-to-br from-emerald-50 to-teal-50 border border-emerald-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-2">
        <div className="w-7 h-7 rounded-lg grid place-items-center text-white" style={{ background: '#059669' }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        <div className="text-xs uppercase tracking-widest font-semibold text-emerald-800">Final answer</div>
      </div>
      <div className="text-sm text-slate-900 whitespace-pre-wrap leading-relaxed">{text}</div>
    </div>
  );
}

function CareGaps({ toolCalls }: { toolCalls: ToolCall[] }) {
  const radar = [...toolCalls].reverse().find(tc => tc.name === "find_care_gaps");
  if (!radar || !radar.output || typeof radar.output !== "object") return null;
  const output = radar.output as { gaps?: CareGapItem[]; days_considered?: number };
  const gaps = output.gaps ?? [];
  const days = output.days_considered ?? 5;
  const highCount = gaps.filter(g => g.severity === "high").length;
  const watchCount = gaps.filter(g => g.severity === "watch").length;

  return (
    <Card className="overflow-hidden">
      <header className="px-4 py-3 border-b border-slate-100 bg-gradient-to-r from-indigo-50 via-white to-white flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg grid place-items-center text-white shadow-md" style={{ background: 'linear-gradient(135deg, #6366f1, #4338ca)' }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Care Gap Radar</h3>
          <div className="text-[11px] text-slate-500">past {days} days</div>
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          {highCount > 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-rose-100 text-rose-700 border border-rose-200">{highCount} high</span>
          )}
          {watchCount > 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-amber-100 text-amber-800 border border-amber-200">{watchCount} watch</span>
          )}
          {gaps.length === 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-emerald-100 text-emerald-800 border border-emerald-200">all clear</span>
          )}
        </div>
      </header>
      <div className="p-3">
        {gaps.length === 0 ? (
          <div className="text-sm text-slate-500 italic px-1 py-2">No care gaps detected.</div>
        ) : (
          <ul className="space-y-2">{gaps.map((g, i) => <GapRow key={i} gap={g} />)}</ul>
        )}
      </div>
    </Card>
  );
}

const _GAP_KIND_ICONS: Record<string, ReactNode> = {
  nutrition_pattern: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 11h18M5 11V8a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v3M5 11v5a4 4 0 0 0 4 4h6a4 4 0 0 0 4-4v-5"/></svg>,
  missing_vital: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
  escalating_vital: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>,
  plan_risk_unaddressed: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>,
  overdue_followup: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
};

function GapRow({ gap }: { gap: CareGapItem }) {
  const sev = gap.severity || "info";
  const sevCls =
    sev === "high"  ? "bg-rose-50 border-rose-200" :
    sev === "watch" ? "bg-amber-50 border-amber-200" :
                      "bg-slate-50 border-slate-200";
  const iconBgCls =
    sev === "high"  ? "bg-rose-500 text-white" :
    sev === "watch" ? "bg-amber-500 text-white" :
                      "bg-slate-400 text-white";
  const sevBadgeCls =
    sev === "high"  ? "bg-rose-100 text-rose-800 border-rose-200" :
    sev === "watch" ? "bg-amber-100 text-amber-800 border-amber-200" :
                      "bg-slate-100 text-slate-700 border-slate-200";
  const kindLabel = gap.kind.replace(/_/g, " ");
  return (
    <li className={`border rounded-lg p-3 ${sevCls} flex gap-3`}>
      <div className={`w-7 h-7 rounded-lg grid place-items-center shrink-0 ${iconBgCls}`}>
        {_GAP_KIND_ICONS[gap.kind] ?? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/></svg>}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${sevBadgeCls}`}>{sev}</span>
          <span className="text-[11px] font-mono text-slate-500">{kindLabel}</span>
        </div>
        <div className="text-sm text-slate-800 mt-1 leading-snug">{gap.description}</div>
        {gap.suggested_action && (
          <div className="text-xs text-slate-600 mt-1.5 flex items-start gap-1.5">
            <span className="text-slate-400">→</span>
            <span>{gap.suggested_action}</span>
          </div>
        )}
      </div>
    </li>
  );
}

const _TOOL_FAMILY: Record<string, { label: string; cls: string }> = {
  get_resident:               { label: "read",     cls: "bg-sky-100 text-sky-700 border-sky-200" },
  get_recent_notes:           { label: "read",     cls: "bg-sky-100 text-sky-700 border-sky-200" },
  search_care_plan:           { label: "read",     cls: "bg-sky-100 text-sky-700 border-sky-200" },
  check_vital_ranges:         { label: "analyse",  cls: "bg-violet-100 text-violet-700 border-violet-200" },
  draft_sis_entry:            { label: "write",    cls: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  validate_entry:             { label: "validate", cls: "bg-teal-100 text-teal-700 border-teal-200" },
  synthesize_summary:         { label: "write",    cls: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  flag_for_review:            { label: "flag",     cls: "bg-rose-100 text-rose-700 border-rose-200" },
  schedule_followup:          { label: "schedule", cls: "bg-orange-100 text-orange-700 border-orange-200" },
  ask_caregiver:              { label: "pause",    cls: "bg-amber-100 text-amber-700 border-amber-200" },
  finalize_entry:             { label: "finalize", cls: "bg-slate-100 text-slate-700 border-slate-200" },
  list_pending_documentation: { label: "read",     cls: "bg-sky-100 text-sky-700 border-sky-200" },
  find_care_gaps:             { label: "radar",    cls: "bg-indigo-100 text-indigo-700 border-indigo-200" },
};

function Trajectory({ toolCalls }: { toolCalls: ToolCall[] }) {
  return (
    <Card>
      <header className="px-4 py-3 border-b border-slate-100 flex items-center">
        <h2 className="font-semibold text-sm text-slate-800">Trajectory</h2>
        <span className="ml-2 px-2 py-0.5 rounded-full bg-slate-100 text-[10px] font-semibold text-slate-600">{toolCalls.length} tool calls</span>
      </header>
      <ol>
        {toolCalls.length === 0 && (
          <li className="px-4 py-3 text-sm text-slate-400 italic">(no tool calls)</li>
        )}
        {toolCalls.map((tc, i) => {
          const fam = _TOOL_FAMILY[tc.name] ?? { label: "tool", cls: "bg-slate-100 text-slate-700 border-slate-200" };
          return (
            <li key={i} className="relative px-4 py-2.5 hover:bg-slate-50/60 border-t border-slate-100 first:border-t-0">
              <div className="flex items-center gap-3">
                <span className="text-slate-300 font-mono text-[10px] font-medium w-5 text-right">{String(i + 1).padStart(2, "0")}</span>
                <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${fam.cls}`}>{fam.label}</span>
                <span className="font-mono text-sm text-slate-900">{tc.name}</span>
              </div>
              <details className="mt-1 ml-12">
                <summary className="text-[11px] text-slate-400 cursor-pointer hover:text-slate-700 inline-flex items-center gap-1">
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                  args
                </summary>
                <pre className="mt-1.5 bg-slate-900 text-slate-100 rounded-md p-2.5 text-[11px] overflow-x-auto font-mono">
                  {JSON.stringify(tc.args, null, 2)}
                </pre>
              </details>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function DBWrites({ audit }: { audit: AuditDetail }) {
  const writes = audit.audit.filter(a => !a.action.startsWith("llm"));
  return (
    <Card>
      <header className="px-4 py-3 border-b border-slate-100 flex items-center gap-3">
        <h2 className="font-semibold text-sm text-slate-800">DB writes</h2>
        <div className="flex items-center gap-1.5 ml-auto">
          <Pill count={audit.drafts.length} label="drafts" tone="emerald" />
          <Pill count={audit.flags.length} label="flags" tone="rose" />
          <Pill count={audit.followups.length} label="follow-ups" tone="sky" />
        </div>
      </header>
      <div className="p-4 space-y-4">
        {audit.drafts.length > 0 && (
          <div>
            <SectionLabel>Drafts</SectionLabel>
            <ul className="space-y-2 mt-1.5">
              {audit.drafts.map(d => <DraftItem key={d.id} draft={d} />)}
            </ul>
          </div>
        )}
        {audit.flags.length > 0 && (
          <div>
            <SectionLabel>Flags</SectionLabel>
            <ul className="space-y-1.5 mt-1.5 text-sm">
              {audit.flags.map(f => (
                <li key={f.id} className="px-3 py-2 bg-rose-50 border border-rose-200 rounded-lg flex gap-2">
                  <span className="text-[10px] uppercase font-bold tracking-wider text-rose-700 bg-rose-100 border border-rose-200 px-1.5 py-0.5 rounded shrink-0 self-start">{f.severity}</span>
                  <span className="text-slate-800">{f.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {audit.followups.length > 0 && (
          <div>
            <SectionLabel>Follow-ups</SectionLabel>
            <ul className="space-y-1.5 mt-1.5 text-sm">
              {audit.followups.map(f => (
                <li key={f.id} className="px-3 py-2 bg-sky-50 border border-sky-200 rounded-lg flex justify-between gap-3">
                  <span className="text-slate-800">{f.action}</span>
                  <span className="text-xs text-sky-700 font-mono shrink-0">{f.due_at}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <details>
          <summary className="text-xs text-slate-500 cursor-pointer hover:text-slate-700 inline-flex items-center gap-1.5 font-medium">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
            audit_log ({writes.length} rows)
          </summary>
          <ul className="mt-2 space-y-0.5 text-xs">
            {writes.map(r => (
              <li key={r.id} className="font-mono text-slate-600 px-2 py-1 rounded hover:bg-slate-50">
                <span className="text-slate-400">{r.created_at.slice(11, 19)}</span>{" "}
                <span className="font-semibold text-slate-800">{r.action}</span>
                <span className="text-slate-400"> · {r.actor}</span>
              </li>
            ))}
          </ul>
        </details>
      </div>
    </Card>
  );
}

function Pill({ count, label, tone }: { count: number; label: string; tone: "emerald" | "rose" | "sky" }) {
  const cls = count === 0
    ? "bg-slate-100 text-slate-500 border-slate-200"
    : tone === "emerald" ? "bg-emerald-100 text-emerald-800 border-emerald-200"
    : tone === "rose"    ? "bg-rose-100 text-rose-800 border-rose-200"
    :                      "bg-sky-100 text-sky-800 border-sky-200";
  return (
    <span className={`px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase tracking-wider ${cls}`}>
      {count} {label}
    </span>
  );
}

const _THEME_BADGE_CLS: Record<string, string> = {
  vitals:    "bg-rose-100 text-rose-800 border-rose-200",
  nutrition: "bg-orange-100 text-orange-800 border-orange-200",
  mobility:  "bg-sky-100 text-sky-800 border-sky-200",
  cognition: "bg-violet-100 text-violet-800 border-violet-200",
  social:    "bg-pink-100 text-pink-800 border-pink-200",
  incident:  "bg-red-100 text-red-800 border-red-200",
};

function DraftItem({ draft }: { draft: DraftRow }) {
  const conf = draft.validator_confidence;
  const themeCls = _THEME_BADGE_CLS[draft.theme] ?? "bg-slate-100 text-slate-700 border-slate-200";
  const statusCls = draft.status === "needs_review"
    ? "bg-amber-100 text-amber-800"
    : draft.status === "final"
      ? "bg-emerald-100 text-emerald-800"
      : "bg-slate-100 text-slate-700";
  const confColor =
    conf == null ? "text-slate-400" :
    conf >= 0.6 ? "text-emerald-700" :
    "text-rose-700";
  return (
    <li className="border border-slate-200 rounded-lg overflow-hidden shadow-sm">
      <div className="px-3 py-2 flex items-center gap-2 text-sm bg-slate-50 border-b border-slate-200">
        <span className={`px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider border ${themeCls}`}>{draft.theme}</span>
        <span className={`text-[11px] font-medium ${confColor}`}>
          {conf == null ? "unvalidated" : `${(conf * 100).toFixed(0)}% confidence`}
        </span>
        <span className={`ml-auto text-[10px] uppercase font-bold tracking-wider font-mono px-1.5 py-0.5 rounded ${statusCls}`}>{draft.status}</span>
      </div>
      <pre className="px-3 py-2 text-[11px] overflow-x-auto whitespace-pre-wrap font-mono text-slate-700 bg-white">
        {JSON.stringify(draft.content, null, 2)}
      </pre>
    </li>
  );
}

function Conversation({ messages }: { messages: SerialMessage[] }) {
  return (
    <Card>
      <details>
        <summary className="cursor-pointer px-4 py-3 flex items-center hover:bg-slate-50">
          <h2 className="font-semibold text-sm text-slate-800">Conversation</h2>
          <span className="ml-2 px-2 py-0.5 rounded-full bg-slate-100 text-[10px] font-semibold text-slate-600">{messages.length} messages</span>
          <svg className="ml-auto text-slate-400" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </summary>
        <ol className="p-4 space-y-2 border-t border-slate-100">
          {messages.map((m, i) => (
            <li key={i} className="text-xs">
              <div className="text-slate-500 font-mono font-semibold">{m.type}</div>
              <pre className="mt-1 bg-slate-50 rounded-md p-2 overflow-x-auto whitespace-pre-wrap border border-slate-200">
                {m.content ?? "(empty)"}
              </pre>
            </li>
          ))}
        </ol>
      </details>
    </Card>
  );
}

function StatusDot({ status }: { status: AgentStatus }) {
  const c = status === "complete" ? "bg-emerald-500" : "bg-amber-500";
  return <span className={`inline-block w-2 h-2 rounded-full ${c} ${status === "awaiting_caregiver" ? "animate-pulse" : ""}`} />;
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
