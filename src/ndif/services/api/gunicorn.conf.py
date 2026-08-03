import logging
import threading
import time
from multiprocessing import Process

from ndif.services.api.queue.dispatcher import Dispatcher

logger = logging.getLogger("gunicorn.error")

DISPATCHER_RESTART_BACKOFF_S = 3


def _supervise_dispatcher() -> None:
    # The Dispatcher is the single process that drains the Redis queue and
    # delivers responses. If it dies the API workers stay healthy but no
    # request is ever dispatched — a silent outage. Keep it alive.
    while True:
        proc = Process(target=Dispatcher.start, daemon=False, name="DispatcherProcess")
        proc.start()
        proc.join()
        logger.error(
            "DispatcherProcess (pid %s) exited with code %s; restarting in %ss",
            proc.pid,
            proc.exitcode,
            DISPATCHER_RESTART_BACKOFF_S,
        )
        time.sleep(DISPATCHER_RESTART_BACKOFF_S)


def on_starting(server):
    threading.Thread(
        target=_supervise_dispatcher, name="DispatcherSupervisor", daemon=True
    ).start()
