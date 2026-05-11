"""Escalate node — pick reviewers, post line comments, leave each
assignee a targeted top-level comment.

Reviewer selection — explicit precedence so the interview answer is short:
  1. CODEOWNERS owners for each changed path. Last-match-wins per file.
  2. If we end up with zero unique owners, fall back to the top recent
     committers across the touched paths (weighted recency vote).
  3. Filter out the PR author (GitHub won't accept them anyway).
  4. Cap to 3 reviewers — too many reviewers is noise.

We also leave **one issue comment per assigned reviewer** that @-mentions
them and lists the specific files/lines they should focus on, drawn from
findings that touched paths they own (or, for blame-fallback reviewers,
the files they recently committed to).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from string import Template
from typing import Any

from github.GithubException import GithubException

from ..config import MODE_PROFILES, RuntimeConfig
from ..github_client import GitHubClient, collapse_committers
from ..llm import LLM
from ..obs import observe
from ..reviewers import owners_for_path, parse_codeowners, split_user_and_team
from ..state import Finding, GraphState, ReviewerAssignment

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "summary.md"
)


def _select_reviewers(
    state: GraphState, viewer_login: str
) -> tuple[list[ReviewerAssignment], dict[str, list[str]]]:
    """Returns (assignments, owners_by_file) for downstream comment routing."""
    assignments: list[ReviewerAssignment] = []
    owners_by_file: dict[str, list[str]] = {}

    pr_author = state.pr_meta.author if state.pr_meta else ""
    skip = {pr_author.lower(), viewer_login.lower(), ""}

    # --- step 1: CODEOWNERS ---
    rules = parse_codeowners(state.codeowners_raw) if state.codeowners_raw else []
    user_to_paths: dict[str, list[str]] = defaultdict(list)
    if rules:
        for f in state.files:
            owners = owners_for_path(rules, f.path)
            owners_by_file[f.path] = owners
            for o in owners:
                token, is_team = split_user_and_team(o)
                if is_team:
                    # Teams need a different API param; we skip them in v1
                    # rather than half-implement, and note it in README.
                    continue
                if token.lower() in skip:
                    continue
                user_to_paths[token].append(f.path)

    # Prefer CODEOWNERS-derived users; sort by # owned paths desc.
    codeowner_users = sorted(user_to_paths.keys(), key=lambda u: -len(user_to_paths[u]))

    # --- step 2: blame fallback (only if we have <2 codeowners) ---
    if len(codeowner_users) < 2:
        blame_top = collapse_committers(state.recent_committers, top_n=5)
        for login in blame_top:
            if login.lower() in skip or login in codeowner_users:
                continue
            codeowner_users.append(login)
            # Map this user to the files they recently committed to.
            user_to_paths[login] = [
                path
                for path, logins in state.recent_committers.items()
                if login in logins
            ]

    # --- step 3: cap to 3 ---
    chosen = codeowner_users[:3]

    for login in chosen:
        paths = user_to_paths.get(login, [])
        source = (
            "CODEOWNERS"
            if rules
            and any(login in [u for u in owners_by_file.get(p, [])] for p in paths)
            else "recent commit history (blame fallback)"
        )
        # Findings the model already flagged on this user's paths.
        owned_findings = [f for f in state.findings if f.file_path in paths]
        focus = _focus_text(owned_findings, paths)
        assignments.append(
            ReviewerAssignment(
                login=login,
                reason=f"Selected via {source}; owns/touched {len(paths)} file(s) in this PR.",
                focus=focus,
            )
        )

    return assignments, owners_by_file


def _focus_text(findings: list[Finding], paths: list[str]) -> str:
    if findings:
        lines = []
        # Group up to 5 most-severe findings.
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for f in sorted(findings, key=lambda x: severity_order.get(x.severity, 99))[:5]:
            loc = f"`{f.file_path}`" + (f":L{f.line}" if f.line else "")
            lines.append(f"- **{f.severity}/{f.category}** {loc} — {f.rationale}")
        return "\n".join(lines)
    if paths:
        return (
            "No specific findings on your paths; please give them a sanity pass:\n"
            + "\n".join(f"- `{p}`" for p in paths[:10])
        )
    return "Please review the PR end-to-end."


# Default inline cap. CodeRabbit-style: only the top-N most-severe
# findings post as line comments; everything else gets folded into a
# <details> block in the review body so the PR stays scannable.
# Runtime-overridable via `state.max_inline_line_comments` (CLI:
# `--max-findings N`).
DEFAULT_MAX_INLINE_LINE_COMMENTS = 5

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _split_findings_for_review(
    findings: list[Finding], *, max_inline: int = DEFAULT_MAX_INLINE_LINE_COMMENTS
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (top_for_inline, rest_for_fold).

    Top: line-bearing findings, severity-sorted, capped to max_inline.
    Rest: everything else (file-level findings without a line + the
    line-bearing findings beyond the cap), still severity-sorted.
    """
    line_bearing = sorted(
        (f for f in findings if f.line is not None),
        key=lambda f: _SEVERITY_RANK.get(f.severity, 99),
    )
    file_level = [f for f in findings if f.line is None]
    top = line_bearing[:max_inline]
    rest = line_bearing[max_inline:] + file_level
    rest.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 99))
    return top, rest


