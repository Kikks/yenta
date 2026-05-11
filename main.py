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

try:
    from langfuse.decorators import langfuse_context, observe
    _LANGFUSE_OK = True
except Exception:  # pragma: no cover
    _LANGFUSE_OK = False

    def observe(*_a, **_k):  # type: ignore[no-redef]
        def deco(fn):
            return fn

        return deco

    class _NullCtx:  # type: ignore[no-redef]
        def update_current_trace(self, **_kwargs):
            return

        def flush(self) -> None:
            return

    langfuse_context = _NullCtx()  # type: ignore[assignment]

from pr_agent.config import RuntimeConfig
from pr_agent.graph import build_graph
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
def run(pr_url: str, mode: str) -> GraphState:
    owner, repo, number = _parse_pr_url(pr_url)
    langfuse_context.update_current_trace(
        name=f"pr-review/{owner}/{repo}#{number}",
        tags=[f"mode:{mode}", f"repo:{owner}/{repo}"],
        metadata={"pr_url": pr_url, "mode": mode},
    )

    graph = build_graph()
    initial = GraphState(pr_url=pr_url, mode=mode)  # type: ignore[arg-type]
    final = graph.invoke(initial)
    # LangGraph may return a dict or a GraphState depending on version.
    return final if isinstance(final, GraphState) else GraphState.model_validate(final)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    p = argparse.ArgumentParser(description="PR Review Agent")
    p.add_argument("pr_url", help="https://github.com/<org>/<repo>/pull/<n>")
    p.add_argument(
        "--mode",
        choices=["conservative", "aggressive"],
        required=True,
        help="conservative escalates eagerly; aggressive auto-approves more readily",
    )
    args = p.parse_args(argv)

    # Validate env upfront so we fail fast before any LLM/GH calls.
    try:
        RuntimeConfig.from_env()
    except RuntimeError as e:
        print(f"[config] {e}", file=sys.stderr)
        return 2

    try:
        state = run(args.pr_url, args.mode)
    except Exception as e:  # surface a clean error; full trace is in logs
        log.exception("agent failed")
        print(f"[agent] failed: {e}", file=sys.stderr)
        return 1
    finally:
        if _LANGFUSE_OK:
            try:
                langfuse_context.flush()
            except Exception:
                pass

    print()
    print(f"decision: {state.decision}")
    print(f"risk_score: {state.risk_score}")
    print(f"review_url: {state.review_url}")
    if state.reviewers_assigned:
        print("reviewers:")
        for r in state.reviewers_assigned:
            print(f"  - @{r.login} :: {r.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
