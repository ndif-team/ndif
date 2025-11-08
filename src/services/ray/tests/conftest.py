# ndif/src/services/ray/tests/conftest.py
import sys
import importlib
from pathlib import Path
import types
import pytest

def _put_internal_src_on_path():
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "src",   # .../services/ray/tests -> .../services/ray/src
        Path.cwd() / "src",        # if running from the service root
    ]
    for c in candidates:
        if (c / "ray" / "__init__.py").exists():
            sys.path.insert(0, str(c))
            return str(c)
    return None

_src = _put_internal_src_on_path()

def _ensure_pkg(dotted: str) -> types.ModuleType:
    """
    Ensure a dotted module path exists in sys.modules as a *package* at each level.
    Each created module gets __path__ so that submodule imports work.
    """
    parts = dotted.split(".")
    cur = None
    path = []
    for part in parts:
        path.append(part)
        name = ".".join(path)
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            # mark as package so children like name.sub import work
            mod.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = mod
            if cur is not None:
                setattr(cur, part, mod)
        cur = mod
    return cur

# --- Create stubs BEFORE importing your modules ------------------------------

# 1) ray package (top-level)
ray_pkg = _ensure_pkg("ray")

# Provide minimal top-level APIs; tests will monkeypatch as needed.
if not hasattr(ray_pkg, "get_actor"):
    ray_pkg.get_actor = lambda *a, **k: None
if not hasattr(ray_pkg, "kill"):
    ray_pkg.kill = lambda *a, **k: None

# 2) ray._private and ray._private.state as *packages*
rp = _ensure_pkg("ray._private")
rp_state = _ensure_pkg("ray._private.state")
rp_deployment = _ensure_pkg("ray.deployments")


# Optional: add minimal symbols some code imports
if not hasattr(rp, "services"):
    rp.services = types.SimpleNamespace(get_node_ip_address=lambda *a, **k: "127.0.0.1")
# Leave rp_state mostly empty unless your code needs specific names.

# 3) Dashboard Serve SDK import path used by your code
sdk = _ensure_pkg("ray.dashboard.modules.serve.sdk")
if not hasattr(sdk, "ServeSubmissionClient"):
    class _FakeClient:
        def __init__(self, *a, **k): ...
        def deploy_app(self, *a, **k): return {"ok": True}
    sdk.ServeSubmissionClient = _FakeClient

# ---- Safety: ensure we loaded the *internal* ray, not external wheel --------
try:
    real_ray = importlib.import_module("ray")
except Exception as e:
    raise RuntimeError(
        "Could not import internal 'ray'. Ensure ./src/ray exists and run pytest from "
        "ndif/src/services/ray/ or below."
    ) from e

if "site-packages" in (getattr(real_ray, "__file__", "") or ""):
    raise RuntimeError(
        f"Loaded external Ray instead of internal one: {real_ray.__file__}\n"
        f"sys.path included: {_src}"
    )

# ---- Utilities available to tests ------------------------------------------

@pytest.fixture
def freeze_time(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1_000.0, raising=True)
