from contextlib import contextmanager
from typing import Any, Dict, Optional

from opentelemetry import context, trace

from .setup import get_tracer


def set_request_attributes(span: trace.Span, request) -> None:
    """Set standard NDIF request attributes on a span."""
    if request is None:
        return
    span.set_attribute("ndif.request.id", str(request.id))
    if request.model_key:
        span.set_attribute("ndif.model.key", str(request.model_key))
    if request.api_key:
        span.set_attribute("ndif.api.key", str(request.api_key))
    if request.session_id:
        span.set_attribute("ndif.session.id", str(request.session_id))


@contextmanager
def trace_span(
    name: str,
    parent_context: Optional[context.Context] = None,
    attributes: Optional[Dict[str, Any]] = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
):
    """Context manager that creates a span, optionally under a restored parent context.

    Automatically sets ERROR status and records exceptions on unhandled errors.
    """
    tracer = get_tracer()
    ctx = parent_context or context.get_current()
    with tracer.start_as_current_span(
        name,
        context=ctx,
        kind=kind,
        attributes=attributes or {},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span
