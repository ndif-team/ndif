"""Gunicorn config for the NDIF API.

Settings are env-overridable. ``on_starting`` launches the single queue
dispatcher (as a *spawned* process); ``post_fork`` wires telemetry into each
web worker.

Telemetry / fork note: the Loki + InfluxDB providers connect at import (each
owns a background shipper thread). Threads don't survive ``fork()``, so a thread
created in the master would be dead in every forked worker. The rule that keeps
this safe: **the master must not import the providers before it forks.** So:

  * this config imports neither the providers nor the dispatcher at module level
    (the dispatcher pulls the providers in transitively);
  * ``post_fork`` imports the providers *in each worker* — the import connects
    them there, with a live thread;
  * the dispatcher is started with ``spawn`` (a fresh interpreter, not a fork),
    via a target that imports everything lazily, so nothing provider-related is
    imported into the master.

(Assumes the default ``preload_app = False`` — preloading would import the app,
and its providers, into the master before forking, reintroducing the problem.)

Loaded via ``gunicorn -c python:ndif.services.api.gunicorn_conf``.
"""

import os
from multiprocessing import get_context

bind = f"0.0.0.0:{os.environ.get('NDIF_API_PORT', '8001')}"
workers = int(os.environ.get("NDIF_API_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.environ.get("NDIF_API_TIMEOUT", "120"))


def post_fork(server, worker):
    """Import the telemetry providers in each worker, after the fork.

    Importing them connects (the providers connect at import), so each worker
    gets its own live shipper threads rather than a dead one inherited from the
    master. Runs before the worker loads the app.
    """
    import ndif.common.providers.influx  # noqa: F401  (import connects it)
    import ndif.common.providers.loki  # noqa: F401  (import connects it)


def _run_dispatcher():
    """Entry point for the spawned dispatcher process.

    Everything is imported here (in the child), not at module top, so the master
    never imports the dispatcher's provider chain before it forks the workers.
    The provider imports connect Loki + InfluxDB in this fresh process.
    """
    import ndif.common.providers.influx  # noqa: F401  (import connects it)
    import ndif.common.providers.loki  # noqa: F401  (import connects it)

    from ndif.services.api.queue.dispatcher import Dispatcher

    Dispatcher.start()


def on_starting(server):
    """Start the single queue dispatcher as a spawned (not forked) process."""
    ctx = get_context("spawn")
    ctx.Process(
        target=_run_dispatcher, daemon=False, name="DispatcherProcess"
    ).start()
