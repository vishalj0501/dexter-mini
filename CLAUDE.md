# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`dexter-mini` is a 7-day vertical-slice prototype for a dexter health interview: a voice-driven care-documentation agent for German elderly-care homes. A caregiver speaks; the agent resolves the resident, pulls history, drafts SIS-aligned (Strukturierte Informationssammlung) entries, validates them, flags clinical risks, and pauses for the caregiver when it needs clarification. The full product story lives in `SPEC.md`; treat it as the source of truth for scope decisions and the day-by-day plan.

## Common commands

### Gateway (Python / FastAPI)

The gateway is a `uv`-managed project at `apps/gateway`.

```bash
cd apps/gateway
uv sync                                    # install deps
uv run uvicorn app.main:app --reload       # run locally against Postgres in compose
uv run pytest                              # full suite
uv run pytest tests/test_agent.py -q       # one file
uv run pytest tests/test_agent.py::test_graph_writes_draft_through_full_loop  # one test
```

Tests use an in-memory SQLite per test (see `tests/conftest.py`), so the suite runs in seconds without Docker. The gateway itself uses Postgres via Tortoise ORM — do not assume Postgres-specific SQL features at the tool layer.

### Frontend (Vite + React + Tailwind)

The web app at `apps/web` is **not** in docker-compose by design — run it directly:

```bash
cd apps/web
npm install
npm run dev          # http://localhost:5173, talks to gateway on :8000
```

It reads `VITE_GATEWAY_URL` (defaults to `http://localhost:8000`). CORS for `localhost:5173` is already wired in `app/main.py`.

### Docker

```bash
docker compose up --build       # db + gateway (NOT web)
docker compose down -v          # nuke the seeded DB and start over
```

`docker-compose.yml` mounts `./apps/gateway` into the container with `--reload`, so editing Python files hot-reloads.

## Architecture

### Request flow

```
web (Vite)  →  FastAPI gateway  →  LangGraph agent  →  @tool functions  →  Tortoise/Postgres
                       │                  │                                       │
                       │                  └── LiteLLM ──→ Replicate/OpenAI/etc.   │
                       └── audit_log row per request_id  ─────────────────────────┘
```

Every HTTP request gets a `request_id` from `RequestIDMiddleware`. The agent threads it through `config["configurable"]` so every tool call writes an `audit_log` row tagged with the same `request_id` — that's how you reconstruct a trajectory in SQL after the fact (see `GET /audit/{request_id}`).

### The agent (`app/agent/`)

- `graph.py` — Hand-wired LangGraph state machine: `START → planner → tools → planner → … → END`. Uses `MemorySaver` checkpointer keyed by `thread_id`, so a single conversation can be paused and resumed across HTTP calls.
- `prompts.py` — System prompt assembled at import time. **Prompted ReAct** (Thought / Action / Action Input / Observation / Final Answer) because Replicate doesn't support OpenAI's `tools=` API. Sections like `CONTEXT-GATHERING ORDER`, `VALIDATOR RETRY`, `STUCK DETECTION` are load-bearing — the planner depends on them.
- `react_parser.py` — Parses the model's ReAct text into `AgentAction` or `AgentFinish`. Tolerates markdown fences and trailing prose.
- `llm_tools.py` — `ALL_TOOLS` list of LangChain `@tool`-wrapped callables fed both to the prompt catalog and the tool dispatcher.

The planner does **not** use LLM tool calling. Observations are emitted back as `HumanMessage` with an `Observation:` prefix, not `ToolMessage`, because there's no `tool_call_id` from the model.

### Pause/resume via `ask_caregiver`

`ask_caregiver` is a real tool that calls LangGraph's `interrupt()`. The graph stops, the route serializes pending state, the client sees `status="awaiting_caregiver"`, and a later `POST /agent/resume` calls the graph with `Command(resume=<reply>)`. `thread_id` is the join key. Treat any change to ask_caregiver / the resume route as touching a state machine, not a function call — the same node re-runs on resume and audit rows can be written multiple times.

### State guards (added through Day 4)

`graph.py` enforces a stack of guards in the planner's `AgentFinish` branch (the "COMPLETION CHECK"):

