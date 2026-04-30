"""Dashboard backend configuration.

Resolution order for every setting: env var > default. The defaults are aimed
at "running ``run.sh`` on the head node" — most paths point under ``~/ndif_dashboard``.

Env vars
--------
DASHBOARD_USERNAME            single admin username (required to log in)
DASHBOARD_PASSWORD_HASH       bcrypt hash of the admin password (use the
                              ``ndif.services.dashboard.backend.auth`` CLI
                              helper to generate one)
DASHBOARD_SESSION_SECRET      32+ byte random string used to sign cookies
DASHBOARD_SESSION_TTL_DAYS    cookie TTL in days (default: 7)
DASHBOARD_DATA_DIR            base dir for logs/state/schedule (default:
                              ~/ndif_dashboard)
DASHBOARD_FRONTEND_DIST       built Vue frontend dir to serve (default:
                              <package>/frontend/dist)
DASHBOARD_DEV_MODE            "true" disables auth (handy for frontend dev)
NDIF_RAY_ADDRESS / NDIF_BROKER_URL — inherited by the cli/lib calls.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_", extra="ignore")

    username: str = Field(default="admin")
    password_hash: str = Field(default="")
    session_secret: str = Field(default="change-me-please-this-is-not-secure")
    session_ttl_days: int = Field(default=7)
    dev_mode: bool = Field(default=False)

    # NDIF API base URL — used for read-only /status proxying so the dashboard
    # doesn't need a Ray client connection just to render the deployments page.
    ndif_api_url: str = Field(default="http://localhost:5001")

    data_dir: Path = Field(default_factory=lambda: Path.home() / "ndif_dashboard")
    frontend_dist: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "frontend" / "dist"
    )

    # --- Derived paths ---------------------------------------------------
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def schedule_path(self) -> Path:
        return self.data_dir / "schedule.json"

    @property
    def reconcile_state_path(self) -> Path:
        return self.data_dir / ".reconcile.state.json"

    @property
    def monitor_config_path(self) -> Path:
        return self.data_dir / "config.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s
