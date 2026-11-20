"""Optional OpenTelemetry spans, one per tool call.

If ``opentelemetry-api`` is not installed this module does nothing. Spans carry the tool
name, outcome and result shape only: never the SQL text or any result data.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover - exercised only without the optional extra
    trace = None

F = TypeVar("F", bound=Callable[..., Any])

_RESULT_ATTRS = ("row_count", "truncated")


def traced(tool: str) -> Callable[[F], F]:
    """Wrap a tool function in a ``warehouse_mcp.<tool>`` span."""

    def decorator(fn: F) -> F:
        if trace is None:
            return fn

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer("warehouse_mcp")
            with tracer.start_as_current_span(
                f"warehouse_mcp.{tool}", record_exception=False, set_status_on_exception=False
            ) as span:
                span.set_attribute("mcp.tool", tool)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    span.set_attribute("warehouse.status", "error")
                    # Tool errors can quote the user's SQL, so record only the class.
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
                span.set_attribute("warehouse.status", "ok")
                if isinstance(result, dict):
                    for key in _RESULT_ATTRS:
                        if key in result:
                            span.set_attribute(f"warehouse.{key}", result[key])
                return result

        return wrapper  # type: ignore[return-value]

    return decorator
