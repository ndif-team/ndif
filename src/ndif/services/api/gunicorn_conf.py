"""Gunicorn config for the NDIF API.

Settings are env-overridable. ``post_fork`` wires telemetry into each web
worker.

Telemetry / fork note: the Loki + InfluxDB providers connect at import (each
owns a background shipper thread). Threads don't survive ``fork()``, so a thread
created in the master would be dead in every forked worker. The rule that keeps
this safe: **the master must not import the providers before it forks.** So
this config imports no provider at module level, and ``post_fork`` imports them
*in each worker* — the import connects them there, with a live thread.

(Assumes the default ``preload_app = False`` — preloading would import the app,
and its providers, into the master before forking, reintroducing the problem.)

Loaded via ``gunicorn -c python:ndif.services.api.gunicorn_conf``.
"""

import os

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
