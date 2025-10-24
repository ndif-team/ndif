from multiprocessing import Process
from src.queue.coordinator import Coordinator

def on_starting(server):

    Process(target=Coordinator.start, daemon=False).start()