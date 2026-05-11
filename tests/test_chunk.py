"""Smoke tests for the chunking + scoring logic — the deterministic parts.

We don't test LLM nodes here; those are integration-tested by running
the agent against a real PR. The point of these tests is to prove the
non-LLM logic survives big inputs.
"""
from __future__ import annotations

import os

# Set required env vars BEFORE importing pr_agent so RuntimeConfig doesn't
# blow up when nodes call from_env(). Use unconditional assignment because
# some shells export the var as an empty string (which setdefault treats as
# "already set" — defeats the point).
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["MAX_TOKENS_PER_FILE_CHUNK"] = "200"
os.environ["MAX_LLM_CALLS_PER_RUN"] = "10"

from pr_agent.nodes.aggregate import _sensitive_path_bonus, _size_bonus  # noqa: E402
from pr_agent.nodes.chunk import chunk_node  # noqa: E402
from pr_agent.reviewers import owners_for_path, parse_codeowners  # noqa: E402
from pr_agent.state import FileChange, GraphState, PRMeta  # noqa: E402


def _state(files: list[FileChange]) -> GraphState:
    return GraphState(
        pr_url="https://github.com/o/r/pull/1",
        mode="conservative",
        pr_meta=PRMeta(
            owner="o", repo="r", number=1, title="t", author="a",
            base_ref="main", head_ref="dev", url="https://github.com/o/r/pull/1",
        ),
        files=files,
    )


def test_small_file_yields_single_whole_chunk():
    f = FileChange(path="src/a.py", status="modified", additions=2, deletions=0,
                   patch="@@ -1,1 +1,2 @@\n line\n+new\n")
    out = chunk_node(_state([f]))
    assert len(out["chunks"]) == 1
    assert out["chunks"][0].hunk_index is None
    assert out["truncated"] is False


def test_oversized_file_is_hunk_split():
    # Two valid hunks, each with realistic header counts so unidiff parses.
    pad = "x" * 600  # ~150 tokens; well over our 200-token chunk budget when both hunks are concat'd
    hunk1 = (
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        f" {pad}\n"
        "-old\n"
        "+new1\n"
        "+new2\n"
    )
    hunk2 = (
        "@@ -20,3 +21,4 @@\n"
        " line20\n"
        f" {pad}\n"
        "-old2\n"
        "+newA\n"
        "+newB\n"
    )
    f = FileChange(path="src/big.py", status="modified",
                   additions=4, deletions=2, patch=hunk1 + hunk2)
    out = chunk_node(_state([f]))
    # 200-token budget; combined patch is way over -> must hunk-split.
    assert len(out["chunks"]) == 2
    assert all(c.hunk_index is not None for c in out["chunks"])
    assert [c.hunk_index for c in out["chunks"]] == [0, 1]


def test_chunk_budget_cap_truncates_and_flags():
    # 20 files at the budget boundary, MAX_LLM_CALLS_PER_RUN=10 -> truncated
    files = [
        FileChange(path=f"src/f{i}.py", status="modified", additions=1, deletions=0,
                   patch="@@ -1,1 +1,2 @@\n line\n+x\n")
        for i in range(20)
    ]
    out = chunk_node(_state(files))
    assert out["truncated"] is True
    assert len(out["chunks"]) == 10


def test_binary_files_are_skipped_silently():
    f = FileChange(path="img/logo.png", status="modified", additions=0, deletions=0, patch=None)
    out = chunk_node(_state([f]))
    assert out["chunks"] == []


def test_sensitive_path_bonus_caps_at_25():
    paths = {
        ".env",
        "migrations/0001_init.sql",
        ".github/workflows/deploy.yml",
        "src/auth/login.py",
        "src/crypto/aes.py",
        "Dockerfile",
        "package.json",
    }
    # 7 hits * 5 = 35 -> capped at 25
    assert _sensitive_path_bonus(paths) == 25


def test_sensitive_path_bonus_none_clean():
    assert _sensitive_path_bonus({"src/utils/format.py", "README.md"}) == 0


def test_size_bonus_curve():
    assert _size_bonus(50, 50) == 0
    assert _size_bonus(300, 100) == 5
    assert _size_bonus(800, 300) == 10
    assert _size_bonus(2500, 500) == 18
    assert _size_bonus(5000, 5000) == 25


def test_codeowners_last_match_wins():
    text = """
# everything: @core-team
*       @alice
# python: @py-team
*.py    @bob
"""
    rules = parse_codeowners(text)
    assert owners_for_path(rules, "src/foo.py") == ["bob"]
    assert owners_for_path(rules, "README.md") == ["alice"]


def test_codeowners_directory_rule():
    text = "/src/auth/   @sec-team\n*.py    @py-team\n"
    rules = parse_codeowners(text)
    # last-match-wins: *.py beats /src/auth/ for a .py file inside it
    assert owners_for_path(rules, "src/auth/login.py") == ["py-team"]
    # non-py inside src/auth still goes to sec-team
    assert owners_for_path(rules, "src/auth/config.yml") == ["sec-team"]
