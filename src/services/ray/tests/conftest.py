# ndif/src/services/ray/tests/conftest.py
import sys
import importlib
from pathlib import Path
import types
import pytest

# 1) Put the service src on sys.path so ndif_ray is importable
SERVICE_SRC = Path(__file__).resolve().parents[1] / "src"  # .../services/ray/src
sys.path.insert(0, str(SERVICE_SRC))

# 2) Force stdlib 'logging' (avoid shadowing by ./src/logging)
stdlib_logging = importlib.import_module("logging")
sys.modules["logging"] = stdlib_logging

# 3) Stub 'logging_loki' so your internal logger can import it
if "logging_loki" not in sys.modules:
    logging_loki = types.ModuleType("logging_loki")
    class LokiHandler(stdlib_logging.Handler):
        def __init__(self, *a, **k): super().__init__()
        def emit(self, record): pass
    logging_loki.LokiHandler = LokiHandler
    sys.modules["logging_loki"] = logging_loki

# 4) Robust Ray stub: make 'ray' a *package* and add subpackages you need
def _ensure_pkg(dotted: str) -> types.ModuleType:
    """
    Ensure a dotted module path exists in sys.modules as a *package* at each level
    (i.e., has __path__), so that child imports work (e.g., ray._private.state).
    """
    parts = dotted.split(".")
    cur_name = ""
    parent = None
    for p in parts:
        cur_name = f"{cur_name+'.' if cur_name else ''}{p}"
        mod = sys.modules.get(cur_name)
        if mod is None:
            mod = types.ModuleType(cur_name)
            mod.__path__ = []  # mark as package
            sys.modules[cur_name] = mod
            if parent:
                setattr(parent, p, mod)
        parent = mod
    return sys.modules[dotted]

# Create 'ray' as a package and provide APIs used by your code under test
ray_pkg = _ensure_pkg("ray")

# Minimal functions used in deployment.py
def _default_get_actor(*a, **k):
    raise RuntimeError("actor not found (stubbed)")
if not hasattr(ray_pkg, "get_actor"):
    ray_pkg.get_actor = _default_get_actor
if not hasattr(ray_pkg, "kill"):
    ray_pkg.kill = lambda *a, **k: None

# Subpackages some of your modules import
rp = _ensure_pkg("ray._private")
rp_state = _ensure_pkg("ray._private.state")

# Provide the exact attribute your code imports: from ray._private import services
if not hasattr(rp, "services"):
    rp.services = types.SimpleNamespace(
        get_node_ip_address=lambda *a, **k: "127.0.0.1"
    )
    
# 👉 Provide GlobalState in ray._private.state
if not hasattr(rp_state, "GlobalState"):
    class GlobalState:
        def __init__(self, *a, **k): pass
        # Ray versions differ; expose both names just in case
        def initialize_global_state(self, *a, **k): pass
        def _initialize_global_state(self, *a, **k): pass
        def disconnect(self, *a, **k): pass
        # Common query helpers some code calls; return safe defaults
        def node_table(self, *a, **k): return []
        def job_table(self, *a, **k): return []
        def cluster_resources(self, *a, **k): return {}
        def available_resources(self, *a, **k): return {}
    rp_state.GlobalState = GlobalState

# If anything references the dashboard Serve SDK, stub that too
sdk = _ensure_pkg("ray.dashboard.modules.serve.sdk")
if not hasattr(sdk, "ServeSubmissionClient"):
    class _FakeClient:
        def __init__(self, *a, **k): ...
        def deploy_app(self, *a, **k): return {"ok": True}
    sdk.ServeSubmissionClient = _FakeClient
    
#end of ray stub

# Utility fixture you already had
@pytest.fixture
def freeze_time(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000.0, raising=True)
