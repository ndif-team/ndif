"""Dispatcher: route queued requests to per-model Processors.

Architecture:
    Redis queue -> Dispatcher -> Processor(s) -> Replica(s) -> model actor

The Dispatcher pops requests from Redis, lazily creates a Processor per
``model_key``, and hands the request off. Processors are self-healing: they
provision and re-provision replicas on demand.

It runs inside the Ray cluster, as the detached ``Dispatcher`` actor on the head
node (:class:`DispatcherActor`), and goes down and comes back with the head.
Redis is its only link to the API, which reads the ``ray:connected`` key it
renews to know it is alive.

Launched by the ray service's ``start.sh``, after the Controller:
    python -m ndif.services.ray.queue.dispatcher
"""

import asyncio
import json
import logging
import pickle

import ray

from ....common.providers.ray import NAMESPACE, controller_handle
from ....common.providers.redis import RedisProvider
from ....common.redis import (
    ENV_KEY,
    ENV_READY_CHANNEL,
    ENV_REQUESTED_KEY,
    ENV_TIMEOUT_S,
    ENV_TRIGGER_STREAM,
    ENV_TTL_S,
    EVENT_KILL,
    EVENT_QUEUE_STATE,
    EVENT_RECONCILE,
    EVENT_RESPONSE_TTL_S,
    EVENTS_STREAM,
    STATUS_KEY,
    STATUS_READY_CHANNEL,
    STATUS_REQUESTED_KEY,
    STATUS_TIMEOUT_S,
    STATUS_TRIGGER_STREAM,
    STATUS_TTL_S,
)
from ....common.schema import Status
from ....common.schema.request import BackendRequestModel
from .config import CONFIG
from .processor import Processor

logger = logging.getLogger("ndif.queue.dispatcher")


# How long ``ray:connected`` outlives the dispatcher. Renewed three times within it.
CONNECTED_TTL_S = 30

STREAM_BLOCK_MS = CONFIG.fetch_timeout_s * 1000


async def stream_tail(client, stream: str) -> str:
    """The id a worker reads ``stream`` on from: that of its newest entry.

    Not ``$``. A blocking read returns empty when its block runs out, and one
    issued again from ``$`` misses whatever was added in between.
    """
    newest = await client.xrevrange(stream, count=1)
    return newest[0][0] if newest else "0-0"


