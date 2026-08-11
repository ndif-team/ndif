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

from ..deployments.modeling.nns import execute_traced_block, prepare_traced_block
from ..deployments.modeling.util import resolve_dtype, set_process_limits
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
            args.model_key, dtype, tp_size=args.tp_size, **load_kwargs
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

        if message[0] != "PREPARE":
            continue

        _, payload, compress, env, seed = message

        # Phase 1 — apply the request's environment and build the block. No
        # collective runs here, so a failure is still safe to report: rank 0 has
        # not started its forward, and the same payload is about to fail there
        # for the same reason.
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
            continue

        # Phase 2 — run it, in lockstep with every other rank. Whatever the block
        # saved is dropped: rank 0 computed the same values from the same gathered
        # tensors, and it is the one that answers the client.
        abort.arm()
        try:
            execute_traced_block(tracer, dtype)
            connection.send(("DONE",))
        except AbortedError:
            # Every rank raised this on the same iteration; the group is intact.
            connection.send(("DONE",))
        except Exception as exception:
            connection.send(("ERROR", _format(exception)))
        finally:
            abort.disarm()
            torch.cuda.synchronize()


def _await_go(connection: Channel) -> bool:
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
