from multiprocessing import Process
from src.queue.dispatcher import Dispatcher

def on_starting(server):

    Process(target=Dispatcher.start, daemon=False).start()