class Dispatcher:
    """Central coordinator routing requests to per-model Processors."""

    def __init__(self) -> None:
        self.processors: dict[str, Processor] = {}

    async def startup(self) -> None:
        """Wait for Redis and the Controller before serving.

        Clears the cached status and env, and their locks: they describe
        whatever cluster ran before this one.
        """
        while True:
            try:
                await RedisProvider.async_client.delete(
                    STATUS_KEY, STATUS_REQUESTED_KEY, ENV_KEY, ENV_REQUESTED_KEY
                )
                break
            except Exception:
                logger.exception("Waiting for Redis")
                await asyncio.sleep(1)

        while True:
            try:
                controller_handle()
                break
            except Exception:
                logger.exception("Waiting for the Controller")
                await asyncio.sleep(1)

    async def get(self) -> list[BackendRequestModel]:
        """Fetch pending requests from the Redis queue.

        Blocking-pop one, for at most ``fetch_timeout_s``, then batch-pop up to
        ``fetch_batch_max`` more to amortize round-trips under load.
        """
        client = RedisProvider.async_bytes_client

        result = await client.brpop(CONFIG.queue_key, timeout=CONFIG.fetch_timeout_s)
        if result is None:
            return []

        requests = [pickle.loads(result[1])]

        while len(requests) < CONFIG.fetch_batch_max:
            item = await client.rpop(CONFIG.queue_key)
            if item is None:
                break
            requests.append(pickle.loads(item))

        return requests

    async def dispatch(self, request: BackendRequestModel) -> None:
        """Route a request to its model's Processor, creating one if needed.

        The Processor is lazy: ``enqueue`` provisions a replica on demand and
        re-provisions itself after an eviction, so there's no worker task to
        spawn or tear down here.
        """
        if request.model_key not in self.processors:
            self.processors[request.model_key] = Processor(request.model_key)

        await self.processors[request.model_key].enqueue(request)

    async def heartbeat_worker(self) -> None:
        """Renew ``ray:connected``, which tells the API the dispatcher is alive."""
        client = RedisProvider.async_client

        while True:
            try:
                await client.set("ray:connected", "1", ex=CONNECTED_TTL_S)
                await asyncio.sleep(CONNECTED_TTL_S / 3)
            except Exception:
                logger.exception("heartbeat_worker: failed to renew ray:connected")
                await asyncio.sleep(1)

    async def status_worker(self) -> None:
        """Serve coalesced cluster-status refreshes for the API's /status.

        Blocks on the status trigger stream (one refresh is triggered per
        coalescing window, so this fetches at most once per window). On each
        trigger, pulls the heavy status from the controller (time-bounded),
        caches it, and wakes waiters. On failure it clears the coalescing lock
        and wakes waiters with an error so they don't hang.
        """
        client = RedisProvider.async_client
        last_id = None

        while True:
            try:
                if last_id is None:
                    last_id = await stream_tail(client, STATUS_TRIGGER_STREAM)
                messages = await client.xread(
                    {STATUS_TRIGGER_STREAM: last_id}, block=STREAM_BLOCK_MS, count=1
                )
                if not messages:
                    continue
                _, entries = messages[0]
                last_id = entries[-1][0]

                handle = controller_handle()
                status = await asyncio.wait_for(
                    handle.status.remote(), timeout=STATUS_TIMEOUT_S
                )

                await client.set(
                    STATUS_KEY, json.dumps(status, default=str), ex=STATUS_TTL_S
                )
                await client.delete(STATUS_REQUESTED_KEY)
                await client.publish(STATUS_READY_CHANNEL, "ok")
            except Exception:
                logger.exception("status_worker: failed to refresh status")
                try:
                    await client.delete(STATUS_REQUESTED_KEY)
                    await client.publish(STATUS_READY_CHANNEL, "error")
                except Exception:
                    # Redis is what failed. Escaping here would end this task,
                    # which nothing restarts.
                    logger.exception("status_worker: failed to report the error")
                # Don't spin: while Redis is down every read fails at once.
                await asyncio.sleep(1)

    async def env_worker(self) -> None:
        """Serve coalesced cluster-env refreshes for the API's /env.

        Structurally identical to :meth:`status_worker` but for the controller's
        ``env`` (python version + installed packages). The API can't reach Ray,
        so it triggers a refresh here and waits on the ready channel.
        """
        client = RedisProvider.async_client
        last_id = None

        while True:
            try:
                if last_id is None:
                    last_id = await stream_tail(client, ENV_TRIGGER_STREAM)
                messages = await client.xread(
                    {ENV_TRIGGER_STREAM: last_id}, block=STREAM_BLOCK_MS, count=1
                )
                if not messages:
                    continue
                _, entries = messages[0]
                last_id = entries[-1][0]

                handle = controller_handle()
                env = await asyncio.wait_for(
                    handle.env.remote(), timeout=ENV_TIMEOUT_S
                )

                await client.set(ENV_KEY, json.dumps(env, default=str), ex=ENV_TTL_S)
                await client.delete(ENV_REQUESTED_KEY)
                await client.publish(ENV_READY_CHANNEL, "ok")
            except Exception:
                logger.exception("env_worker: failed to refresh env")
                try:
                    await client.delete(ENV_REQUESTED_KEY)
                    await client.publish(ENV_READY_CHANNEL, "error")
                except Exception:
                    # As in status_worker: this must not end the task.
                    logger.exception("env_worker: failed to report the error")
                await asyncio.sleep(1)

    async def events_worker(self) -> None:
        """Serve CLI operational events: queue introspection, kill, reconcile.

        Blocks on the events stream and dispatches each entry by ``event_type``
        to a handler. Handlers are individually guarded so one bad event can't
        take the worker down. See :mod:`ndif.common.redis.events`.
        """
        client = RedisProvider.async_client
        last_id = None
        handlers = {
            EVENT_QUEUE_STATE: self._handle_queue_state,
            EVENT_KILL: self._handle_kill,
            EVENT_RECONCILE: self._handle_reconcile,
        }

        while True:
            try:
                if last_id is None:
                    last_id = await stream_tail(client, EVENTS_STREAM)
                messages = await client.xread(
                    {EVENTS_STREAM: last_id}, block=STREAM_BLOCK_MS, count=1
                )
                if not messages:
                    continue
                _, entries = messages[0]
                for entry_id, fields in entries:
                    last_id = entry_id
                    handler = handlers.get(fields.get("event_type"))
                    if handler is None:
                        continue
                    try:
                        await handler(fields)
                    except Exception:
                        logger.exception(f"events_worker: handler failed for {fields}")
            except Exception:
                logger.exception("events_worker: error reading events stream")
                await asyncio.sleep(1)

    async def _respond(self, response_key: str, payload: dict) -> None:
        """Push a JSON reply to the caller's response_key (brpop'd by the CLI)."""
        client = RedisProvider.async_client
        await client.lpush(response_key, json.dumps(payload, default=str))
        await client.expire(response_key, EVENT_RESPONSE_TTL_S)

    async def _handle_queue_state(self, fields: dict) -> None:
        payload = {
            "processors": {mk: p.snapshot() for mk, p in self.processors.items()}
        }
        await self._respond(fields["response_key"], payload)

    async def _handle_kill(self, fields: dict) -> None:
        await self._respond(fields["response_key"], await self._kill(fields["request_id"]))

    async def _kill(self, request_id: str) -> dict:
        """Cancel a request: remove it if queued, else cancel it if executing."""
        for processor in self.processors.values():
            request = processor.pop_queued(request_id)
            if request is not None:
                await request.arespond(Status.ERROR, "Request cancelled by operator.")
                return {"status": "removed_from_queue",
                        "message": f"Request {request_id} removed from the queue."}

        for processor in self.processors.values():
            replica = processor.executing_replica(request_id)
            if replica is not None:
                await replica.cancel("Request cancelled by operator.", interrupt=True)
                return {"status": "cancelled_execution",
                        "message": f"Request {request_id} cancelled while executing."}

        return {"status": "not_found", "message": f"Request {request_id} not found."}

    async def _handle_reconcile(self, fields: dict) -> None:
        processor = self.processors.get(fields["model_key"])
        if processor is not None:
            await processor.reconcile()

    async def dispatch_worker(self) -> None:
        """Main loop: fetch requests and route them.

        Runs indefinitely. Errors in the loop are logged but don't terminate
        the dispatcher.
        """
        await self.startup()

        asyncio.create_task(self.heartbeat_worker())
        asyncio.create_task(self.status_worker())
        asyncio.create_task(self.env_worker())
        asyncio.create_task(self.events_worker())

        while True:
            try:
                requests = await self.get()

                for request in requests:
                    try:
                        await self.dispatch(request)
                    except Exception:
                        logger.exception(f"Error dispatching request {request.id}")
            except Exception:
                logger.exception("Error in dispatch worker")
                # Don't spin: while Redis is down every read fails at once.
                await asyncio.sleep(1)