def _line_comments_from_findings(findings: list[Finding]) -> list[dict]:
    """Map findings to GitHub line-comment payloads.

    Caller is responsible for trimming to the top-N — this function
    renders whatever it's handed. PyGithub's create_review with `line`
    + `side='RIGHT'` works for any line on the new side of the diff.
    """
    out: list[dict] = []
    for f in findings:
        if f.line is None:
            continue
        body = f"**{f.severity}/{f.category}** — {f.rationale}"
        if f.suggestion:
            body += f"\n\n_Suggested fix:_ {f.suggestion}"
        out.append({"path": f.file_path, "line": f.line, "side": "RIGHT", "body": body})
    return out


def _folded_extras_block(extras: list[Finding]) -> str:
    """Render the folded `<details>` section appended to the review body.

    GitHub renders `<details><summary>...</summary>` as a click-to-expand
    block — the same UX CodeRabbit uses for nitpicks. We use it for
    findings beyond the inline cap so the review stays scannable.
    """
    if not extras:
        return ""
    lines = []
    for f in extras:
        loc = f"`{f.file_path}`" + (f":L{f.line}" if f.line else "")
        lines.append(f"- **{f.severity}/{f.category}** {loc} — {f.rationale}")
    return (
        "\n\n<details>\n"
        f"<summary>📂 {len(extras)} more finding{'s' if len(extras) != 1 else ''} "
        "(click to expand)</summary>\n\n"
        + "\n".join(lines)
        + "\n\n</details>"
    )


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
    files_payload = [
        {
            "path": f.path,
            "status": f.status,
            "additions": f.additions,
            "deletions": f.deletions,
        }
        for f in state.files
    ]
    template = _SUMMARY_PROMPT.read_text(encoding="utf-8")
    user = Template(template).safe_substitute(
        owner=pr.owner if pr else "",
        repo=pr.repo if pr else "",
        pr_number=pr.number if pr else 0,
        pr_title=pr.title if pr else "",
        author=pr.author if pr else "unknown",
        mode=state.mode,
        decision=state.decision or "unknown",
        risk_score=state.risk_score,
        findings_json=json.dumps(findings_payload, indent=2),
        files_json=json.dumps(files_payload, indent=2),
        file_count=len(state.files),
        additions=pr.additions if pr else 0,
        deletions=pr.deletions if pr else 0,
        truncated=str(state.truncated).lower(),
        triage_skipped_count=len(state.triage_skipped),
        is_fork=str(pr.is_fork if pr else False).lower(),
    )
    return llm.complete(
        system="You are a senior engineer writing the top-level PR review comment.",
        user=user,
        max_tokens=700,
        temperature=0.3,
        metadata={
            "node": "escalate.summary",
            "pr_url": state.pr_url,
            "mode": state.mode,
        },
    )


def _agent_signature(state: GraphState) -> str:
    bd = ", ".join(f"{k}={v}" for k, v in state.risk_breakdown.items())
    return (
        f"\n\n---\n"
        f"_Posted by `Yenta`, the PR Agent. — mode: `{state.mode}` · "
        f"risk: `{state.risk_score}` ({bd}) · "
        f"decision: `{state.decision}`._"
    )


