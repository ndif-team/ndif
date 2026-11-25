"""Running one request in the runner.

`run` deserializes the request against this runner's meta model, executes the
block with the executor the trusted path uses, and sends the host an ``End``
holding what the block saved, or a ``Failed`` holding its traceback.
"""

import functools
import io
import sys
import time

import torch

from nnsight.tracing.globals import block_scope
from nnsight.util import apply

from ....common.errors import RequestError
from ....common.schema.request import BackendRequestModel
from ..deployments.modeling.nns import execute_traced_block
from ..deployments.modeling.util import cpu_pickle_module, resolve_dtype
from .link import Link
from .messages import End, Failed, OnHost
from .meta import PERSISTENT_OBJECTS, IPCCloudUnpickler, set_env


def run(conn, blob: bytes, compress: bool = False, dtype: str = "float32",
        seed: "int | None" = None, env: "dict | None" = None) -> None:
    """Deserialize the request in this process and execute it; interleave over IPC.

    Runs as the runner's user code: ``conn`` is the socket to the host. Model calls
    hit ``IPCEnvoy.interleave`` and drive the host's forward pass over the socket.
    On success ``End`` carries the ``torch.save`` blob of the block's
    ``nnsight.save()``-marked values (ready for the host to upload as-is, no pickle
    round-trip) plus ``deserialize_ms`` — deserialize happens here, so the host
    can't time that phase itself.

    ``dtype`` is the model's, shipped from the host because this process has no
    model to ask. It matters: the block runs under the same autocast bracket the
    in-process path uses, and without it an untrusted submission computed at
    different numerics from the identical trusted one.

    ``seed`` rides along for the same reason and is usually ``None``. It is set
    when several processes must draw the *same* numbers within one request — a
    tensor-parallel group, where the block's RNG now lives out here rather than
    in the rank. Seeding the rank would seed the wrong process.

    ``env`` is the request's per-request environment, the same dict the host
    applied to the loaded model just before this — see :func:`set_env` for why
    the runner needs it too.
    """
    # Deserialization is separated from execution because the two fail for
    # entirely different reasons and only the second has a user traceback worth
    # sending. Sharing one `except` meant a corrupt payload came back dressed as
    # a user-code failure: NDIF's own frames, this file's path, and a
    # `ZstdError` the caller could do nothing with.
    # The same per-request source-cache hygiene the model actor applies. A fresh
    # runner per request makes it redundant today; it is here so that stops being
    # load-bearing, because the failure it prevents is a block silently running
    # the *previous* request's code (see nnsight's `block_scope`).
    with block_scope():
        _run(Link(conn), blob, compress, dtype, seed, env)


def _run(
    link,
    blob: bytes,
    compress: bool,
    dtype: str,
    seed: "int | None",
    env: "dict | None" = None,
) -> None:
    try:
        # Before deserializing, not after: the map the unpickler resolves against
        # has to describe the tree this request asked for.
        set_env(env)
        # Same rebuild as a normal server (code recompiled, scope restored), but
        # with our unpickler so persistent ids resolve to the IPC interleaver /
        # this runner's meta model.
        deserialize_started = time.time()
        tracer = BackendRequestModel.deserialize(
            blob,
            persistent_objects=PERSISTENT_OBJECTS,
            compress=compress,
            unpickler=functools.partial(IPCCloudUnpickler, link=link),
        )
        deserialize_ms = round((time.time() - deserialize_started) * 1000, 2)
    except RequestError as error:
        # No user frames exist yet, so there is no traceback worth sending —
        # just the sentence BackendRequestModel.deserialize already classified
        # this into. The in-process path raises the very same object; only text
        # crosses this socket, so the runner ships str() of it, plus the type's
        # name so telemetry records what failed rather than the wrapper (#280).
        terminal = Failed(str(error), type(error).__name__)
    else:
        try:
            # The shared executor, not a copy of it: the autocast bracket and the
            # save collection have to be identical to the in-process path, or the
            # same block returns different numbers depending on whether the
            # request was trusted.
            saved = execute_traced_block(tracer, resolve_dtype(dtype), seed)
            # A saved value can hold the stand-in for a KV cache (a layer's
            # `.inputs`). The cache never came here, and the client could not
            # load the stand-in's class, so the result holds None there.
            saved = apply(saved, lambda stand_in: None, OnHost)
            buffer = io.BytesIO()
            torch.save(saved, buffer, pickle_module=cpu_pickle_module())
        except Exception as error:
            # Format the traceback here, where it's live: an exception loses its
            # __traceback__ when cloudpickled across the socket. A worker error already
            # carries a clean, intervention-only traceback (stashed by Mediator.switch);
            # otherwise drop nnsight's plumbing. Send the formatted text, not the object.
            import traceback

            from nnsight.tracing.util import clean_traceback

            tb = getattr(error, "__intervention_tb__", None) or clean_traceback(
                error.__traceback__
            )
            message = "".join(traceback.format_exception(type(error), error, tb))
            # The type's *name* rides along: the host wraps the text in a
            # RunnerError, and without the name a user mistake is recorded in
            # telemetry as the wrapper — OutOfOrderError trusted, RunnerError
            # untrusted, for the identical block (#280).
            terminal = Failed(message, type(error).__name__)
        else:
            terminal = End(buffer.getvalue(), deserialize_ms)
    # Drain a trailing partial print line (stdout is the runner's line-buffering
    # Writer here) so it lands before the terminal event on the socket.
    sys.stdout.flush()
    link.send(terminal)
