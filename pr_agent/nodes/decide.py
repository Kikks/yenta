"""Decide node — two-line decision logic.

The brevity here is intentional. The whole point of conservative-vs-
aggressive is: one threshold per mode. If this function grew complex
the modes would stop being meaningfully different.
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

from ..config import MODE_PROFILES
from ..state import GraphState

log = logging.getLogger(__name__)


@observe(name="node.decide")
def decide_node(state: GraphState) -> dict[str, Any]:
    profile = MODE_PROFILES[state.mode]

    # Hard escalations: critical findings, or any from-fork PR with
    # findings, always escalate regardless of score/mode. Defensible
    # safety floor.
    has_critical = any(f.severity == "critical" for f in state.findings)
    fork_with_findings = bool(
        state.pr_meta and state.pr_meta.is_fork and state.findings
    )

    if has_critical or fork_with_findings or state.risk_score >= profile.escalate_threshold:
        decision = "escalate"
        why_parts = []
        if has_critical:
            why_parts.append("at least one critical finding")
        if fork_with_findings:
            why_parts.append("fork PR with findings (untrusted source)")
        if state.risk_score >= profile.escalate_threshold:
            why_parts.append(
                f"risk_score={state.risk_score} >= {state.mode} threshold "
                f"({profile.escalate_threshold})"
            )
        rationale = "Escalating to human review: " + "; ".join(why_parts) + "."
    else:
        decision = "auto_approve"
        rationale = (
            f"Auto-approving: risk_score={state.risk_score} < {state.mode} "
            f"threshold ({profile.escalate_threshold}), no critical findings, "
            "not a fork."
        )

    log.info("decision=%s :: %s", decision, rationale)
    return {"decision": decision, "decision_rationale": rationale}
