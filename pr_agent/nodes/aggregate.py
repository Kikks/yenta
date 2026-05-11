"""Aggregate node — deterministic risk scoring.

The LLM does perception (what's in the diff). Code does decision (what
to do about it). Keeping the threshold logic deterministic means:
  - the agent's behaviour is reproducible and auditable
  - mode tuning is one number, easy to defend in interview
  - we don't ask the LLM to be a judge — they're bad at it

Score is in [0, 100]+ (we cap at 100 only for display; raw can go higher
on monster PRs which we *want* to escalate aggressively).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..config import SENSITIVE_PATH_PATTERNS, SEVERITY_WEIGHTS
from ..obs import observe, update_span
from ..state import GraphState

log = logging.getLogger(__name__)


_SENSITIVE_RX = [re.compile(p) for p in SENSITIVE_PATH_PATTERNS]


def _sensitive_path_bonus(paths: set[str]) -> int:
    """+5 per sensitive path touched, capped at 25."""
    hits = sum(1 for p in paths if any(rx.search(p) for rx in _SENSITIVE_RX))
    return min(25, hits * 5)


def _size_bonus(additions: int, deletions: int) -> int:
    """Bigger PRs are riskier by default. Soft curve:
       <200 LOC -> 0, 200-800 -> 5, 800-2000 -> 10, 2000-5000 -> 18, >5000 -> 25"""
    loc = additions + deletions
    if loc < 200:
        return 0
    if loc < 800:
        return 5
    if loc < 2000:
        return 10
    if loc < 5000:
        return 18
    return 25


@observe(name="node.aggregate")
def aggregate_node(state: GraphState) -> dict[str, Any]:
    score_findings = sum(SEVERITY_WEIGHTS.get(f.severity, 0) for f in state.findings)
    paths = {f.file_path for f in state.files}
    score_paths = _sensitive_path_bonus(paths)
    score_size = _size_bonus(
        state.pr_meta.additions if state.pr_meta else 0,
        state.pr_meta.deletions if state.pr_meta else 0,
    )
    score_fork = 10 if (state.pr_meta and state.pr_meta.is_fork) else 0
    score_truncated = 10 if state.truncated else 0

    total = score_findings + score_paths + score_size + score_fork + score_truncated
    breakdown = {
        "findings": score_findings,
        "sensitive_paths": score_paths,
        "pr_size": score_size,
        "from_fork": score_fork,
        "analysis_truncated": score_truncated,
    }

    update_span(
        input={"finding_count": len(state.findings), "files": len(state.files)},
        output={"risk_score": total, "breakdown": breakdown},
    )

    log.info("risk_score=%d breakdown=%s", total, breakdown)
    return {"risk_score": total, "risk_breakdown": breakdown}
