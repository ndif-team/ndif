# ndif/src/services/ray/tests/conftest.py
import sys
import importlib
from pathlib import Path
import types
import pytest

# ensure src/ is on path so ndif_ray is importable
#sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 1) Put the *service* src on sys.path so ndif_ray is importable
SERVICE_SRC = Path(__file__).resolve().parents[1] / "src"  # .../services/ray/src
sys.path.insert(0, str(SERVICE_SRC))

# 2) Force stdlib 'logging' (avoid shadowing by ./src/logging)
stdlib_logging = importlib.import_module("logging")  # stdlib
sys.modules["logging"] = stdlib_logging  # lock it before any project import

# 3) Stub 'logging_loki' so your internal logger module can import it
if "logging_loki" not in sys.modules:
    logging_loki = types.ModuleType("logging_loki")
    # Provide a minimal LokiHandler to satisfy imports
    class LokiHandler(stdlib_logging.Handler):
        def __init__(self, *a, **k): super().__init__()
        def emit(self, record): pass
    logging_loki.LokiHandler = LokiHandler
    sys.modules["logging_loki"] = logging_loki

# 4) (Only if you still import any ray internals later — usually not needed after renaming to ndif_ray)
# You can add targeted stubs similarly with sys.modules if something else pops up.

@pytest.fixture
def freeze_time(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000.0, raising=True)

