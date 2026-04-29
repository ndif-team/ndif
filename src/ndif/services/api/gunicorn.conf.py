from multiprocessing import Process
from ndif.services.api.queue.dispatcher import Dispatcher


def on_starting(server):
    Process(target=Dispatcher.start, daemon=False, name="DispatcherProcess").start()
