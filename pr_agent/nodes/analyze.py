"""Analyze node — one LLM call per chunk, structured findings out.

Fan-out is sequential, not concurrent. Reasons:
  - Anthropic per-key rate limits make naive parallelism unreliable on a
    monorepo PR. The budget cap in LLM already protects against runaway
    cost; sequential keeps us in friendly territory.
  - Sequential traces in Langfuse are dramatically easier to read in the
    interview demo.
If this became a real product, the right move is bounded concurrency
(asyncio.Semaphore, ~4-8 in-flight) — flagged in README future work.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Template
from typing import Any

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


@observe(name="node.analyze")
def analyze_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    llm = LLM(cfg)
    system_prompt, user_template = _load_prompts()

    # Count chunks per file so we can label "hunk X of Y" in the prompt.
    per_file: dict[str, int] = {}
    for c in state.chunks:
        per_file[c.file_path] = per_file.get(c.file_path, 0) + 1

    findings: list[Finding] = []
    errors: list[str] = []

    for chunk in state.chunks:
        # Triage said skip — log it and move on. The skip is already
        # captured in state.triage_skipped (set by triage_node) so the
        # final review body can honestly disclose what wasn't analyzed.
        # Keeping these OUT of state.findings means they don't inflate
        # the risk score (important on monorepo PRs where 50 lockfile
        # bumps would otherwise force escalation).
        if chunk.triage_decision == "skip":
            log.info(
                "analyze: skipping %s by triage (%s)",
                chunk.file_path, chunk.triage_reason or "no reason given",
            )
            continue

        user_prompt = _format_user_prompt(
            user_template, state, chunk, per_file[chunk.file_path]
        )

        try:
            raw = llm.complete(
                system=system_prompt,
                user=user_prompt,
                max_tokens=1500,
                temperature=0.1,
                # Cache the (large, static) system prompt. First call in
                # the run pays cache-write (~1.25x normal); every call
                # after pays cache-read (~0.1x normal) for the cached
                # portion. Big win on per-file fan-out.
                cache_system=True,
                metadata={
                    "node": "analyze",
                    "file_path": chunk.file_path,
                    "hunk_index": chunk.hunk_index,
                    "pr_url": state.pr_url,
                    "mode": state.mode,
                },
            )
        except LLMBudgetExceeded as e:
            log.warning("budget exceeded: %s", e)
            errors.append(str(e))
            break

        try:
            parsed = parse_json_block(raw)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("could not parse analyze output for %s: %s", chunk.file_path, e)
            errors.append(f"json parse failed for {chunk.file_path}: {e}")
            continue

        for item in parsed.get("findings", []):
            try:
                findings.append(
                    Finding(
                        file_path=chunk.file_path,
                        line=item.get("line"),
                        severity=item.get("severity", "low"),
                        category=item.get("category", "style"),
                        rationale=item.get("rationale", ""),
                        suggestion=item.get("suggestion"),
                    )
                )
            except Exception as e:  # malformed finding from the model
                log.warning("dropped malformed finding from %s: %s", chunk.file_path, e)

    # Surface skipped binary / no-patch files as low-severity informational
    # findings so the reviewer knows we didn't pretend to review them.
    seen_paths = {f.path for f in state.files if f.patch}
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
        # ensure no dupe with patch-having files
        _ = seen_paths

    log.info(
        "analyze: %d chunks -> %d findings (%d errors); llm calls=%d",
        len(state.chunks), len(findings), len(errors), llm.calls_made,
    )
    return {"findings": findings, "errors": state.errors + errors}
