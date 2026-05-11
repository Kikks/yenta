"""Anthropic client wrapped with Langfuse observability.

The spec is explicit: "For every LLM call, we must be able to see: the
prompt sent, the model used, the output received, token usage, latency."

`@observe(as_type="generation")` (via pr_agent.obs) creates a Langfuse
generation span; inside it we call `update_generation(...)` to attach
prompt, model, output, usage and latency. The wrapper degrades to a
no-op when Langfuse env isn't configured — the agent still runs.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from anthropic import Anthropic

from .config import RuntimeConfig
from .obs import observe, update_generation

log = logging.getLogger(__name__)


class LLMBudgetExceeded(RuntimeError):
    """Raised when we'd exceed MAX_LLM_CALLS_PER_RUN — protects against
    runaway fan-out on monorepo PRs."""


class LLM:
    """Thin Anthropic wrapper.

    One responsibility: take a system prompt + user prompt, return text,
    and emit a Langfuse generation with prompt/model/output/tokens/latency.
    """

    def __init__(self, cfg: RuntimeConfig) -> None:
        self._client = Anthropic(api_key=cfg.anthropic_api_key)
        self._model = cfg.anthropic_model
        self._max_calls = cfg.max_llm_calls_per_run
        self._calls_made = 0

    @property
    def calls_made(self) -> int:
        return self._calls_made

    @observe(name="anthropic.messages.create", as_type="generation")
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if self._calls_made >= self._max_calls:
            raise LLMBudgetExceeded(
                f"hit MAX_LLM_CALLS_PER_RUN={self._max_calls}; aborting to prevent runaway cost"
            )
        self._calls_made += 1

        start = time.perf_counter()
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Anthropic returns a list of content blocks; we only ask for text.
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")

        usage = {
            "input": resp.usage.input_tokens,
            "output": resp.usage.output_tokens,
            "total": resp.usage.input_tokens + resp.usage.output_tokens,
        }

        # Push everything the spec asks for into Langfuse so a reviewer can
        # see *exactly* what the model saw and what it returned.
        update_generation(
            input={"system": system, "user": user},
            output=text,
            model=self._model,
            usage=usage,
            model_parameters={"temperature": temperature, "max_tokens": max_tokens},
            metadata={**(metadata or {}), "latency_ms": latency_ms},
        )

        log.info(
            "llm call #%d model=%s in=%d out=%d latency_ms=%d",
            self._calls_made,
            self._model,
            usage["input"],
            usage["output"],
            latency_ms,
        )
        return text


def parse_json_block(text: str) -> Any:
    """Tolerant JSON extractor for LLM output.

    Models sometimes wrap JSON in ```json fences or add a preamble. We
    take the first {...} or [...] balanced block. If parsing fails we
    raise — callers decide whether to retry or skip.
    """
    text = text.strip()
    # strip code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        # also strip leading "json"
        if text.startswith("json"):
            text = text[4:].lstrip()

    # find first { or [
    start_obj = text.find("{")
    start_arr = text.find("[")
    candidates = [c for c in (start_obj, start_arr) if c >= 0]
    if not candidates:
        raise ValueError("no JSON object/array found in LLM output")
    start = min(candidates)
    return json.loads(text[start:])


def approx_token_count(s: str) -> int:
    """Cheap token estimate — ~4 chars per token. Good enough for chunking
    decisions; we don't ship a tokenizer dependency for this."""
    return max(1, len(s) // 4)
