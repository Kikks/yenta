"""Fetch node — pull everything we need from GitHub in one place.

We deliberately do all GitHub reads up-front, rather than letting later
nodes lazy-fetch. Reasons:
  - keeps the graph honest: every node after this works on local state
  - one place to reason about rate limits / pagination
  - easier to trace: the Langfuse span for "fetch" has every GH call
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

from ..config import RuntimeConfig
from ..github_client import GitHubClient
from ..state import GraphState

log = logging.getLogger(__name__)


def _parse_pr_url(url: str) -> tuple[str, str, int]:
    parts = url.rstrip("/").split("/")
    return parts[3], parts[4], int(parts[6])


@observe(name="node.fetch")
def fetch_node(state: GraphState) -> dict[str, Any]:
    cfg = RuntimeConfig.from_env()
    gh = GitHubClient(cfg.github_token)
    owner, repo, number = _parse_pr_url(state.pr_url)

    meta, pr = gh.load_pr_meta(owner, repo, number)
    files = gh.load_changed_files(pr)

    # Only fetch CODEOWNERS once, against the PR's base ref (what would-be
    # merged into) — that's the source of truth for ownership.
    codeowners = gh.load_codeowners(owner, repo, meta.base_ref)

    # For reviewer fallback we want recent committers, but only for the
    # files the PR touched. Cap to the 25 largest-impact files so a
    # monorepo PR doesn't fan out across thousands of paths.
    impactful = sorted(files, key=lambda f: f.additions + f.deletions, reverse=True)[:25]
    recent = gh.recent_committers_for_paths(
        owner, repo, [f.path for f in impactful], per_path_limit=5
    )

    log.info(
        "fetched PR #%d (%s/%s): %d files, +%d/-%d, fork=%s",
        meta.number, meta.owner, meta.repo, meta.changed_files,
        meta.additions, meta.deletions, meta.is_fork,
    )

    return {
        "pr_meta": meta,
        "files": files,
        "codeowners_raw": codeowners,
        "recent_committers": recent,
    }
