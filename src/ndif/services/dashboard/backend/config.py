"""Dashboard backend configuration.

Resolution order for every setting: env var > default.

Env vars
--------
NDIF_DASHBOARD_USERNAME           single admin username (required to log in)
NDIF_DASHBOARD_PASSWORD_HASH      bcrypt hash of the admin password (generate
                                  via ``python -m ndif.services.dashboard.\
                                  backend.auth hash <password>``)
NDIF_DASHBOARD_SESSION_SECRET     32+ byte random string used to sign cookies
NDIF_DASHBOARD_SESSION_TTL_DAYS   cookie TTL in days (default: 7)
NDIF_DASHBOARD_DATA_DIR           base dir for logs / state / schedule
                                  (default: ~/ndif_dashboard)
NDIF_DASHBOARD_FRONTEND_DIST      built Vue frontend dir to serve
                                  (default: <package>/frontend/dist)
NDIF_DASHBOARD_DEV_MODE           "true" disables auth (frontend dev only)

NDIF_API_URL                      reused from the rest of the stack — the
                                  dashboard reads this directly via Pydantic
                                  ``validation_alias`` to proxy ``/api/status``
                                  back through the public NDIF API.

NDIF_RAY_ADDRESS / NDIF_BROKER_URL inherited by the cli/lib calls (deploy /
                                  evict / restart / reconcile).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NDIF_DASHBOARD_", extra="ignore")

    username: str = Field(default="admin")
    password_hash: str = Field(default="")
    session_secret: str = Field(default="change-me-please-this-is-not-secure")
    session_ttl_days: int = Field(default=7)
    dev_mode: bool = Field(default=False)

    # The NDIF API URL the dashboard's /api/status proxy hits. Reuses the
    # standard NDIF_API_URL set by docker-compose for the rest of the stack;
    # falls back to NDIF_DASHBOARD_API_URL if set, then the default.
    ndif_api_url: str = Field(
        default="http://localhost:5001",
        validation_alias=AliasChoices("NDIF_DASHBOARD_API_URL", "NDIF_API_URL"),
    )

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
