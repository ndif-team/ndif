# ndif/src/services/ray/tests/conftest.py
import sys
import importlib
from pathlib import Path
import types
import pytest

# 1) Put the *service* src on sys.path so ndif_ray is importable
SERVICE_SRC = Path(__file__).resolve().parents[1] / "src"  # .../services/ray/src
sys.path.insert(0, str(SERVICE_SRC))

# 2) Force stdlib 'logging' (avoid shadowing by ./src/logging)
stdlib_logging = importlib.import_module("logging")  # stdlib
sys.modules["logging"] = stdlib_logging  # lock it before any project import

# 3) Stub 'logging_loki' so your internal logger module can import it
if "logging_loki" not in sys.modules:
    logging_loki = types.ModuleType("logging_loki")
    class LokiHandler(stdlib_logging.Handler):
        def __init__(self, *a, **k): super().__init__()
        def emit(self, record): pass
    logging_loki.LokiHandler = LokiHandler
    sys.modules["logging_loki"] = logging_loki

# 4) Stub the EXTERNAL ray library (your internal package is now ndif_ray)
#    deployment.py does `import ray` to call get_actor/kill; we provide a tiny fake.
if "ray" not in sys.modules:
    ray_stub = types.ModuleType("ray")

    # Default behavior: act like "actor not found" so tests can assert error-path logging.
    def _default_get_actor(*a, **k):
        raise RuntimeError("actor not found (stubbed)")

    ray_stub.get_actor = _default_get_actor
    ray_stub.kill = lambda *a, **k: None

    # If other tests later need more symbols, you can add them here
    # e.g., ray_stub.util = types.SimpleNamespace(placement_group=lambda *a, **k: None)

    sys.modules["ray"] = ray_stub

@pytest.fixture
def freeze_time(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000.0, raising=True)
