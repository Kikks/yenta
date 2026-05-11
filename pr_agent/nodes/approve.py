"""Approve node — leave a summary comment and APPROVE.

We deliberately don't approve our own PRs (GitHub returns 422). If the
agent's token belongs to the PR author, we degrade gracefully to a
COMMENT review so the demo still shows a real GitHub write.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from github.GithubException import GithubException

from ..config import MODE_PROFILES, RuntimeConfig
from ..github_client import GitHubClient
from ..llm import LLM
from ..obs import observe
from ..state import GraphState

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = Path(__file__).resolve().parent.parent.parent / "prompts" / "summary.md"


def _render_summary(state: GraphState, llm: LLM) -> str:
    pr = state.pr_meta
    findings_payload = [
        {
            "file": f.file_path,
            "line": f.line,
            "severity": f.severity,
            "category": f.category,
            "rationale": f.rationale,
        }
        for f in state.findings
    ]
    template = _SUMMARY_PROMPT.read_text(encoding="utf-8")
    import json
    user = template.format(
        owner=pr.owner if pr else "",
        repo=pr.repo if pr else "",
        pr_number=pr.number if pr else 0,
        pr_title=pr.title if pr else "",
        author=pr.author if pr else "unknown",
        mode=state.mode,
        decision=state.decision or "unknown",
        risk_score=state.risk_score,
        findings_json=json.dumps(findings_payload, indent=2),
        file_count=len(state.files),
        additions=pr.additions if pr else 0,
        deletions=pr.deletions if pr else 0,
        truncated=str(state.truncated).lower(),
        is_fork=str(pr.is_fork if pr else False).lower(),
    )
    return llm.complete(
        system="You are a senior engineer writing the top-level PR review comment.",
        user=user,
        max_tokens=600,
        temperature=0.3,
        metadata={
            "node": "approve.summary",
            "pr_url": state.pr_url,
            "mode": state.mode,
        },
    )


def _agent_signature(state: GraphState) -> str:
    return (
        f"\n\n---\n"
        f"_Posted by the PR Review Agent — mode: `{state.mode}` · "
        f"risk: `{state.risk_score}` · "
        f"decision: `{state.decision}`._"
    )


@observe(name="node.approve")
def approve_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    gh = GitHubClient(cfg.github_token)
    llm = LLM(cfg)
    profile = MODE_PROFILES[state.mode]
    assert state.pr_meta

    pr = gh.pull(state.pr_meta.owner, state.pr_meta.repo, state.pr_meta.number)

    body = _render_summary(state, llm) + _agent_signature(state)

    # GitHub forbids approving your own PR. Degrade to COMMENT so the
    # demo run still produces a visible write.
    event = profile.review_event_on_approve
    try:
        viewer = gh.viewer_login
    except GithubException:
        viewer = ""
    if viewer and state.pr_meta.author == viewer:
        log.warning("agent token owns the PR; downgrading APPROVE -> COMMENT")
        event = "COMMENT"

    try:
        review = gh.post_review(pr, body=body, event=event, comments=[])
        url = getattr(review, "html_url", state.pr_meta.url)
    except GithubException as e:
        log.exception("approve review post failed")
        return {"errors": state.errors + [f"approve failed: {e}"]}

    return {"review_url": url}
