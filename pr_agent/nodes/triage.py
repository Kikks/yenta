"""Triage node — cheap Haiku pass that decides which chunks need the
expensive Sonnet analyze.

Cost math (rough, with prompt caching on the analyze step):
  Triage call (Haiku):  ~$0.0006 per chunk
  Analyze call (Sonnet, cached): ~$0.010 per chunk

Triage pays off if it can skip even 1 in ~17 chunks. On real monorepo
PRs (lockfile bumps, generated stubs, whitespace cleanups) the skip
rate is often 30-60%, which is real money.

Design choices:
  - Triage uses a *separate* model (Haiku) but runs through the same
    LLM wrapper so its calls show up in Langfuse with the same shape.
    The same prompt-cache marker is applied to the Haiku system prompt
    too (~580 tokens — won't actually cache below 2048-tok minimum for
    Haiku, but the cache_control header is a no-op when too small).
  - Each chunk's triage decision + reason is written back onto the
    DiffChunk. analyze.py reads it and skips chunks marked `skip`.
  - Triage is *opt-out* via `TRIAGE_ENABLED=0`. Default is on.
  - If triage fails (timeout, JSON parse, API error) we fall back to
    `decision="review"` for that chunk — safe default.
"""
from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any

from ..config import RuntimeConfig
from ..llm import LLM, LLMBudgetExceeded, parse_json_block
from ..obs import observe
from ..state import DiffChunk, GraphState

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "triage_system.md"
_USER_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "triage_user.md"


def _format_user(template: str, chunk: DiffChunk, total_for_file: int) -> str:
    chunk_note = (
        "whole file"
        if chunk.hunk_index is None
        else f"hunk {chunk.hunk_index + 1} of {total_for_file}"
    )
    return Template(template).safe_substitute(
        file_path=chunk.file_path,
        file_status=chunk.file_status,
        chunk_note=chunk_note,
        diff=chunk.content,
    )


@observe(name="node.triage")
def triage_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()

    if not cfg.triage_enabled:
        log.info("triage disabled via env (TRIAGE_ENABLED=0); passing all chunks through")
        return {}

    system_prompt = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    user_template = _USER_PROMPT_PATH.read_text(encoding="utf-8")

    llm = LLM(cfg)

    per_file: dict[str, int] = {}
    for c in state.chunks:
        per_file[c.file_path] = per_file.get(c.file_path, 0) + 1

    # Build new chunks with triage decisions filled in. Pydantic models
    # are immutable by convention so we copy-with-updates.
    triaged: list[DiffChunk] = []
    skipped_count = 0

    for chunk in state.chunks:
        user_prompt = _format_user(user_template, chunk, per_file[chunk.file_path])

        try:
            raw = llm.complete(
                system=system_prompt,
                user=user_prompt,
                max_tokens=200,
                temperature=0.0,
                cache_system=True,
                model_override=cfg.anthropic_triage_model,
                metadata={
                    "node": "triage",
                    "file_path": chunk.file_path,
                    "hunk_index": chunk.hunk_index,
                    "pr_url": state.pr_url,
                    "mode": state.mode,
                },
            )
            parsed = parse_json_block(raw)
            decision = parsed.get("decision", "review")
            reason = parsed.get("reason", "")
            if decision not in ("review", "skip"):
                decision = "review"
        except (LLMBudgetExceeded, ValueError, Exception) as e:
            # Safe default: when triage fails, send the chunk through for
            # deep review. Never silently skip a chunk because of an
            # infra glitch.
            log.warning("triage failed for %s (%s); defaulting to review", chunk.file_path, e)
            decision = "review"
            reason = f"triage error: {e}"

        # Re-create the chunk with the decision attached. Pydantic v2
        # supports model_copy(update=...).
        triaged.append(
            chunk.model_copy(update={"triage_decision": decision, "triage_reason": reason})
        )
        if decision == "skip":
            skipped_count += 1

    skipped_report = [
        {"file_path": c.file_path, "hunk_index": c.hunk_index, "reason": c.triage_reason}
        for c in triaged
        if c.triage_decision == "skip"
    ]
    log.info(
        "triage: %d chunks -> %d to analyze, %d to skip (model=%s, calls=%d)",
        len(state.chunks),
        len(state.chunks) - skipped_count,
        skipped_count,
        cfg.anthropic_triage_model,
        llm.calls_made,
    )
    return {"chunks": triaged, "triage_skipped": skipped_report}
