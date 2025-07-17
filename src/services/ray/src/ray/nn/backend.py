from typing import Any, Callable

from nnsight.intervention.backends import Backend
from .utils import (
    WHITELISTED_MODULES_DESERIALIZATION,
    Protector,
    WHITELISTED_MODULES,
    ProtectorEscape,
)
from .sandbox import run
from nnsight.intervention.tracing.globals import Globals
from nnsight.intervention.tracing.tracer import Tracer


class RemoteExecutionBackend(Backend):

    def __init__(self, fn: Callable):
        self.fn = fn

    def __call__(self, tracer: Tracer):

        protector = Protector(WHITELISTED_MODULES)
        #escape = ProtectorEscape(protector)

        Globals.enter()

        with protector:
            #with escape:
            run(tracer, self.fn)

        Globals.exit()

        return {
            key: value
            for key, value in tracer.info.frame.f_locals.items()
            if not key
            in {
                "__nnsight_tracer__",
                "__nnsight_model__",
                "tracer",
                "fn",
                "__nnsight_tracing_info__",
                "_frame",
                "mediator"
            }
        }
