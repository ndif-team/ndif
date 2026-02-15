from typing import Dict, Optional

from opentelemetry import context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


class TracingContext:
    """Serialize/deserialize OTel context for cross-process propagation."""

    @staticmethod
    def inject() -> Dict[str, str]:
        """Extract current trace context into a dict suitable for pickling."""
        carrier: Dict[str, str] = {}
        _propagator.inject(carrier)
        return carrier

    @staticmethod
    def extract(carrier: Optional[Dict[str, str]] = None) -> context.Context:
        """Restore trace context from a carrier dict."""
        if not carrier:
            return context.get_current()
        return _propagator.extract(carrier=carrier)
