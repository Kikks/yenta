"""Centralised configuration.

All knobs in one place so reviewers (and the interview panel) can scan how
each mode behaves without grep-ing the codebase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# Note: we deliberately do NOT call load_dotenv() at import time. The
# CLI entrypoint (main.py) is the *only* place that loads .env, with
# override=True so values in .env beat shell-exported blanks (e.g.
# `ANTHROPIC_API_KEY=''`). Keeping config import side-effect-free means
# tests can set os.environ directly without .env stomping on them.

Mode = Literal["conservative", "aggressive"]


@dataclass(frozen=True)
class ModeProfile:
    """Concrete behaviour for a mode.

    `escalate_threshold` is compared against the deterministic risk score
    computed in `nodes/aggregate.py` (range 0-100).
    `review_event` is the GitHub review event used when escalating —
    conservative is louder (REQUEST_CHANGES); aggressive is softer (COMMENT).
    """

    escalate_threshold: int
    review_event_on_escalate: Literal["REQUEST_CHANGES", "COMMENT"]
    review_event_on_approve: Literal["APPROVE"]
    # How much of the LLM's commentary to surface back. Conservative writes
    # everything down; aggressive only surfaces medium+ severity findings.
    min_severity_to_comment: Literal["low", "medium", "high", "critical"]


MODE_PROFILES: dict[Mode, ModeProfile] = {
    "conservative": ModeProfile(
        # Was 25; raised to 50 so the agent feels self-sufficient by default.
        # Most PRs now auto-approve; escalation fires only on genuine risk.
        # Hard escalations (any critical finding, fork PR with findings)
        # still override this threshold — see decide.py.
        escalate_threshold=50,
        review_event_on_escalate="REQUEST_CHANGES",
        review_event_on_approve="APPROVE",
        min_severity_to_comment="low",
    ),
    "aggressive": ModeProfile(
        # Was 60; raised to 80. Aggressive should approve almost everything;
        # escalation here is "this is genuinely scary, get a human now".
        escalate_threshold=80,
        review_event_on_escalate="COMMENT",
        review_event_on_approve="APPROVE",
        min_severity_to_comment="medium",
    ),
}


# Paths the risk scorer treats as inherently sensitive. Hits here add a
# bonus to the deterministic risk score regardless of LLM findings.
SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.env",
    r"(^|/)secrets?(/|\.)",
    r"(^|/)auth(/|\.)",
    r"(^|/)crypto(/|\.)",
    r"(^|/)migrations?/",
    r"(^|/)Dockerfile$",
    r"(^|/)\.github/workflows/",
    r"(^|/)package\.json$",
    r"(^|/)requirements\.txt$",
    r"(^|/)pyproject\.toml$",
]


# Severity weights used by aggregate.py to compute the risk score.
SEVERITY_WEIGHTS = {
    "low": 2,
    "medium": 8,
    "high": 18,
    "critical": 40,
}


@dataclass(frozen=True)
class RuntimeConfig:
    github_token: str
    anthropic_api_key: str
    anthropic_model: str
    anthropic_triage_model: str
    triage_enabled: bool
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None
    max_tokens_per_file_chunk: int
    max_llm_calls_per_run: int

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        gh = os.environ.get("GITHUB_TOKEN")
        anth = os.environ.get("ANTHROPIC_API_KEY")
        if not gh:
            raise RuntimeError("GITHUB_TOKEN is required (see .env.example)")
        if not anth:
            raise RuntimeError("ANTHROPIC_API_KEY is required (see .env.example)")

        return cls(
            github_token=gh,
            anthropic_api_key=anth,
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            anthropic_triage_model=os.environ.get(
                "ANTHROPIC_TRIAGE_MODEL", "claude-haiku-4-5"
            ),
            triage_enabled=os.environ.get("TRIAGE_ENABLED", "1") not in ("0", "false", "False"),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
            langfuse_host=os.environ.get("LANGFUSE_HOST"),
            max_tokens_per_file_chunk=int(os.environ.get("MAX_TOKENS_PER_FILE_CHUNK", "6000")),
            max_llm_calls_per_run=int(os.environ.get("MAX_LLM_CALLS_PER_RUN", "80")),
        )
