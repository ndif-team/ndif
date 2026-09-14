"""The services the CLI can start, and how each is launched.

``ray`` and ``api`` reuse their service directory's ``start.sh`` verbatim
(every knob is env-driven, so the CLI just injects the environment). ``redis``
and ``minio`` are external servers spawned directly, deriving their ports from
the matching ``NDIF_*`` URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import click

# .../src/ndif/services — sibling of this cli package.
SERVICES_DIR = Path(__file__).resolve().parents[1] / "services"


def _script_command(relative: str) -> Callable[[dict], list[str]]:
    """A launcher that runs one of the service ``start.sh`` scripts."""
    path = SERVICES_DIR / relative
    return lambda env: ["bash", str(path)]


def _redis_command(env: dict) -> list[str]:
    """Spawn ``redis-server`` on the port from ``NDIF_REDIS_URL``."""
    port = urlparse(env.get("NDIF_REDIS_URL", "redis://localhost:6379")).port or 6379
    return ["redis-server", "--port", str(port), "--daemonize", "no"]


def _minio_command(env: dict) -> list[str]:
    """Spawn ``minio server`` on the port from ``NDIF_OBJECT_STORE_URL``."""
    port = urlparse(env.get("NDIF_OBJECT_STORE_URL", "http://localhost:9000")).port or 9000
    console = env.get("NDIF_OBJECT_STORE_CONSOLE_PORT", "9001")
    data_dir = Path(env.get("NDIF_HOME", "~/.ndif")).expanduser() / "minio"
    data_dir.mkdir(parents=True, exist_ok=True)
    return ["minio", "server", str(data_dir),
            "--address", f":{port}", "--console-address", f":{console}"]


def _minio_env(env: dict) -> dict:
    """MinIO reads its root credentials from its own env vars, not NDIF_*."""
    return {
        "MINIO_ROOT_USER": env.get("NDIF_OBJECT_STORE_ACCESS_KEY", "minioadmin"),
        "MINIO_ROOT_PASSWORD": env.get("NDIF_OBJECT_STORE_SECRET_KEY", "minioadmin"),
    }


@dataclass(frozen=True)
class Service:
    name: str
    build_command: Callable[[dict], list[str]]
    description: str
    # Extra process env this service needs beyond the shared NDIF_* environment.
    build_env: Callable[[dict], dict] = lambda env: {}


# The core stack, ordered by startup dependency: redis/minio come up before the
# ray and api that use them. `start`/`stop` with no args walk this list (stop
# reverses). Whether ray comes up as head or worker is decided by
# NDIF_RAY_HEAD_ADDRESS (see ray/start.sh), not by anything here.
SERVICES: list[Service] = [
    Service("redis", _redis_command, "Redis (request queue + response pub/sub)"),
    Service("minio", _minio_command, "MinIO object store (execution results)", _minio_env),
    Service("ray", _script_command("ray/start.sh"), "Ray node + NDIF controller (head)"),
    Service("api", _script_command("api/start.sh"), "FastAPI served by gunicorn"),
]

# Opt-in services: startable/stoppable by name (`ndif start dashboard`) but not
# pulled in by a bare `ndif start`.
OPTIONAL_SERVICES: list[Service] = [
    Service("dashboard", _script_command("dashboard/start.sh"),
            "Dashboard (Vue UI + FastAPI): deploy/evict/status, schedules, monitor"),
]

SERVICE_MAP: dict[str, Service] = {s.name: s for s in SERVICES + OPTIONAL_SERVICES}


def env_services() -> tuple[str, ...]:
    """Service names from ``NDIF_SERVICE`` (space/comma separated), if set."""
    return tuple(os.environ.get("NDIF_SERVICE", "").replace(",", " ").split())


def resolve_targets(names, *, default: list[Service]) -> list[Service]:
    """Map service names to ``Service`` objects, or fall back to ``default``.

    Empty (or the literal ``all``) yields ``default``. De-duplicates while
    preserving the order given; errors on unknown names.
    """
    if not names or list(names) == ["all"]:
        return default
    unknown = [n for n in names if n not in SERVICE_MAP]
    if unknown:
        raise click.BadParameter(
            f"unknown service(s): {', '.join(unknown)}. "
            f"choose from: {', '.join(SERVICE_MAP)}"
        )
    seen: dict[str, Service] = {}
    for n in names:
        seen[n] = SERVICE_MAP[n]
    return list(seen.values())
