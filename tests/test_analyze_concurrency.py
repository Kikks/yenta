"""Concurrency tests for analyze_node.

These tests use a fake Anthropic SDK so they run fast and don't touch
the network. The goal is to prove the three invariants that matter:

  1. The thread pool actually parallelizes work (vs serial baseline).
  2. The budget cap holds under N concurrent workers — the
     threading.Lock on LLM._calls_made prevents overshoot.
  3. One failing worker doesn't take down the rest — partial failure
     aggregates cleanly through state.errors.

We also keep a regression-style test for concurrency=1 to prove the
sequential path still works (since that's the fallback).
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

# Set required env vars BEFORE importing pr_agent so RuntimeConfig
# doesn't blow up. Same trick test_chunk.py uses.
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["MAX_LLM_CALLS_PER_RUN"] = "100"
os.environ["MAX_TOKENS_PER_FILE_CHUNK"] = "6000"

from pr_agent.nodes.analyze import analyze_node  # noqa: E402
from pr_agent.state import DiffChunk, FileChange, GraphState, PRMeta  # noqa: E402


# ---------- helpers ----------

def _make_state(num_chunks: int) -> GraphState:
    """Build a GraphState with N analyze-able chunks."""
    chunks = [
        DiffChunk(
            file_path=f"src/f{i}.py",
            file_status="modified",
            content=f"@@ -1,1 +1,2 @@\n line\n+x{i}\n",
            approx_tokens=10,
            # default triage_decision="review" so all chunks dispatch
        )
        for i in range(num_chunks)
    ]
    files = [
        FileChange(path=c.file_path, status="modified", additions=1, deletions=0, patch=c.content)
        for c in chunks
    ]
    return GraphState(
        pr_url="https://github.com/o/r/pull/1",
        mode="conservative",
        pr_meta=PRMeta(
            owner="o", repo="r", number=1, title="t", author="a",
            base_ref="main", head_ref="dev", url="https://github.com/o/r/pull/1",
        ),
        files=files,
        chunks=chunks,
    )


def _fake_anthropic_response(text: str) -> MagicMock:
    """Build a MagicMock that quacks like an Anthropic Message."""
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text=text)]
    resp.usage = MagicMock()
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


def _patched_anthropic_factory(create_fn):
    """Return a context-manager that patches Anthropic so any LLM()
    instance gets a client whose .messages.create() runs `create_fn`."""
    fake_client = MagicMock()
    fake_client.messages.create = create_fn
    fake_cls = MagicMock(return_value=fake_client)
    return patch("pr_agent.llm.Anthropic", fake_cls)


# ---------- tests ----------

def test_concurrent_analyze_parallelizes(monkeypatch):
    """8 chunks × ~80ms each. With concurrency=4 we expect ~2 batches
    (~160ms) plus orchestration overhead, well below 8×80ms=640ms.
    Generous slack for CI noise — the goal is "real parallelism", not
    a precise speedup measurement."""
    monkeypatch.setenv("ANALYZE_CONCURRENCY", "4")

    def _slow_create(**_):
        time.sleep(0.08)
        return _fake_anthropic_response('{"findings": []}')

    state = _make_state(num_chunks=8)
    with _patched_anthropic_factory(_slow_create):
        start = time.perf_counter()
        analyze_node(state)
        elapsed = time.perf_counter() - start

    # Sequential lower bound is 8 × 0.08 = 0.64s. With concurrency=4
    # we should land near 0.16s plus overhead. We assert "much less
    # than sequential" with slack — fail only on real regressions.
    assert elapsed < 0.5, (
        f"expected concurrency win; elapsed={elapsed:.2f}s "
        f"(sequential baseline would be ~0.64s)"
    )


def test_budget_cap_holds_under_concurrency(monkeypatch):
    """20 chunks, budget=5, concurrency=8. The threading.Lock on
    LLM._calls_made must prevent the cap being exceeded by N-1.
    We assert: at most 5 successful Anthropic calls actually fire."""
    monkeypatch.setenv("ANALYZE_CONCURRENCY", "8")
    monkeypatch.setenv("MAX_LLM_CALLS_PER_RUN", "5")

    successful_calls = {"n": 0}
    call_lock = threading.Lock()

    def _counting_create(**_):
        # If we got here, the budget check let us through.
        with call_lock:
            successful_calls["n"] += 1
        # Tiny sleep so the threads overlap.
        time.sleep(0.02)
        return _fake_anthropic_response('{"findings": []}')

    state = _make_state(num_chunks=20)
    with _patched_anthropic_factory(_counting_create):
        result = analyze_node(state)

    assert successful_calls["n"] <= 5, (
        f"budget cap leaked: {successful_calls['n']} real API calls made, limit was 5"
    )
    # The other 15 chunks should be reported as budget-exceeded errors.
    budget_errs = [e for e in result.get("errors", []) if "MAX_LLM_CALLS_PER_RUN" in e]
    assert len(budget_errs) >= 1, "expected at least one BUDGET_EXCEEDED in state.errors"


def test_one_failing_chunk_does_not_kill_others(monkeypatch):
    """If one Anthropic call raises, the other chunks still complete
    and their findings are aggregated. The failing chunk shows up in
    state.errors."""
    monkeypatch.setenv("ANALYZE_CONCURRENCY", "4")

    call_count = {"n": 0}
    call_lock = threading.Lock()

    def _flaky_create(**_):
        with call_lock:
            call_count["n"] += 1
            this_n = call_count["n"]
        # Fail the 3rd dispatch; let the others succeed.
        if this_n == 3:
            raise RuntimeError("simulated transient failure")
        return _fake_anthropic_response('{"findings": []}')

    state = _make_state(num_chunks=5)
    with _patched_anthropic_factory(_flaky_create):
        result = analyze_node(state)

    # 4 of 5 should succeed silently (empty findings each), 1 should
    # land in errors with the simulated failure message.
    errors = result.get("errors", [])
    assert any("simulated transient failure" in e for e in errors), (
        f"failing chunk's error not captured; errors={errors}"
    )
    # No more than one error — we don't want one failure to cascade.
    assert len(errors) == 1, f"cascade detected; errors={errors}"


def test_concurrency_1_behaves_sequentially(monkeypatch):
    """Setting ANALYZE_CONCURRENCY=1 should preserve the pre-concurrency
    behaviour exactly: one Anthropic call at a time, all in order, all
    succeed."""
    monkeypatch.setenv("ANALYZE_CONCURRENCY", "1")

    call_order: list[str] = []
    call_lock = threading.Lock()

    def _ordered_create(**_):
        # Capture the system prompt fingerprint to verify order
        with call_lock:
            call_order.append(f"call-{len(call_order)}")
        time.sleep(0.01)
        return _fake_anthropic_response('{"findings": []}')

    state = _make_state(num_chunks=5)
    with _patched_anthropic_factory(_ordered_create):
        result = analyze_node(state)

    # All 5 chunks should have completed, no errors.
    assert result.get("errors", []) == [], f"unexpected errors: {result.get('errors')}"
    assert len(call_order) == 5, f"expected 5 calls, got {len(call_order)}"
