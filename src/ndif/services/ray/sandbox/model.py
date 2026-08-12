"""Model deployment that runs user code in a separate process (no VM).

Subclasses :class:`BaseModelDeployment`: the model still loads on the host (so
the base's caching/lifecycle are unchanged), but the request's traced block is
deserialized and executed in a pooled runner process. The intervention code runs
there while the forward pass runs here, interleaved over the socket.

What this module owns is the *actor* half: the runner pool, the request
lifecycle, and turning a runner's failure into a client-facing error. The half
that actually drives the model for a runner — proxies, interleaving, control
events — is [`driver.py`](driver.py), which knows nothing about Ray or requests
so that a tensor-parallel shard (a host with a model and no actor around it) can
use it too.
"""

import contextlib
import os
import threading
from typing import TYPE_CHECKING, Any, Optional

import ray

from nnsight.schema.response import Status

from ..deployments.modeling.base import BaseModelDeployment
from .driver import RunnerError, SandboxDriver
from .host import Pool

if TYPE_CHECKING:
    from ....common.schema.request import BackendRequestModel


# How many runners to keep pre-warmed per model actor.
#
# Sized from the two costs it trades off, both measured on a g4dn.xlarge:
# a cold spawn (python + torch + nnsight import) takes ~4s, while executing an
# already-warm request takes ~0.7s. A pool only keeps up if refills — which run
# concurrently, one thread each — cover the drain rate, so it needs to be at
# least spawn/execute ~= 6. At the old default of 2 a saturated queue drained
# the pool immediately and every other request paid the full ~4s spawn inline,
# which cost roughly 5x throughput on the untrusted path.
#
# The costs of raising it: each warm runner holds ~420 MB (PSS) whether or not
# it is used, so 7 is ~2.9 GB per model actor, and concurrent refills contend
# for CPU on the node hosting the actor. On a memory- or core-tight node, or
# with many models resident at once, turn this down.
DEFAULT_POOL_SIZE = int(os.environ.get("NDIF_SANDBOX_POOL_SIZE", "7"))


class SandboxModelDeployment(BaseModelDeployment):
    """Loads the model on the host; runs user code in a process pool."""

    def __init__(
        self, *args: Any, pool_size: Optional[int] = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.pool = Pool(size=DEFAULT_POOL_SIZE if pool_size is None else pool_size)
        # The runner currently executing (fresh per request), tracked so run() can
        # stop it to interrupt a timed-out or cancelled request.
        self.execution_sandbox = None

    def execution_scope(self, request: "BackendRequestModel"):
        # A trusted request runs in-process, so use the base's stdout capture. A
        # sandboxed one forwards stdout as PRINT events instead (see next_event),
        # so there's nothing to wrap here.
        if request.trusted:
            return super().execution_scope(request)
        return contextlib.nullcontext()

    def format_error(self, exception):
        # A user-block failure arrives as RunnerError with its traceback already
        # formatted in the runner (tracebacks don't survive cloudpickle); it's user
        # code, so never fatal. Host-side failures fall back to the base.
        if isinstance(exception, RunnerError):
            return str(exception), False
        return super().format_error(exception)

    def cleanup(self) -> None:
        # Fresh process per request: discard the runner, then do the base reset.
        self.discard_sandbox()
        super().cleanup()

    def interrupt(self) -> None:
        # Stopping the runner unblocks the host thread if it's parked on the socket;
        # the base's kill_thread covers it running the forward pass in Python.
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
        super().interrupt()

    def discard_sandbox(self) -> None:
        """Stop the request's runner (idempotent — ``stop`` checks the process)."""
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
            self.execution_sandbox = None

    def block_seed(self) -> "int | None":
        """The seed the runner should draw from, or None to leave its RNG alone.

        None here: one process runs the block, and two identical requests are
        meant to draw differently. A deployment whose block runs in *several*
        processes at once overrides this — see the tensor-parallel actor, where
        ranks drawing different numbers is a correctness failure, not a
        cosmetic one.
        """
        return None

    def execute(self, request: "BackendRequestModel") -> "tuple[bytes, float | None]":
        """Worker-thread body: run the request's block in a fresh runner.

        A trusted request skips the sandbox and runs in-process via the base.
        Otherwise acquire a runner (tracked on the actor so run() can stop it to
        interrupt a timed-out or cancelled request), hand it the payload, and let
        a :class:`~ndif.services.ray.sandbox.driver.SandboxDriver` drive the model
        until the block finishes. run() discards the runner afterward — one fresh
        process per request.
        """
        if request.trusted:
            return super().execute(request)
        self.execution_ident = threading.current_thread().ident
        sandbox = self.pool.acquire()
        self.execution_sandbox = sandbox
        connection = sandbox.connection()
        try:
            # The dtype rides along because the runner has no model to ask, and
            # runs the block under the same autocast bracket this actor would.
            connection.send(
                (request.payload, request.compress, str(self.dtype), self.block_seed())
            )
            driver = SandboxDriver(
                self.model,
                self.dtype,
                # The one thing a driver can't know: where a runner's stdout
                # goes. Here it is this request's client.
                on_log=lambda text: request.respond(Status.LOG, text),
            )
            return driver.pump(connection)
        finally:
            connection.close()

    def close(self) -> None:
        self.pool.close()


@ray.remote(num_cpus=1, max_restarts=-1)
class SandboxModelActor(SandboxModelDeployment):
    """Ray actor that runs user code in a separate process."""

    pass
