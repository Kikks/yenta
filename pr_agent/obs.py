"""Langfuse observability helpers — thin wrapper with no-op fallback.

Why this module exists:
  - Langfuse SDK churned hard between v2 and v3 (decorators moved,
    `langfuse_context` is gone, `update_current_observation` split into
    `update_current_generation` / `update_current_span`, `usage` became
    `usage_details`). Centralising the shim means a future v4 migration
    is a one-file change.
  - When LANGFUSE_* env is absent we want the agent to still run. The
    helpers here degrade to no-ops cleanly.

Target SDK: langfuse >= 3.0.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

try:
    from langfuse import get_client as _get_client
    from langfuse import observe as _observe

    _LANGFUSE_OK = True
except Exception:  # pragma: no cover
    _LANGFUSE_OK = False
    _get_client = None  # type: ignore[assignment]

    def _observe(func=None, *, name=None, as_type=None, **_kw):  # type: ignore[no-redef]
        def deco(fn: Callable) -> Callable:
            return fn

        return deco if func is None else func


def observe(func=None, *, name: Optional[str] = None, as_type: Optional[str] = None):
    """`@observe` decorator that survives without Langfuse installed.

    Usage:
        @observe(name="node.fetch")
        def fetch_node(state): ...

        @observe(name="anthropic.messages.create", as_type="generation")
        def complete(self, ...): ...
    """
    if _LANGFUSE_OK:
        if as_type is not None:
            return _observe(func, name=name, as_type=as_type)  # type: ignore[arg-type]
        return _observe(func, name=name)  # type: ignore[arg-type]
    # no-op
    if func is None:
        def deco(fn: Callable) -> Callable:
            return fn

        return deco
    return func


def _client():
    if not _LANGFUSE_OK or _get_client is None:
        return None
    try:
        return _get_client()
    except Exception:
        return None


def update_generation(
    *,
    input: Any = None,
    output: Any = None,
    model: Optional[str] = None,
    usage: Optional[dict] = None,
    metadata: Optional[dict] = None,
    model_parameters: Optional[dict] = None,
) -> None:
    """Attach generation-specific fields to the current observation.

    Use inside a function decorated with `@observe(as_type="generation")`.
    """
    c = _client()
    if c is None:
        return
    kwargs: dict[str, Any] = {}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    if model is not None:
        kwargs["model"] = model
    if usage is not None:
        # v3 renamed `usage` -> `usage_details`
        kwargs["usage_details"] = usage
    if metadata is not None:
        kwargs["metadata"] = metadata
    if model_parameters is not None:
        kwargs["model_parameters"] = model_parameters
    try:
        c.update_current_generation(**kwargs)
    except Exception:
        # Never let observability break the agent. Worst case: missing trace data.
        pass


def update_span(
    *,
    input: Any = None,
    output: Any = None,
    metadata: Optional[dict] = None,
) -> None:
    """Attach data to the current non-generation span."""
    c = _client()
    if c is None:
        return
    kwargs: dict[str, Any] = {}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    if metadata is not None:
        kwargs["metadata"] = metadata
    try:
        c.update_current_span(**kwargs)
    except Exception:
        pass


def update_trace(
    *,
    name: Optional[str] = None,
    tags: Optional[list] = None,
    metadata: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> None:
    """Attach data to the root trace of the current run."""
    c = _client()
    if c is None:
        return
    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if tags is not None:
        kwargs["tags"] = tags
    if metadata is not None:
        kwargs["metadata"] = metadata
    if user_id is not None:
        kwargs["user_id"] = user_id
    try:
        c.update_current_trace(**kwargs)
    except Exception:
        pass


def flush() -> None:
    """Block until pending traces are uploaded. Call in `finally:` of main."""
    c = _client()
    if c is None:
        return
    try:
        c.flush()
    except Exception:
        pass
