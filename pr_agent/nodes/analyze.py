"""Analyze node — one LLM call per chunk, structured findings out.

Fan-out is *bounded-concurrent* via `concurrent.futures.ThreadPoolExecutor`.
Why bounded threads (not asyncio):
  - Anthropic SDK is sync. Threads add concurrency at zero refactor cost;
    GIL is released on the underlying HTTP I/O so we get real parallelism.
  - LangGraph nodes are sync. Going async would push async/await through
    every node and the graph executor — a far bigger change.
  - `ANALYZE_CONCURRENCY` env knob (default 4, capped at 8 in config) lets
    operators tune for their rate limits. Set to 1 for the old sequential
    behaviour.

Thread-safety guarantees relied upon here:
  - `LLM._calls_made` is incremented under a `threading.Lock` (see llm.py),
    so the budget cap holds even under N concurrent workers.
  - Worker functions are PURE — they take inputs and return a tuple; they
    never mutate shared state. Only the main thread aggregates results.
  - Langfuse / OTel context propagates from `node.analyze` into worker
    threads via `contextvars.copy_context()` + `ctx.run(...)`. Without
    this, generation spans created in workers would be orphaned (no
    parent), producing broken Langfuse traces.
"""
from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template
from typing import Any, Optional

from ..config import RuntimeConfig
from ..llm import LLM, LLMBudgetExceeded, parse_json_block
from ..obs import observe
from ..state import DiffChunk, Finding, GraphState

log = logging.getLogger(__name__)

# Prompts are split system/user so the system half can be cached.
# Anthropic prompt caching requires >=1024 cached tokens (Sonnet 4+);
# analyze_system.md is intentionally padded with concrete do/don't
# examples to clear that threshold AND improve review quality.
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "analyze_system.md"
_USER_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "analyze_user.md"


def _load_prompts() -> tuple[str, str]:
    return (
        _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        _USER_PROMPT_PATH.read_text(encoding="utf-8"),
    )


def _format_user_prompt(template: str, state: GraphState, chunk: DiffChunk, total_for_file: int) -> str:
    pr = state.pr_meta
    chunk_note = (
        "whole file"
        if chunk.hunk_index is None
        else f"hunk {chunk.hunk_index + 1} of {total_for_file}"
    )
    return Template(template).safe_substitute(
        owner=pr.owner if pr else "",
        repo=pr.repo if pr else "",
        pr_title=(pr.title if pr else "")[:200],
        pr_body=((pr.body or "")[:1500] if pr else ""),
        file_path=chunk.file_path,
        file_status=chunk.file_status,
        chunk_note=chunk_note,
        diff=chunk.content,
    )


# Sentinel returned by a worker when the budget cap was hit. We surface
# this distinctly from "the model returned bad JSON" because the caller
# wants to count budget hits in state.errors with a clear prefix.
_BUDGET_PREFIX = "BUDGET_EXCEEDED:"


def _analyze_one_chunk(
    chunk: DiffChunk,
    user_prompt: str,
    system_prompt: str,
    llm: LLM,
    state_pr_url: str,
    state_mode: str,
) -> tuple[DiffChunk, Optional[str], Optional[str]]:
    """Worker: format the prompt and call the LLM.

    Returns ``(chunk, raw_text_or_None, error_or_None)``. NEVER raises —
    all exceptions are caught and returned as the third element so the
    main thread can aggregate them. This contract is critical: if a
    worker raises, ThreadPoolExecutor swallows the exception until
    .result() is called, and partial failures become silent loss.

    The function takes only its dependencies as parameters (no module
    globals, no shared mutable state) so it's safe to call from any
    thread.
    """
    try:
        raw = llm.complete(
            system=system_prompt,
            user=user_prompt,
            max_tokens=1500,
            temperature=0.1,
            # Cache the (large, static) system prompt. First call pays
            # cache-write (~1.25x); every call after pays cache-read
            # (~0.1x) for the cached portion. Big win on per-file fan-out.
            cache_system=True,
            metadata={
                "node": "analyze",
                "file_path": chunk.file_path,
                "hunk_index": chunk.hunk_index,
                "pr_url": state_pr_url,
                "mode": state_mode,
            },
        )
        return chunk, raw, None
    except LLMBudgetExceeded as e:
        return chunk, None, f"{_BUDGET_PREFIX}{e}"
    except Exception as e:  # any other Anthropic/network/etc. failure
        return chunk, None, f"{chunk.file_path}: {e}"


