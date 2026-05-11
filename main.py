"""CLI entrypoint.

Usage (per spec):
    python main.py https://github.com/<org>/<repo>/pull/<n> --mode conservative
    python main.py https://github.com/<org>/<repo>/pull/<n> --mode aggressive
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from dotenv import load_dotenv

from pr_agent.config import RuntimeConfig
from pr_agent.graph import build_graph
from pr_agent.obs import flush as lf_flush
from pr_agent.obs import observe, update_trace
from pr_agent.state import GraphState

log = logging.getLogger("pr_agent")


def _parse_pr_url(url: str) -> tuple[str, str, int]:
    # https://github.com/<owner>/<repo>/pull/<n>
    parts = url.rstrip("/").split("/")
    if len(parts) < 7 or parts[2] != "github.com" or parts[5] != "pull":
        raise ValueError(f"not a GitHub PR URL: {url}")
    try:
        return parts[3], parts[4], int(parts[6])
    except ValueError as e:
        raise ValueError(f"not a GitHub PR URL: {url}") from e


@observe(name="pr_review_agent.run")
def run(pr_url: str, mode: str, dry_run: bool = False) -> GraphState:
    owner, repo, number = _parse_pr_url(pr_url)
    update_trace(
        name=f"pr-review/{owner}/{repo}#{number}",
        tags=[f"mode:{mode}", f"repo:{owner}/{repo}"] + (["dry-run"] if dry_run else []),
        metadata={"pr_url": pr_url, "mode": mode, "dry_run": dry_run},
    )

    graph = build_graph()
    initial = GraphState(pr_url=pr_url, mode=mode, dry_run=dry_run)  # type: ignore[arg-type]
    final = graph.invoke(initial)
    # LangGraph may return a dict or a GraphState depending on version.
    return final if isinstance(final, GraphState) else GraphState.model_validate(final)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(override=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    p = argparse.ArgumentParser(
        description="Yenta — a LangGraph PR review agent. Matches PRs to reviewers.",
    )
    p.add_argument("pr_url", help="https://github.com/<org>/<repo>/pull/<n>")
    p.add_argument(
        "--mode",
        choices=["conservative", "aggressive"],
        required=True,
        help="conservative escalates eagerly; aggressive auto-approves more readily",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full pipeline (fetch + LLM + decision) but skip real "
            "GitHub writes. Prints exactly what would be posted."
        ),
    )
    args = p.parse_args(argv)

    # Validate env upfront so we fail fast before any LLM/GH calls.
    try:
        RuntimeConfig.from_env()
    except RuntimeError as e:
        print(f"[config] {e}", file=sys.stderr)
        return 2

    try:
        state = run(args.pr_url, args.mode, dry_run=args.dry_run)
    except Exception as e:  # surface a clean error; full trace is in logs
        log.exception("agent failed")
        print(f"[agent] failed: {e}", file=sys.stderr)
        return 1
    finally:
        lf_flush()

    _print_report(state, dry_run=args.dry_run)
    return 0


_BAR = "─" * 72


def _print_report(state: GraphState, *, dry_run: bool) -> None:
    print()
    print(_BAR)
    print(f"  Mode: {state.mode}{'  (DRY-RUN)' if dry_run else ''}")
    print(f"  Decision: {state.decision}")
    print(f"  Risk score: {state.risk_score}  {state.risk_breakdown}")
    if state.decision_rationale:
        print(f"  Rationale: {state.decision_rationale}")
    print(f"  Findings: {len(state.findings)} "
          f"(critical={sum(1 for f in state.findings if f.severity == 'critical')}, "
          f"high={sum(1 for f in state.findings if f.severity == 'high')}, "
          f"medium={sum(1 for f in state.findings if f.severity == 'medium')}, "
          f"low={sum(1 for f in state.findings if f.severity == 'low')})")
    if state.triage_skipped:
        print(f"  Triage-skipped: {len(state.triage_skipped)} chunk(s) (cheap pass declined deep review)")
        for sk in state.triage_skipped[:5]:
            print(f"     - {sk['file_path']} :: {sk['reason']}")
        if len(state.triage_skipped) > 5:
            print(f"     ... ({len(state.triage_skipped) - 5} more)")
    if state.truncated:
        print("  TRUNCATED: hit MAX_LLM_CALLS_PER_RUN")
    print(_BAR)

    if state.pending_review_body:
        print()
        print(f"  -> would post review with event={state.pending_review_event}")
        print(_BAR)
        for line in state.pending_review_body.splitlines():
            print(f"  | {line}")
        print(_BAR)

    if state.pending_line_comments:
        print()
        print(f"  -> would post {len(state.pending_line_comments)} line comments:")
        for c in state.pending_line_comments[:10]:
            print(f"     {c['path']}:L{c['line']}")
            for line in c["body"].splitlines():
                print(f"        | {line}")
        if len(state.pending_line_comments) > 10:
            print(f"     ... ({len(state.pending_line_comments) - 10} more)")

    if state.reviewers_assigned:
        print()
        print(f"  -> would request {len(state.reviewers_assigned)} reviewers:")
        for r in state.reviewers_assigned:
            print(f"     @{r.login} :: {r.reason}")

    if state.pending_reviewer_comments:
        print()
        print(f"  -> would post {len(state.pending_reviewer_comments)} per-reviewer comments:")
        for rc in state.pending_reviewer_comments:
            print(f"     @ {rc['login']}")
            for line in rc["body"].splitlines():
                print(f"        | {line}")

    if state.review_url:
        print()
        print(f"  review URL: {state.review_url}")

    if state.errors:
        print()
        print("  ERRORS:")
        for e in state.errors:
            print(f"    - {e}")


if __name__ == "__main__":
    raise SystemExit(main())
