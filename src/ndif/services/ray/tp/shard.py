"""A non-zero rank: hold a shard of the weights and run what rank 0 runs.

Launched by [`ShardGroup`][ndif.services.ray.tp.host.ShardGroup] as
``python -m ndif.services.ray.tp.shard``, one per GPU beyond the first. It loads
its shard, then sits on its socket running whatever rank 0 sends.

It runs the user's block in full — that is the point. Interventions are gathered
across ranks, so every rank has to reach every collective, which means every rank
executes the same Python. What a shard does *not* do is talk to anyone: no
responses, no uploads, no metrics, no logs to the user. Rank 0 owns all of that,
and the values a shard computes are identical to rank 0's anyway (they come from
gathered tensors), so they are dropped rather than shipped.

Errors go home as text over the socket. A shard has no way to reach the client
and no business classifying failures — rank 0 turns what it reports into the
user's error.
"""

from __future__ import annotations

import argparse
import ast
import socket
import sys
import traceback

from ..deployments.modeling.nns import (
    block_scope,
    execute_traced_block,
    prepare_traced_block,
)
from ..deployments.modeling.util import resolve_dtype, set_process_limits
from ..sandbox.driver import SandboxDriver
from ..sandbox.host import FollowerConnection as RunnerConnection
from .common import (
    AbortController,
    AbortedError,
    Channel,
    load_sharded_model,
    seed_ranks,
)

