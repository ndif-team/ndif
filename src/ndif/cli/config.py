"""CLI configuration: NDIF_* defaults and environment assembly.

The services read every knob straight from the environment (provider ``CONFIG``
specs and the ``start.sh`` scripts). The CLI only reads the handful it needs
directly — ports and URLs for ``info``/``doctor`` — and guarantees a few
local-run defaults that differ from the service defaults tuned for docker's
per-container network.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from dotenv import load_dotenv

# Overlaid *beneath* the real environment: anything set in the shell or a .env
# wins. These make single-host ``ndif start`` work out of the box, where the
# service defaults either collide (Ray's own GCS port is 6379, same as Redis)
# or assume docker service-name hosts.
DEFAULTS: dict[str, str] = {
    "NDIF_HOME": str(Path("~/.ndif").expanduser()),
    "NDIF_REDIS_URL": "redis://localhost:6379",
    "NDIF_OBJECT_STORE_URL": "http://localhost:9000",
    "NDIF_API_URL": "http://localhost:8001",
    "NDIF_API_PORT": "8001",
    "NDIF_RAY_ADDRESS": "ray://localhost:10001",
    "NDIF_RAY_HEAD_PORT": "6385",
    "NDIF_RAY_DASHBOARD_PORT": "8265",
}

# CLI options that each set a single env var the services read.
ENV_OPTIONS = {
    "redis_url": "NDIF_REDIS_URL",
    "ray_address": "NDIF_RAY_ADDRESS",
    "api_port": "NDIF_API_PORT",
    "ray_head_address": "NDIF_RAY_HEAD_ADDRESS",
}


def load_env_files(env_file: str | None = None) -> None:
    """Load a CWD-relative ``.env``, then an explicit ``--env-file`` on top."""
    load_dotenv(Path.cwd() / ".env")
    if env_file:
        load_dotenv(env_file, override=True)


def get(name: str) -> str | None:
    """Value for an NDIF_* var: the environment, else the CLI default."""
    return os.environ.get(name, DEFAULTS.get(name))


def build_env(env_pairs: tuple[str, ...] = (), typed: dict | None = None) -> dict:
    """The environment to hand a spawned service.

    ``DEFAULTS`` underneath, the real environment on top, then CLI overrides
    last: ``-e KEY=VALUE`` pairs and the typed shortcuts (``--redis-url`` etc.).
    """
    env = {**DEFAULTS, **os.environ}
    for pair in env_pairs:
        if "=" not in pair:
            raise click.BadParameter(f"expected KEY=VALUE, got {pair!r}", param_hint="-e")
        key, value = pair.split("=", 1)
        env[key] = value
    for opt, var in ENV_OPTIONS.items():
        value = (typed or {}).get(opt)
        if value is not None:
            env[var] = str(value)
    return env
