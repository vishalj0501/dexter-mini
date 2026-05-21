"""Eval harness CLI.

Usage:
    uv run python -m app.evals                       # default planner, all cases
    uv run python -m app.evals --case muller-elevated-bp-and-refusal
    uv run python -m app.evals --providers replicate/anthropic/claude-4.5-sonnet,replicate/openai/gpt-4o
    uv run python -m app.evals --no-persist          # don't write EvalRun rows

By default uses an in-memory SQLite seeded fresh, so the harness needs no
docker. Override with DATABASE_URL to point at the real Postgres if you want
the EvalRun rows to land in your dev DB.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import subprocess
from typing import Iterable

from tortoise import Tortoise

from app.evals.cases import GOLDEN_SET, EvalCase
from app.evals.runner import CaseResult, run_case
from app.evals.scorers import ScoreBreakdown, aggregate, score
from app.llm import LiteLLMClient, clear_cache, get_client
from app.llm import _settings as _llm_settings
from app.llm._settings import LLMSettings
from app.models import EvalRun
from app.seeds.seed import seed_if_empty

log = logging.getLogger("evals")


SQLITE_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
    "use_tz": True,
    "timezone": "UTC",
}


async def _bootstrap_db() -> None:
    """In-memory SQLite + seeded residents/care plans/history."""
    if Tortoise._inited:
        return
    await Tortoise.init(config=SQLITE_CONFIG)
    await Tortoise.generate_schemas()
    await seed_if_empty()


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=2,
        )
        return out.stdout.strip()[:40] or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _build_client(planner_model: str | None) -> LiteLLMClient:
    """Return the planner client, with an optional model override.

    Reassigns the module-level `llm_settings` so `get_client("planner")`
    picks up the new model on next construction. Clears the lru cache so
    the next call rebuilds with fresh config.
    """
    if planner_model:
        os.environ["DEXTER_LLM_PLANNER"] = planner_model
        _llm_settings.llm_settings = LLMSettings()
    clear_cache()
    return get_client("planner")


async def _run_provider(
    provider: str | None,
    cases: list[EvalCase],
    persist: bool,
    git_sha: str | None,
) -> list[tuple[EvalCase, CaseResult, ScoreBreakdown]]:
    label = provider or "default"
    print(f"\n=== provider: {label} ===")
    client = _build_client(provider)
    out: list[tuple[EvalCase, CaseResult, ScoreBreakdown]] = []
    for case in cases:
        print(f"  • {case.id} … ", end="", flush=True)
        result = await run_case(case, client=client)
        sb = score(case, result)
        out.append((case, result, sb))
        status = "PASS" if sb.passed else "FAIL"
        print(f"{status}  (tools={sb.tool_selection_accuracy:.2f} "
              f"flag={sb.flagged_when_should_have:.0f} "
              f"halluc={sb.hallucination_rate:.2f} "
              f"validate={sb.schema_validity_rate:.2f})")
        if sb.notes:
            for note in sb.notes:
                print(f"      - {note}")

    if persist:
        await _persist_run(label, cases, out, git_sha)
    return out


async def _persist_run(
    provider: str,
    cases: list[EvalCase],
    results: list[tuple[EvalCase, CaseResult, ScoreBreakdown]],
    git_sha: str | None,
) -> None:
    scores = [sb for _, _, sb in results]
    agg = aggregate(scores)
    latencies = [r.total_latency_ms for _, r, _ in results if r.total_latency_ms]
    costs = [r.total_cost_usd for _, r, _ in results]
    p50 = int(statistics.median(latencies)) if latencies else None
    p95 = int(_percentile(latencies, 95)) if latencies else None

    raw = {
        "provider": provider,
        "cases": [
            {
                "case_id": c.id,
                "passed": sb.passed,
                "tool_sequence": r.tool_sequence,
                "drafted_themes": r.drafted_themes,
                "flag_ids": [str(i) for i in r.flag_ids],
                "drafted_entry_ids": [str(i) for i in r.drafted_entry_ids],
                "final_message": r.final_message,
                "notes": sb.notes,
                "latency_ms": r.total_latency_ms,
                "cost_usd": r.total_cost_usd,
            }
            for c, r, sb in results
        ],
    }
    await EvalRun.create(
        scenario_id=f"golden-set::{provider}",
        git_sha=git_sha,
        config={"provider": provider, "case_count": len(cases)},
        tool_selection_accuracy=agg.get("tool_selection_accuracy"),
        flagged_when_should_have=agg.get("flagged_when_should_have"),
        hallucination_rate=1.0 - agg.get("hallucination_rate", 1.0),  # store as fraction-fake
        schema_validity_rate=agg.get("schema_validity_rate"),
        reliability_rate=agg.get("reliability_rate"),
        cost_usd=sum(costs),
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        raw_results=raw,
    )


def _percentile(values: Iterable[float], pct: float) -> float:
    xs = sorted(values)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _print_summary(rows: list[tuple[str, dict[str, float]]]) -> None:
    print("\n=== summary ===")
    header = ["provider", "pass", "tools", "flag", "halluc", "validate", "reliab"]
    print("  " + "  ".join(f"{h:>10s}" for h in header))
    for prov, agg in rows:
        cols = [
            prov[:30],
            f"{agg.get('pass_rate', 0):.2f}",
            f"{agg.get('tool_selection_accuracy', 0):.2f}",
            f"{agg.get('flagged_when_should_have', 0):.2f}",
            f"{agg.get('hallucination_rate', 0):.2f}",
            f"{agg.get('schema_validity_rate', 0):.2f}",
            f"{agg.get('reliability_rate', 0):.2f}",
        ]
        print("  " + "  ".join(f"{c:>10s}" for c in cols))


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    await _bootstrap_db()

    cases = GOLDEN_SET
    if args.case:
        wanted = set(args.case.split(","))
        cases = [c for c in GOLDEN_SET if c.id in wanted]
        if not cases:
            print(f"no matching cases for {wanted}; available: {[c.id for c in GOLDEN_SET]}")
            return 2

    providers = [p.strip() for p in args.providers.split(",")] if args.providers else [None]
    git_sha = _git_sha()
    summary: list[tuple[str, dict[str, float]]] = []
    any_failed = False
    for provider in providers:
        results = await _run_provider(provider, cases, args.persist, git_sha)
        agg = aggregate([sb for _, _, sb in results])
        summary.append((provider or "default", agg))
        if any(not sb.passed for _, _, sb in results):
            any_failed = True

    _print_summary(summary)
    await Tortoise.close_connections()
    return 1 if any_failed else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Run the dexter-mini golden-set eval.")
    p.add_argument("--case", help="Run a single case (or comma-separated list of ids).")
    p.add_argument(
        "--providers",
        help="Comma-separated list of planner model strings to compare. "
             "Defaults to whatever DEXTER_LLM_PLANNER is set to.",
    )
    p.add_argument("--no-persist", dest="persist", action="store_false", default=True,
                   help="Don't write an EvalRun row.")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args()
    raise SystemExit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
