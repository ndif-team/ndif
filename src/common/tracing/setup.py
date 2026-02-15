import atexit
import os
import socket

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_initialized = False
_provider = None


def init_tracing(service_name: str) -> None:
    global _initialized, _provider
    if _initialized:
        return
    _initialized = True

    resource = Resource.create({
        SERVICE_NAME: service_name,
        "service.namespace": "ndif",
        "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
    })
    _provider = TracerProvider(resource=resource)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    else:
        exporter = ConsoleSpanExporter()

    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)

    atexit.register(_shutdown)


def _shutdown() -> None:
    if _provider is not None:
        _provider.shutdown()



def get_tracer(name: str = "ndif") -> trace.Tracer:
    return trace.get_tracer(name)
