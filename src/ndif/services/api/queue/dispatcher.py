"""Dispatcher: route queued requests to per-model Processors.

Architecture:
    Redis queue -> Dispatcher -> Processor(s) -> Replica(s) -> model actor

The Dispatcher pops requests from Redis, lazily creates a Processor per
``model_key``, and hands the request off. Processors are self-healing — they
provision and re-provision replicas on demand — so the Dispatcher only drains
the shared error queue between batches, purging everything and reconnecting on a
connection error.

Run as a standalone process:
    python -m ndif.services.api.queue.dispatcher
"""

import asyncio
import json
import logging
import os
import pickle
import traceback

from ....common.providers.ray import RayProvider, controller_handle
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


class Dispatcher:
    """Central coordinator routing requests to per-model Processors."""

    def __init__(self) -> None:
        self.processors: dict[str, Processor] = {}

        # Processors report errors here; the dispatch loop drains them and, on a
        # connection error, purges everything and reconnects to Ray.
        self.error_queue: asyncio.Queue[tuple[str, Exception]] = asyncio.Queue()

        self.connect()

    @classmethod
    def start(cls) -> None:
        """Create a Dispatcher and run its dispatch loop forever (blocking).

        Telemetry (Loki + InfluxDB) is connected by the caller's provider imports
        — ``_run_dispatcher`` when spawned from gunicorn, or the ``__main__``
        block when run standalone — so it's live in this process (it emits the
        RECEIVED / QUEUED status-time points as requests move through the queue).
        """
        dispatcher = cls()
        logger.info(f"Starting dispatcher with PID {os.getpid()}")
        asyncio.run(dispatcher.dispatch_worker())

    def connect(self) -> None:
        """Block until connected to Ray, retrying every second.

        Maintains the ``ray:connected`` Redis flag so API endpoints can tell
        whether the cluster is reachable. Also called during error recovery.
        """
        logger.info("Connecting to Ray")

        RedisProvider.sync_client.delete("ray:connected")

        # Drop every Redis key derived from the cluster so nothing stale is
        # served across the reconnect gap. The cached status/env describe a
        # cluster we're no longer attached to; clearing the coalescing locks
        # too means the first request after reconnect triggers a fresh refresh
        # instead of briefly serving stale data or waiting out a lock's TTL.
        # (Called on first connect as well, where these keys are simply absent.)
        RedisProvider.sync_client.delete(
            STATUS_KEY, STATUS_REQUESTED_KEY, ENV_KEY, ENV_REQUESTED_KEY
        )

        while not RayProvider.connected():
            try:
                RayProvider.reset()
                RayProvider.connect()
            except Exception:
                logger.exception("Error connecting to Ray")
                # Synchronous on purpose: connect() runs before the event loop
                # starts (from __init__), so this can't be an async sleep.
                import time

                time.sleep(1)

        RedisProvider.sync_client.set("ray:connected", "1")
        logger.info("Connected to Ray")

    async def get(self) -> list[BackendRequestModel]:
        """Fetch pending requests from the Redis queue.

        Blocking-pop one (bounded by ``fetch_timeout_s`` so the loop can
        periodically drain evictions/errors when idle), then batch-pop up to
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
            self.processors[request.model_key] = Processor(
                request.model_key, self.error_queue
            )

        await self.processors[request.model_key].enqueue(
            request, prepend=request.priority
        )

    async def purge(self, message: str) -> None:
        """Error every queued user and drop every replica (critical failure).

        Processors are self-healing and re-used, so they're purged in place
        rather than removed — the next request re-provisions them.
        """
        for processor in list(self.processors.values()):
            try:
                await processor.purge(message)
            except Exception:
                logger.exception(
                    f"Error purging processor `{processor.model_key}`"
                )

    async def handle_errors(self) -> None:
        """Drain the error queue; on a connection error, purge and reconnect.

        With per-replica workers, individual request errors are already
        surfaced to the user and drift is recovered inside the worker; the
        dispatcher only needs to handle connection-level purge/reconnect.
        """
        if self.error_queue.empty():
            return

        errors: list[tuple[str, Exception]] = []
        has_connection_error = False

        while not self.error_queue.empty():
            name, error = self.error_queue.get_nowait()
            errors.append((name, error))
            if RayProvider.is_connection_error(error):
                has_connection_error = True

        if has_connection_error or not RayProvider.connected():
            logger.warning(
                f"Connection error detected "
                f"(has_connection_error={has_connection_error}); reconnecting...",
                extra={
                    "event": "ray_reconnect",
                    "has_connection_error": has_connection_error,
                    "purged_processors": len(self.processors),
                },
            )
            await self.purge(
                "Critical server error occurred. "
                "Please try again later. Sorry for the inconvenience."
            )
            self.connect()

        for name, error in errors:
            tb_str = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            logger.error(
                f"Error in component {name}: {error}\n{tb_str}",
                extra={
                    "component_name": name,
                    "error_type": type(error).__name__,
                    "is_connection_error": RayProvider.is_connection_error(error),
                },
            )

    async def status_worker(self) -> None:
        """Serve coalesced cluster-status refreshes for the API's /status.

        Blocks on the status trigger stream (one refresh is triggered per
        coalescing window, so this fetches at most once per window). On each
        trigger, pulls the heavy status from the controller (time-bounded),
        caches it, and wakes waiters. On failure it reports to the error queue
        (so the dispatcher rechecks the Ray connection), clears the coalescing
        lock, and wakes waiters with an error so they don't hang.
        """
        client = RedisProvider.async_client
        last_id = "$"

        while True:
            try:
                messages = await client.xread(
                    {STATUS_TRIGGER_STREAM: last_id}, block=0, count=1
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
            except Exception as e:
                logger.exception("status_worker: failed to refresh status")
                self.error_queue.put_nowait(("status_worker", e))
                await client.delete(STATUS_REQUESTED_KEY)
                await client.publish(STATUS_READY_CHANNEL, "error")
                # Yield so a tight failure loop (e.g. controller down) doesn't
                # peg the event loop before the trigger stream refills.
                await asyncio.sleep(0)

    async def env_worker(self) -> None:
        """Serve coalesced cluster-env refreshes for the API's /env.

        Structurally identical to :meth:`status_worker` but for the controller's
        ``env`` (python version + installed packages). The API can't reach Ray,
        so it triggers a refresh here and waits on the ready channel.
        """
        client = RedisProvider.async_client
        last_id = "$"

        while True:
            try:
                messages = await client.xread(
                    {ENV_TRIGGER_STREAM: last_id}, block=0, count=1
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
            except Exception as e:
                logger.exception("env_worker: failed to refresh env")
                self.error_queue.put_nowait(("env_worker", e))
                await client.delete(ENV_REQUESTED_KEY)
                await client.publish(ENV_READY_CHANNEL, "error")
                # Yield so a tight failure loop (e.g. controller down) doesn't
                # peg the event loop before the trigger stream refills.
                await asyncio.sleep(0)

    async def events_worker(self) -> None:
        """Serve CLI operational events: queue introspection, kill, reconcile.

        Blocks on the events stream and dispatches each entry by ``event_type``
        to a handler. Handlers are individually guarded so one bad event can't
        take the worker down. See :mod:`ndif.common.redis.events`.
        """
        client = RedisProvider.async_client
        last_id = "$"
        handlers = {
            EVENT_QUEUE_STATE: self._handle_queue_state,
            EVENT_KILL: self._handle_kill,
            EVENT_RECONCILE: self._handle_reconcile,
        }

        while True:
            try:
                messages = await client.xread({EVENTS_STREAM: last_id}, block=0, count=1)
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
                await asyncio.sleep(0)

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
                await replica.cancel("Request cancelled by operator.")
                return {"status": "cancelled_execution",
                        "message": f"Request {request_id} cancelled while executing."}

        return {"status": "not_found", "message": f"Request {request_id} not found."}

    async def _handle_reconcile(self, fields: dict) -> None:
        processor = self.processors.get(fields["model_key"])
        if processor is not None:
            await processor.reconcile()

    async def dispatch_worker(self) -> None:
        """Main loop: fetch requests, route them, drain evictions/errors.

        Runs indefinitely. Errors in the loop are logged but don't terminate
        the dispatcher.
        """
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

                await self.handle_errors()
            except Exception:
                logger.exception("Error in dispatch worker")
                continue


if __name__ == "__main__":
    # Importing the providers connects them in this process (Loki for logs,
    # InfluxDB for metrics) before the dispatch loop starts.
    import ndif.common.providers.influx  # noqa: F401  (import connects it)
    import ndif.common.providers.loki  # noqa: F401  (import connects it)

    logging.basicConfig(level=logging.INFO)
    Dispatcher.start()