@ray.remote(num_cpus=0, num_gpus=0, max_restarts=-1, resources={"head": 1})
class DispatcherActor(Dispatcher):
    """The Dispatcher as a detached actor on the head node.

    The dispatch loop is a task started here and not a method somebody calls,
    so a restart of the actor (``max_restarts=-1``) starts it again. Requests a
    previous incarnation had popped are gone with it.
    """

    def __init__(self) -> None:
        super().__init__()

        # Install the telemetry sinks in this actor's worker process: Loki for
        # logs, InfluxDB for the RECEIVED / QUEUED status-time points.
        from ....common.providers.influx import InfluxProvider
        from ....common.providers.loki import LokiProvider

        LokiProvider.connect()
        InfluxProvider.connect()

        # Redis can be across a WAN, where a connection can die without
        # closing. Give replies 10 s beyond the longest blocking read.
        RedisProvider.async_socket_timeout = CONFIG.fetch_timeout_s + 10
        RedisProvider.connect()

        self.task = asyncio.create_task(self.dispatch_worker())


def app() -> None:
    """Launch the detached Dispatcher actor in the NDIF namespace."""
    # get_if_exists makes relaunch idempotent, as for the Controller.
    DispatcherActor.options(
        name="Dispatcher",
        namespace=NAMESPACE,
        lifetime="detached",
        get_if_exists=True,
        runtime_env={"env_vars": {"NDIF_SERVICE": "dispatcher"}},
    ).remote()


if __name__ == "__main__":
    # Driver-side launch: attach to the running head and schedule the actor.
    # The detached actor persists after this process exits.
    #
    # Through the module under its own name: an actor class defined in
    # ``__main__`` is shipped to its worker as a copy of this file's state.
    from ndif.services.ray.queue import dispatcher

    ray.init(address="auto", namespace=NAMESPACE, logging_level="error")
    dispatcher.app()
