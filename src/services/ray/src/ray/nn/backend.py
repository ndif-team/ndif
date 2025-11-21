from typing import Any, Callable

from nnsight.intervention.backends import Backend
from .security.protected_environment import (
    Protector,
    WHITELISTED_MODULES,
)
from .sandbox import run
from nnsight.intervention.tracing.globals import Globals
from nnsight.intervention.tracing.tracer import Tracer


class RemoteExecutionBackend(Backend):
    def __init__(self, fn: Callable, protector: Protector):
        self.fn = fn
        self.protector = protector

    def __call__(self, tracer: Tracer):
        Globals.stack = 0
        Globals.enter()

        try:
            with self.protector:
                run(tracer, self.fn)

        finally:
            Globals.exit()

        saves = {
            key: value
            for key, value in tracer.info.frame.f_locals.items()
            if id(value) in Globals.saves
        }

        Globals.saves.clear()
        Globals.stack = 0

        return saves
