from typing import Any, Callable

from nnsight.intervention.backends import Backend
from .security.protected_environment import (
    Protector,
)
from nnsight.intervention.tracing.globals import Globals
from nnsight.intervention.tracing.tracer import Tracer
from nnsight.intervention.tracing.util import wrap_exception

from ...tracing import trace_span


class RemoteExecutionBackend(Backend):
    def __init__(self, fn: Callable, protector: Protector):
        self.fn = fn
        self.protector = protector

    def __call__(self, tracer: Tracer):
        Globals.stack = 0
        Globals.enter()

        try:
            with trace_span("model_actor.nnsight_execute") as span:
                num_mediators = len(tracer.mediators) if hasattr(tracer, 'mediators') else None
                if num_mediators is not None:
                    span.set_attribute("ndif.nnsight.num_mediators", num_mediators)

                with self.protector:
                    saves = tracer.execute(self.fn)

                num_saves = len(saves) if saves else 0
                span.set_attribute("ndif.nnsight.num_saves", num_saves)
        except Exception as e:
            raise wrap_exception(e, tracer.info) from None
        finally:
            Globals.cache.clear()
            Globals.saves.clear()
            Globals.exit()
            Globals.stack = 0

        return saves