# How long to wait for the next command before deciding rank 0 is gone. Long,
# because a shard sits idle between requests; it exists so an orphaned child
# exits instead of living forever.
IDLE_TIMEOUT_SECONDS = 3600.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--tp-size", type=int, required=True)
    parser.add_argument("--load-kwargs", default="{}")
    parser.add_argument("--mem-bytes", type=int, default=0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(args.socket)
    connection = Channel(sock)

    # Same split as the actor's: the name loads (it may be a quantization, which
    # is not a torch.dtype), the resolved one autocasts. Every rank has to make
    # this split identically or they hold their weights differently.
    dtype = resolve_dtype(args.dtype)
    load_kwargs = ast.literal_eval(args.load_kwargs)

    # This rank's device is visible index LOCAL_RANK — which, because the group
    # shares a CUDA_VISIBLE_DEVICES listing only its assigned cards, is this
    # rank's own GPU. It caps only that one; the others belong to other ranks.
    import torch

    torch.cuda.set_device(args.rank)
    if args.mem_bytes:
        set_process_limits({args.rank: args.mem_bytes})

    # Say who we are before doing anything slow. Loading is a rendezvous every
    # rank has to reach, rank 0 included, so it cannot be waiting on us to finish
    # loading before it starts — it waits for this, then loads, which is what
    # releases us. READY comes afterwards.
    connection.send(("HELLO", args.rank))

    try:
        model = load_sharded_model(
            args.model_key, args.dtype, tp_size=args.tp_size, **load_kwargs
        )
        abort = AbortController(model._module, source=False)
    except Exception as exception:
        # Rank 0 is waiting on READY; tell it why it will never come, rather
        # than dying and leaving it to time out.
        connection.send(("ERROR", _format(exception)))
        return 1

    connection.send(("READY", args.rank))

    from nnsight.schema.request import RequestModel

    while True:
        try:
            message = connection.recv(timeout=IDLE_TIMEOUT_SECONDS)
        except Exception:
            # Rank 0 is gone (socket closed) or has said nothing for an hour.
            # Nothing here is worth keeping without it.
            return 0

        if not isinstance(message, tuple) or message[0] == "SHUTDOWN":
            return 0

        if message[0] == "SKIP":
            # A stand-down for a request this shard never got as far as running:
            # it rejected the payload in phase 1 and looped back here, while
            # another rank's failure was what actually stopped the group. Answer
            # anyway — rank 0 waits for every shard, and cannot tell "already
            # idle" from "wedged" except by being told.
            connection.send(("IDLE", args.rank))
            continue

        if message[0] == "SANDBOX":
            # An untrusted request: the block is not ours to build or run. One
            # runner process holds it for the whole group, and this rank is a
            # *host* to it -- it drives the forward and answers reads, exactly as
            # the single-GPU sandbox actor does, with the difference that the
            # runner is answering several of us and serves each worker once for
            # all of us (see sandbox.protocol.Fanout).
            _, path, env, seed = message
            if _sandboxed_request(
                connection, args.rank, model, dtype, abort, path, env, seed
            ):
                continue
            return 0

        if message[0] != "PREPARE":
            continue

        _, payload, compress, env, seed = message

        # Spans both phases, because the block built in phase 1 is the one run in
        # phase 2. Rank 0 wraps its own request the same way, and it has to: a
        # shard that kept a previous request's registered source would fail to
        # find the *next* block in that file and silently leave the forward,
        # stranding every other rank in a collective until the execution timeout.
        with block_scope():
            # Phase 1 — apply the request's environment and build the block. No
            # collective runs here, so a failure is still safe to report: rank 0
            # has not started its forward, and the same payload is about to fail
            # there for the same reason.
            try:
                model._remoteable_set_env(env)
                seed_ranks(seed)
                tracer, _ = prepare_traced_block(
                    model, payload, compress, RequestModel.deserialize
                )
                connection.send(("READY", args.rank))
            except Exception as exception:
                connection.send(("ERROR", _format(exception)))
                continue

            if not _await_go(connection):
                # Stood down (or rank 0 vanished). Say so: `release` waits for
                # this, because "the group is back at idle" has to be something
                # rank 0 observes rather than assumes. Sending nothing here is
                # indistinguishable from a wedged shard, and cost a replica
                # restart on every request that failed to build.
                try:
                    connection.send(("IDLE", args.rank))
                except OSError:
                    return 0
                continue

            # Phase 2 — run it, in lockstep with every other rank. Whatever the
            # block saved is dropped: rank 0 computed the same values from the
            # same gathered tensors, and it is the one that answers the client.
            abort.arm()
            try:
                execute_traced_block(tracer, dtype)
                connection.send(("DONE",))
            except AbortedError:
                # Every rank raised this on the same iteration; the group is
                # intact.
                connection.send(("DONE",))
            except Exception as exception:
                connection.send(("ERROR", _format(exception)))
            finally:
                abort.disarm()
                torch.cuda.synchronize()


def _sandboxed_request(connection, rank, model, dtype, abort, path, env, seed) -> bool:
    """Serve one untrusted request as a host to the group's runner.

    Returns True to keep serving, False if rank 0 has gone and this process
    should exit.

    The two phases are the same as a trusted request's and exist for the same
    reason, only what happens in each differs: there is no block to build here, so
    "ready" means the environment is applied and the runner is reachable, and that
    is still the last point before any collective.
    """
    runner = None
    with block_scope():
        try:
            model._remoteable_set_env(env)
            # The *model's* own sampling still happens in this process even though
            # the block does not, so the ranks still have to agree here. The
            # block's own RNG is seeded separately, in the runner, from the same
            # number.
            seed_ranks(seed)
            # A *follower* connection: this rank has to reach the barrier for
            # every event, but the runner only ever reads rank 0's payload, so
            # sending the activation as well would be N-1 copies serialized and
            # discarded. See FollowerConnection.
            runner = RunnerConnection.open(path)
            connection.send(("READY", rank))
        except Exception as exception:
            if runner is not None:
                runner.close()
            connection.send(("ERROR", _format(exception)))
            return True

        if not _await_go(connection):
            runner.close()
            try:
                connection.send(("IDLE", rank))
            except OSError:
                return False
            return True

        abort.arm()
        try:
            # No `on_log`: a shard has no client to tell. The runner fans its
            # PRINTs to every host and only rank 0 forwards them.
            SandboxDriver(model, dtype).pump(runner)
            # Whatever the block saved came back to every host; rank 0 is the one
            # that uploads it, so this copy is dropped.
            connection.send(("DONE",))
        except AbortedError:
            # Every rank raised this on the same iteration; the group is intact.
            connection.send(("DONE",))
        except Exception as exception:
            connection.send(("ERROR", _format(exception)))
        finally:
            import torch

            abort.disarm()
            runner.close()
            torch.cuda.synchronize()
    return True


def _await_go(connection: Channel) -> bool:
    """Whether rank 0 committed to the forward.

    ``False`` for a ``SKIP`` (rank 0 could not go ahead), and for rank 0 going
    away entirely — the caller answers ``IDLE`` either way, and a dead socket
    makes that a no-op.
    """
    try:
        message = connection.recv(timeout=IDLE_TIMEOUT_SECONDS)
    except Exception:
        return False
    return isinstance(message, tuple) and message[0] == "GO"


def _format(exception: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    )


if __name__ == "__main__":
    sys.exit(main())