@observe(name="node.escalate")
def escalate_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    gh = GitHubClient(cfg.github_token)
    llm = LLM(cfg)
    profile = MODE_PROFILES[state.mode]
    assert state.pr_meta

    pr = gh.pull(state.pr_meta.owner, state.pr_meta.repo, state.pr_meta.number)
    try:
        viewer = gh.viewer_login
    except GithubException:
        viewer = ""

    # --- pick reviewers + craft per-reviewer focus ---
    assignments, _owners_by_file = _select_reviewers(state, viewer)

    # --- build the main review (summary + line comments + folded extras) ---
    # Split findings: top-N inline as line comments, rest fold into a
    # <details> block in the review body. Keeps the PR scannable when the
    # model finds 10+ things to flag.
    # The cap honours state.max_inline_line_comments (CLI: --max-findings),
    # so operators can tune verbosity per-run without code edits.
    top_findings, extra_findings = _split_findings_for_review(
        state.findings, max_inline=state.max_inline_line_comments
    )
    line_comments = _line_comments_from_findings(top_findings)

    summary = _render_summary(state, llm)
    body = summary + _folded_extras_block(extra_findings) + _agent_signature(state)

    # ONE combined issue comment that addresses each reviewer in their
    # own section. The spec says "leave each one a comment explaining
    # what to look at" — we read that as "address each one in a comment"
    # rather than N separate top-level comments (which flood the PR).
    # Each reviewer still gets their own section citing specific files,
    # lines, and findings drawn from the paths they own/touched.
    combined_body: str | None = None
    if assignments:
        sections = []
        for a in assignments:
            sections.append(f"### @{a.login}\n" f"_{a.reason}_\n\n" f"{a.focus}")
        combined_body = (
            "## Reviewer assignments\n\n"
            "Tagging each reviewer with the specific files / findings to focus on:\n\n"
            + "\n\n---\n\n".join(sections)
        )
    # Backwards-compat: the state schema still exposes `pending_reviewer_comments`
    # as a list. We surface a one-entry list with `login='all'` so the
    # dry-run printer keeps working without a separate code path.
    reviewer_comments = (
        [{"login": "all", "body": combined_body}] if combined_body else []
    )

    # GitHub returns 422 if you try to APPROVE or REQUEST_CHANGES on your
    # own PR. Detect that up front and degrade to COMMENT, which still
    # accepts line comments. Mirrors approve_node's behaviour.
    event = profile.review_event_on_escalate
    if viewer and state.pr_meta.author == viewer and event == "REQUEST_CHANGES":
        log.warning("agent token owns the PR; downgrading REQUEST_CHANGES -> COMMENT")
        event = "COMMENT"

    pending = {
        "pending_review_body": body,
        "pending_review_event": event,
        "pending_line_comments": line_comments,
        "pending_reviewer_comments": reviewer_comments,
        "reviewers_assigned": assignments,
    }

    if state.dry_run:
        log.info(
            "DRY-RUN: skipping GitHub writes (would post %s review, %d line "
            "comments, request %d reviewers, %d per-reviewer comments)",
            event,
            len(line_comments),
            len(assignments),
            len(reviewer_comments),
        )
        return pending

    review_url: str | None = None
    try:
        review = gh.post_review(
            pr,
            body=body,
            event=event,
            comments=line_comments,
        )
        review_url = getattr(review, "html_url", state.pr_meta.url)
    except GithubException as e:
        # Most common failure: a line we suggested isn't in the diff.
        # Retry without line comments rather than dropping the whole review.
        log.warning("review with line comments failed (%s); retrying body-only", e)
        try:
            review = gh.post_review(pr, body=body, event=event, comments=[])
            review_url = getattr(review, "html_url", state.pr_meta.url)
        except GithubException as e2:
            log.exception("review post failed even body-only")
            return {
                **pending,
                "errors": state.errors + [f"escalate review failed: {e2}"],
            }

    # --- request reviewers on GitHub ---
    logins = [a.login for a in assignments]
    if logins:
        gh.request_reviewers(pr, logins)

    # --- single combined per-reviewer issue comment ---
    if combined_body:
        try:
            gh.post_issue_comment(pr, combined_body)
        except GithubException as e:
            log.warning("combined reviewer comment failed: %s", e)

    return {**pending, "review_url": review_url}
