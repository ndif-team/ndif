import sys
from typing import Callable


class StdoutRedirect:
    def __init__(self, fn: Callable):
        self.fn = fn
        self.stdout = sys.stdout

    def __enter__(self):
        sys.stdout = self

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self.stdout

    def write(self, text: str):
        if text.strip():
            self.fn(text)

    def flush(self):
        pass