1. **Anti-hallucination** — block `Final Answer` if no real `entry_id` appears in observations but the text claims drafting.
2. **Reflection** — block if expected themes (derived from regex against the transcript) aren't all drafted.
3. **Unvalidated drafts** — every `draft_sis_entry` must have a matching `validate_entry` observation with `passed`.
4. **Missing flag** — abnormal vitals or watch + plan-risk must trigger `flag_for_review`.
5. **Claimed-but-not-called flag** — `Final Answer` text that says "flagged" without a real `flag_id` observation is rejected.

When you change planner behavior, expect the test sequences in `tests/test_agent.py` (which use a `ScriptedClient` driving deterministic LLM outputs) to need updated scripts — the guards reject otherwise-passing scripts that don't validate or flag.

In-loop hints injected into observations: `_retry_hint` (on validator fail), `_give_up_hint` (after 2 retries → flag and move on), `_stuck_hint` (many tool calls without a draft).

### Tools (`app/tools/`)

Each public tool is wrapped in `@audited(AuditAction.X)` which writes an `audit_log` row with the tool name, args, result status, latency, and `request_id`. Tool inputs/outputs are Pydantic models in `_types.py` — that's the contract the LLM sees (via JSON schemas rendered into the prompt) and what the audit log serializes. Don't bypass the Pydantic boundary; the prompt catalog is generated from those schemas.

`get_resident` returns `recent_activity` (24h event count, last event time, open flags/follow-ups). The prompt has a mandatory rule that the planner must call `get_recent_notes` whenever those counters are non-zero — this is how cross-conversation continuity ("escalating BP pattern") works without giving the agent a persistent memory.

### LLM layer (`app/llm/`)

- `client.py` — `LLMClient.complete(...)` is the single chokepoint. Returns a normalized `Completion`, writes an `audit_log` row per call.
- `_settings.py` — Role → (primary, fallback) model map: planner / extractor / judge. Defaults to `replicate/anthropic/claude-4.5-sonnet`; swap via `DEXTER_LLM_PLANNER`, `DEXTER_LLM_EXTRACTOR`, etc. env vars.
- `reliability.py` — Per-role circuit breaker.
- LangFuse integration is via LiteLLM's success callback; activates iff `LANGFUSE_PUBLIC_KEY` is set. (Pinned to `langfuse<3.0` — LiteLLM v3 callback isn't compatible yet.)

### Database (`app/models.py`, `app/db.py`)

Tortoise ORM with `generate_schemas(safe=True)` — no migrations yet. Reset = `docker compose down -v`. Seeds (`app/seeds/`) populate 8 demo residents with a week of history on first boot if `AUTO_SEED=true`.

Key tables: `residents`, `care_plans`, `care_events` (SIS entries with `theme`, `content` JSON, `status`, `request_id`), `audit_log`, `review_flags`, `followups`, `eval_runs`.

### Frontend (`apps/web/`)

Single-page caregiver console in `src/App.tsx` (~440 lines). One file, no router. Components: TranscriptInput, RunBadge, TrajectoryView (renders tool calls from `tool_calls`), AwaitingCard (`ask_caregiver` interrupt UI), MessagesView, DBWrites. `localStorage` persists recent runs by `request_id`. Voice (Whisper + MediaRecorder) is the last Day-5 task and is deliberately deferred.

## Conventions worth knowing before editing

- **Don't claim work without doing it.** The completion-check guard and prompt section "CRITICAL — DON'T LIE" exist because the agent will otherwise hallucinate "I've documented X" without calling `draft_sis_entry`. Mirror this in any new tools: the Final Answer is only allowed to reference real `entry_id` / `flag_id` / `followup_id` values that came from real observations.
- **`request_id` is mandatory** in every tool call and every agent run. Tools take it as a kwarg; the agent threads it via `config["configurable"]["request_id"]`. Don't add a tool that doesn't accept it — audit traceability breaks.
- **The tool catalog is generated from Pydantic schemas in `_types.py`** via `render_tool_catalog`. Renaming a model field changes what the LLM sees in the prompt.
- **Tests are async** (`pytest-asyncio` in auto mode). Fixtures `resident` and `other_resident` (in `conftest.py`) cover the common Müller / ambiguity setup.
- **No native function calling.** If you find yourself wanting to use `tools=` on the LLM client, stop — the whole design assumes prompted ReAct so the same code works across Replicate, OpenAI, Anthropic, OpenRouter, etc.
