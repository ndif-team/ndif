from typing import Any, Callable

from nnsight.intervention.backends import Backend
from .security.protected_environment import (
    Protector,
)
from nnsight.intervention.tracing.globals import Globals
from nnsight.intervention.tracing.tracer import Tracer
from nnsight.intervention.tracing.util import wrap_exception


class RemoteExecutionBackend(Backend):
    def __init__(self, fn: Callable, protector: Protector):
        self.fn = fn
        self.protector = protector

    def __call__(self, tracer: Tracer):
        Globals.stack = 0
        Globals.enter()

        try:
            with self.protector:
                saves = tracer.execute(self.fn)
        except Exception as e:
            raise wrap_exception(e, tracer.info) from None
        finally:
            Globals.exit()

        Globals.saves.clear()
        Globals.stack = 0

        return saves
