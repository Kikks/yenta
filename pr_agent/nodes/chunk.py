"""Chunk node — split the PR diff into LLM-sized pieces.

Strategy (in order):
  1. One chunk per file. The LLM sees the whole file diff as a unit —
     this is the highest-signal way to reason about a change.
  2. If a single file's diff would blow the per-chunk token budget,
     split it into hunks using `unidiff`. Each hunk becomes its own
     chunk, tagged with hunk_index so analyze.py knows it's a partial
     view.
  3. Hard cap on total chunks: if we'd exceed MAX_LLM_CALLS_PER_RUN,
     truncate and set state.truncated = True so the README/Final
     comment honestly reports it.

Files without a patch (binary, generated, GitHub-suppressed) are noted
as findings later but skipped here.
"""
from __future__ import annotations

import logging
from typing import Any

try:
    from langfuse.decorators import observe
except Exception:  # pragma: no cover
    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

from unidiff import PatchSet

from ..config import RuntimeConfig
from ..llm import approx_token_count
from ..state import DiffChunk, GraphState

log = logging.getLogger(__name__)


def _hunks_from_patch(file_path: str, patch: str) -> list[str]:
    """Return one unified-diff string per hunk in the file."""
    # PatchSet expects a full unified diff with file headers; we synthesize
    # minimal ones so unidiff parses it.
    synthetic = f"--- a/{file_path}\n+++ b/{file_path}\n{patch}"
    try:
        ps = PatchSet(synthetic)
    except Exception as e:
        log.warning("unidiff parse failed for %s (%s); falling back to whole patch", file_path, e)
        return [patch]
    out: list[str] = []
    for pf in ps:
        for hunk in pf:
            out.append(str(hunk))
    return out or [patch]


@observe(name="node.chunk")
def chunk_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    chunks: list[DiffChunk] = []
    truncated = False

    for f in state.files:
        if not f.patch:
            # Binary / suppressed / too-large from GitHub. We surface this
            # as a finding in the aggregate stage rather than spending an
            # LLM call on something we can't read.
            continue

        whole_tokens = approx_token_count(f.patch)
        if whole_tokens <= cfg.max_tokens_per_file_chunk:
            chunks.append(
                DiffChunk(
                    file_path=f.path,
                    file_status=f.status,
                    hunk_index=None,
                    content=f.patch,
                    approx_tokens=whole_tokens,
                )
            )
        else:
            # Split into hunks. If a *single hunk* is still too big we
            # send it anyway and let the LLM's own context-handling deal
            # with it — better than dropping the change.
            for i, h in enumerate(_hunks_from_patch(f.path, f.patch)):
                chunks.append(
                    DiffChunk(
                        file_path=f.path,
                        file_status=f.status,
                        hunk_index=i,
                        content=h,
                        approx_tokens=approx_token_count(h),
                    )
                )

        if len(chunks) >= cfg.max_llm_calls_per_run:
            log.warning(
                "hit MAX_LLM_CALLS_PER_RUN=%d during chunking; truncating",
                cfg.max_llm_calls_per_run,
            )
            chunks = chunks[: cfg.max_llm_calls_per_run]
            truncated = True
            break

    log.info("chunked %d files into %d analyze-able chunks (truncated=%s)",
             len(state.files), len(chunks), truncated)
    return {"chunks": chunks, "truncated": truncated}
