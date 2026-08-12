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


class SandboxHost:
    """The actor-side half of running a block in a runner process.

    Mixed into a deployment that owns a model and wants some request's Python to
    run somewhere else. Two do: the single-GPU actor below, and the
    tensor-parallel one, which is rank 0 of a group that *shares* a runner.

    Split out because it was duplicated. The two copies drifted exactly once and
    it cost a correctness bug -- the tensor-parallel copy forgot to seed rank 0's
    own RNG, so the model sampled a different token there than on every shard.
    Everything here is what both copies agreed on; what they genuinely disagree
    about is left to them:

    * **`interrupt`.** The single-GPU actor stops the runner first, then lets the
      base kill the thread. The group cannot: killing rank 0 mid-collective
      strands the others, so it asks every rank to stop at a shared checkpoint
      first. Opposite orders, both deliberate.
    * **What happens around the payload.** A group has to tell its shards where
      the runner is before the block is sent, and release them into the forward
      after -- see the hooks on :meth:`run_in_runner`.

    A host must set ``self.pool`` and ``self.execution_sandbox`` in its
    ``__init__``; :meth:`open_pool` is the one line that does both.
    """

    def open_pool(self, pool_size: Optional[int] = None, peers: int = 1) -> None:
        """Warm a pool of runners for this actor.

        ``peers`` is how many hosts drive one runner: one ordinarily, and
        ``tp_size`` for a group that shares one, where a pool entry is a *group's*
        runner rather than a process per rank.
        """
        self.pool = Pool(
            size=DEFAULT_POOL_SIZE if pool_size is None else pool_size, peers=peers
        )
        # The runner currently executing (fresh per request), tracked so run() can
        # stop it to interrupt a timed-out or cancelled request.
        self.execution_sandbox = None

    def run_in_runner(
        self, request: "BackendRequestModel", seed: "int | None"
    ) -> "tuple[bytes, float | None]":
        """Hand a runner the block and drive the model until it finishes.

        ``seed`` is what the block's RNG is set from, or ``None`` to leave it
        alone. One process running one block wants ``None`` -- two identical
        requests are *meant* to draw differently. Several processes running the
        same block within one request want the same number, because ranks that
        sample differently go on to all-reduce activations computed from
        different tokens.
        """
        # `kill` is gated on this: it is how the actor knows a request is in
        # flight at all. Without it a cancel or a preempt is silently dropped and
        # the block runs to completion.
        self.execution_ident = threading.current_thread().ident
        sandbox = self.pool.acquire()
        self.execution_sandbox = sandbox
        connection = sandbox.connection()
        try:
            self.before_payload(request, sandbox, seed)
            # The dtype rides along because the runner has no model to ask, and
            # runs the block under the same autocast bracket this actor would.
            connection.send(
                (request.payload, request.compress, str(self.dtype), seed)
            )
            self.after_payload()
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

    def before_payload(
        self, request: "BackendRequestModel", sandbox: Any, seed: "int | None"
    ) -> None:
        """Run once the runner exists and this host is connected to it, before the
        block is sent. Nothing to do with one host; a group tells its shards where
        the runner is here, while nothing is in a collective yet and a shard that
        cannot reach it can still fail safely."""

    def after_payload(self) -> None:
        """Run once the block is on its way, before the model does anything. A
        group releases its shards into the forward here -- the last point at which
        no rank has run anything."""

    def execution_scope(self, request: "BackendRequestModel"):
        # A trusted request runs in-process, so use the base's stdout capture. A
        # sandboxed one forwards stdout as PRINT events instead (see next_event),
        # so there's nothing to wrap here.
        if request.trusted:
            return super().execution_scope(request)
        return contextlib.nullcontext()

    def format_error(self, exception):
        # A user-block failure arrives as RunnerError with its traceback already
        # formatted in the runner (tracebacks don't survive serialization); it's
        # user code, so never fatal. Host-side failures fall back to the base.
        if isinstance(exception, RunnerError):
            return str(exception), False
        return super().format_error(exception)

    def cleanup(self) -> None:
        # Fresh process per request: discard the runner, then do the base reset.
        self.discard_sandbox()
        super().cleanup()

    def restart(self) -> None:
        # The actor is about to be killed; the runner would outlive the socket it
        # is holding otherwise (die_with_parent gets it eventually, this is
        # immediate).
        self.discard_sandbox()
        super().restart()

    def discard_sandbox(self) -> None:
        """Stop the request's runner (idempotent — ``stop`` checks the process)."""
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
            self.execution_sandbox = None

    def close(self) -> None:
        self.pool.close()


class SandboxModelDeployment(SandboxHost, BaseModelDeployment):
    """Loads the model on the host; runs user code in a process pool."""

    def __init__(
        self, *args: Any, pool_size: Optional[int] = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.open_pool(pool_size)

    def interrupt(self) -> None:
        # Stopping the runner unblocks the host thread if it's parked on the socket;
        # the base's kill_thread covers it running the forward pass in Python.
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
        super().interrupt()

    def execute(self, request: "BackendRequestModel") -> "tuple[bytes, float | None]":
        """Worker-thread body: run the request's block in a fresh runner.

        A trusted request skips the sandbox and runs in-process via the base;
        anything else goes to a runner.

        ``None`` for the seed, always: one process runs the block, and two
        identical requests are *meant* to draw differently. Seeding every request
        would quietly turn repeated sampling into repeated identical samples. It
        is the group that needs a shared number, and it passes its own.
        """
        if request.trusted:
            return super().execute(request)
        return self.run_in_runner(request, None)


@ray.remote(num_cpus=1, max_restarts=-1)
class SandboxModelActor(SandboxModelDeployment):
    """Ray actor that runs user code in a separate process."""

    pass
