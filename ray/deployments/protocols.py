from typing import Callable

from nnsight.schema.format.functions import update_function
from nnsight.tracing.protocols import Protocol


class LogProtocol(Protocol):

    @classmethod
    def put(cls, fn: Callable):

        update_function(print, fn)