def _findings_from_raw(raw: str, file_path: str) -> tuple[list[Finding], Optional[str]]:
    """Parse a raw LLM JSON response into Finding objects.

    Returns ``(findings, error_or_None)``. Pure function; safe to call
    in either thread, kept in the main thread today because it doesn't
    do I/O and avoids serialising Pydantic exceptions across threads.
    """
    try:
        parsed = parse_json_block(raw)
    except (json.JSONDecodeError, ValueError) as e:
        return [], f"json parse failed for {file_path}: {e}"

    out: list[Finding] = []
    for item in parsed.get("findings", []):
        try:
            out.append(
                Finding(
                    file_path=file_path,
                    line=item.get("line"),
                    severity=item.get("severity", "low"),
                    category=item.get("category", "style"),
                    rationale=item.get("rationale", ""),
                    suggestion=item.get("suggestion"),
                )
            )
        except Exception as e:  # malformed finding from the model
            log.warning("dropped malformed finding from %s: %s", file_path, e)
    return out, None


def _finding_sort_key(f: Finding) -> tuple[str, int]:
    """Stable ordering for findings — needed because `as_completed`
    returns in completion (i.e. wall-clock) order, not submission order.
    We sort by (file_path, line) so re-runs with identical model output
    produce identical state. Risk score is order-independent today, but
    deterministic ordering makes downstream review output reproducible
    too."""
    return (f.file_path, f.line if f.line is not None else 0)


@observe(name="node.analyze")
def analyze_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    llm = LLM(cfg)
    system_prompt, user_template = _load_prompts()

    # Count chunks per file so we can label "hunk X of Y" in the prompt.
    per_file: dict[str, int] = {}
    for c in state.chunks:
        per_file[c.file_path] = per_file.get(c.file_path, 0) + 1

    # Filter to chunks we'll actually analyze. Triage-skipped chunks are
    # already captured in state.triage_skipped (set by triage_node) and
    # bypassing them here saves a worker dispatch each.
    to_analyze: list[DiffChunk] = []
    for chunk in state.chunks:
        if chunk.triage_decision == "skip":
            log.info(
                "analyze: skipping %s by triage (%s)",
                chunk.file_path, chunk.triage_reason or "no reason given",
            )
            continue
        to_analyze.append(chunk)

    findings: list[Finding] = []
    errors: list[str] = []

    # Pre-format user prompts in the main thread (cheap, deterministic)
    # so workers don't share the GraphState — they get a flat string.
    payloads: list[tuple[DiffChunk, str]] = [
        (chunk, _format_user_prompt(user_template, state, chunk, per_file[chunk.file_path]))
        for chunk in to_analyze
    ]

    if not payloads:
        log.info("analyze: nothing to analyze after triage; 0 LLM calls")
    else:
        # OTel/Langfuse span context propagation. Each worker needs its
        # OWN snapshot of the current Context — sharing one Context
        # across concurrent .run() calls raises:
        #   RuntimeError: cannot enter context: ... is already entered
        # because Context.run() locks the context while executing.
        # contextvars.copy_context() is cheap; one per submit is fine.
        workers = cfg.analyze_concurrency

        log.info(
            "analyze: dispatching %d chunks with concurrency=%d",
            len(payloads), workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(
                    contextvars.copy_context().run,
                    _analyze_one_chunk,
                    chunk,
                    user_prompt,
                    system_prompt,
                    llm,
                    state.pr_url,
                    state.mode,
                )
                for chunk, user_prompt in payloads
            ]
            for fut in as_completed(futures):
                # .result() should never raise because the worker
                # catches everything. We still guard defensively — a
                # bug in the worker shouldn't take down the whole node.
                try:
                    chunk, raw, err = fut.result()
                except Exception as e:  # pragma: no cover — defensive
                    log.exception("worker raised unexpectedly")
                    errors.append(f"unexpected worker exception: {e}")
                    continue

                if err is not None:
                    if err.startswith(_BUDGET_PREFIX):
                        budget_msg = err[len(_BUDGET_PREFIX):]
                        log.warning("budget exceeded: %s", budget_msg)
                        errors.append(budget_msg)
                    else:
                        log.warning("analyze worker error: %s", err)
                        errors.append(err)
                    continue

                if raw is None:  # belt-and-suspenders
                    continue

                chunk_findings, parse_err = _findings_from_raw(raw, chunk.file_path)
                if parse_err:
                    log.warning(parse_err)
                    errors.append(parse_err)
                findings.extend(chunk_findings)

    # Surface binary / no-patch files as low-severity informational findings
    # so the reviewer knows we didn't pretend to review them. Done in main
    # thread; no LLM call needed.
    for f in state.files:
        if not f.patch and (f.additions + f.deletions) > 0:
            findings.append(
                Finding(
                    file_path=f.path,
                    line=None,
                    severity="low",
                    category="style",
                    rationale=(
                        "Binary or suppressed-diff file; this agent skipped LLM review "
                        "for it. A human should glance at the change."
                    ),
                    suggestion=None,
                )
            )

    # Deterministic ordering — see docstring on _finding_sort_key.
    findings.sort(key=_finding_sort_key)

    log.info(
        "analyze: %d chunks (%d analyzed) -> %d findings (%d errors); llm calls=%d",
        len(state.chunks), len(to_analyze), len(findings), len(errors), llm.calls_made,
    )
    return {"findings": findings, "errors": state.errors + errors}
