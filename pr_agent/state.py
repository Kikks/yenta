"""Graph state — the single object every node reads from and writes to.

LangGraph is happy with TypedDict or Pydantic. We use Pydantic because:
  - validation is cheap insurance on a 6-hr build
  - findings are easier to serialise into Langfuse traces
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
Mode = Literal["conservative", "aggressive"]
Decision = Literal["auto_approve", "escalate"]


class PRMeta(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: Optional[str] = None
    author: str
    base_ref: str
    head_ref: str
    is_fork: bool = False
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    url: str


class FileChange(BaseModel):
    path: str
    status: str  # added / modified / removed / renamed
    additions: int
    deletions: int
    patch: Optional[str] = None  # the unified diff for this file (may be None for huge binaries)


class DiffChunk(BaseModel):
    """A self-contained chunk handed to the analyze node.

    Either covers a whole file (`hunk_index is None`) or one hunk of a
    too-large file (`hunk_index >= 0`).
    """

    file_path: str
    file_status: str
    hunk_index: Optional[int] = None
    content: str  # the unified-diff text we'll send to the LLM
    approx_tokens: int
    # Set by the triage node. Default "review" so chunks created before
    # triage (e.g. in tests) still get analyzed.
    triage_decision: Literal["review", "skip"] = "review"
    triage_reason: Optional[str] = None


class Finding(BaseModel):
    file_path: str
    line: Optional[int] = None  # head-side line number; None for file-level findings
    severity: Severity
    category: str  # e.g. "security", "correctness", "perf", "style", "tests"
    rationale: str  # what the model saw and why it matters
    suggestion: Optional[str] = None  # concrete fix, if the model proposed one


class ReviewerAssignment(BaseModel):
    login: str
    reason: str  # why this person — owned paths, recent commits, etc.
    focus: str  # what to look at, drawn from findings


class GraphState(BaseModel):
    """The state object that flows through the LangGraph."""

    # --- inputs ---
    pr_url: str
    mode: Mode
    # Dry-run: do everything EXCEPT real GitHub writes. The would-be
    # review body, line comments, and reviewer assignments are still
    # populated so we can print them — but nothing posts.
    dry_run: bool = False

    # --- populated by fetch node ---
    pr_meta: Optional[PRMeta] = None
    files: list[FileChange] = Field(default_factory=list)
    codeowners_raw: Optional[str] = None
    recent_committers: dict[str, list[str]] = Field(default_factory=dict)  # path -> logins

    # --- populated by chunk node ---
    chunks: list[DiffChunk] = Field(default_factory=list)
    truncated: bool = False  # we hit the safety cap on LLM calls

    # --- populated by triage node ---
    # List of (file_path, reason) tuples for chunks the triage Haiku decided
    # weren't worth a deep Sonnet pass. Tracked separately from findings so
    # they don't inflate the risk score on monorepo PRs.
    triage_skipped: list[dict] = Field(default_factory=list)

    # --- populated by analyze node ---
    findings: list[Finding] = Field(default_factory=list)

    # --- populated by aggregate node ---
    risk_score: int = 0
    risk_breakdown: dict[str, int] = Field(default_factory=dict)

    # --- populated by decide node ---
    decision: Optional[Decision] = None
    decision_rationale: Optional[str] = None

    # --- populated by approve/escalate nodes ---
    reviewers_assigned: list[ReviewerAssignment] = Field(default_factory=list)
    review_url: Optional[str] = None
    # The exact payloads we would (or did) post to GitHub. Populated in
    # both dry-run and live runs so the CLI can print them for review.
    pending_review_body: Optional[str] = None
    pending_review_event: Optional[str] = None  # APPROVE / REQUEST_CHANGES / COMMENT
    pending_line_comments: list[dict] = Field(default_factory=list)
    pending_reviewer_comments: list[dict] = Field(default_factory=list)  # [{login, body}]

    # --- diagnostics ---
    errors: list[str] = Field(default_factory=list)
