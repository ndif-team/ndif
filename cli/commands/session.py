"""Session management for NDIF CLI.

A session represents the lifetime of NDIF services from `ndif start` to `ndif stop`.
Sessions store configuration, track which services are running, and provide a
consistent way to find and manage NDIF processes.

Session directory structure:
    $NDIF_SESSION_ROOT/              # Default: ~/.ndif
    ├── current -> session_xxx/      # Symlink to active session
    └── session_20250121_143052/
        ├── config.json              # Session configuration
        └── logs/
            ├── api/
            ├── ray/
            ├── broker/
            └── object-store/
"""

import json
import os
import getpass
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# =============================================================================
# Environment Variable Definitions
# =============================================================================

# All canonical NDIF environment variables with their defaults
ENV_VARS = {
    # Session management
    "NDIF_SESSION_ROOT": os.path.expanduser("~/.ndif"),

    # Service URLs/addresses
    "NDIF_BROKER_URL": "redis://localhost:6389/",
    "NDIF_OBJECT_STORE_URL": "http://localhost:27019",
    "NDIF_API_URL": "http://localhost:8001",
    "NDIF_RAY_ADDRESS": "ray://localhost:10001",

    # Ports (derived from URLs by default, but can be overridden)
    "NDIF_BROKER_PORT": "6389",
    "NDIF_OBJECT_STORE_PORT": "27019",
    "NDIF_API_PORT": "8001",
    "NDIF_API_HOST": "0.0.0.0",

    # Ray configuration
    "NDIF_RAY_TEMP_DIR": "/tmp/mripa/ray",
    "NDIF_RAY_HEAD_PORT": "6385",
    "NDIF_RAY_DASHBOARD_PORT": "8265",
    "NDIF_RAY_DASHBOARD_HOST": "0.0.0.0",
    "NDIF_RAY_SERVE_PORT": "8262", # 8267
    "NDIF_RAY_OBJECT_MANAGER_PORT": "8076",
    "NDIF_RAY_DASHBOARD_GRPC_PORT": "8268",

    # Controller configuration
    "NDIF_CONTROLLER_IMPORT_PATH": "src.ray.deployments.controller.controller",
    "NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS": "0",

    # API configuration
    "NDIF_API_WORKERS": "1",
}


def get_env(name: str, default: str = None) -> str:
    """Get an environment variable with NDIF default fallback.

    Args:
        name: Environment variable name (with or without NDIF_ prefix)
        default: Override default (uses ENV_VARS default if not provided)

    Returns:
        Environment variable value
    """
    # Normalize name to include NDIF_ prefix
    if not name.startswith("NDIF_"):
        name = f"NDIF_{name}"

    env_default = ENV_VARS.get(name)
    final_default = default if default is not None else env_default

    return os.environ.get(name, final_default)


def get_session_root() -> Path:
    """Get the session root directory, creating it if needed.

    Returns:
        Path to session root directory

    Raises:
        PermissionError: If directory cannot be created or accessed
    """
    root = Path(get_env("NDIF_SESSION_ROOT"))

    try:
        root.mkdir(parents=True, exist_ok=True)
        # Verify we can write to it
        test_file = root / ".write_test"
        test_file.touch()
        test_file.unlink()
        return root
    except PermissionError as e:
        raise PermissionError(
            f"Cannot access session directory '{root}'. "
            f"Set NDIF_SESSION_ROOT to a writable location.\n"
            f"Example: export NDIF_SESSION_ROOT=/tmp/{getpass.getuser()}/ndif"
        ) from e


# =============================================================================
# Service Configuration
# =============================================================================

@dataclass
class ServiceConfig:
    """Configuration for a single service."""
    name: str
    port: int
    managed: bool = True  # True if CLI manages lifecycle, False if external
    running: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ServiceConfig":
        return cls(**data)


# =============================================================================
# Session Configuration
# =============================================================================

@dataclass
class SessionConfig:
    """Complete session configuration."""

    # Session metadata
    session_id: str
    created_at: str

    # Service URLs
    broker_url: str
    object_store_url: str
    api_url: str
    ray_address: str

    # Ray configuration
    ray_temp_dir: str
    ray_head_port: int
    ray_dashboard_port: int
    ray_dashboard_host: str
    ray_serve_port: int
    ray_object_manager_port: int
    ray_dashboard_grpc_port: int

    # API configuration
    api_host: str
    api_port: int
    api_workers: int

    # Broker/object store ports
    broker_port: int
    object_store_port: int

    # Controller configuration
    controller_import_path: str
    minimum_deployment_time_seconds: int

    # Service states
    services: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        # Convert services to dict format
        data["services"] = {
            name: svc if isinstance(svc, dict) else svc.to_dict()
            for name, svc in self.services.items()
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SessionConfig":
        # Convert services back to ServiceConfig objects
        services_data = data.pop("services", {})
        config = cls(**data)
        config.services = {
            name: ServiceConfig.from_dict(svc) if isinstance(svc, dict) else svc
            for name, svc in services_data.items()
        }
        return config

    @classmethod
    def from_environment(cls, session_id: str = None) -> "SessionConfig":
        """Create a SessionConfig from current environment variables.

        Args:
            session_id: Optional session ID (generated if not provided)

        Returns:
            New SessionConfig with values from environment
        """
        if session_id is None:
            session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")

        # Parse ports from URLs
        broker_url = get_env("NDIF_BROKER_URL")
        object_store_url = get_env("NDIF_OBJECT_STORE_URL")
        api_url = get_env("NDIF_API_URL")
        ray_address = get_env("NDIF_RAY_ADDRESS")

        broker_port = urlparse(broker_url).port or int(get_env("NDIF_BROKER_PORT"))
        object_store_port = urlparse(object_store_url).port or int(get_env("NDIF_OBJECT_STORE_PORT"))
        api_port = urlparse(api_url).port or int(get_env("NDIF_API_PORT"))

        config = cls(
            session_id=session_id,
            created_at=datetime.now().isoformat(),

            # URLs
            broker_url=broker_url,
            object_store_url=object_store_url,
            api_url=api_url,
            ray_address=ray_address,

            # Ray config
            ray_temp_dir=get_env("NDIF_RAY_TEMP_DIR"),
            ray_head_port=int(get_env("NDIF_RAY_HEAD_PORT")),
            ray_dashboard_port=int(get_env("NDIF_RAY_DASHBOARD_PORT")),
            ray_dashboard_host=get_env("NDIF_RAY_DASHBOARD_HOST"),
            ray_serve_port=int(get_env("NDIF_RAY_SERVE_PORT")),
            ray_object_manager_port=int(get_env("NDIF_RAY_OBJECT_MANAGER_PORT")),
            ray_dashboard_grpc_port=int(get_env("NDIF_RAY_DASHBOARD_GRPC_PORT")),

            # API config
            api_host=get_env("NDIF_API_HOST"),
            api_port=api_port,
            api_workers=int(get_env("NDIF_API_WORKERS")),

            # Ports
            broker_port=broker_port,
            object_store_port=object_store_port,

            # Controller config
            controller_import_path=get_env("NDIF_CONTROLLER_IMPORT_PATH"),
            minimum_deployment_time_seconds=int(get_env("NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS")),

            # Initialize services as managed but not running
            services={
                "api": ServiceConfig(name="api", port=api_port, managed=True, running=False),
                "ray": ServiceConfig(name="ray", port=int(get_env("NDIF_RAY_HEAD_PORT")), managed=True, running=False),
                "broker": ServiceConfig(name="broker", port=broker_port, managed=True, running=False),
                "object-store": ServiceConfig(name="object-store", port=object_store_port, managed=True, running=False),
            },
        )

        return config


# =============================================================================
# Session Management
# =============================================================================

class Session:
    """Manages an NDIF session."""

    def __init__(self, config: SessionConfig, path: Path):
        self.config = config
        self.path = path
        self._config_file = path / "config.json"

    @property
    def logs_dir(self) -> Path:
        """Get the logs directory for this session."""
        logs = self.path / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs

    def get_service_log_dir(self, service: str) -> Path:
        """Get the log directory for a specific service."""
        log_dir = self.logs_dir / service
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def save(self):
        """Save session configuration to disk."""
        self._config_file.write_text(
            json.dumps(self.config.to_dict(), indent=2)
        )

    def mark_service_running(self, service: str, running: bool = True):
        """Mark a service as running or stopped."""
        if service in self.config.services:
            self.config.services[service].running = running
            self.save()

    def is_service_running(self, service: str) -> bool:
        """Check if a service is marked as running in the session."""
        if service in self.config.services:
            return self.config.services[service].running
        return False

    def get_running_services(self) -> list:
        """Get list of services marked as running."""
        return [
            name for name, svc in self.config.services.items()
            if svc.running
        ]

    def has_any_running_services(self) -> bool:
        """Check if any services are marked as running."""
        return any(svc.running for svc in self.config.services.values())

    @classmethod
    def load(cls, path: Path) -> Optional["Session"]:
        """Load a session from disk.

        Args:
            path: Path to session directory

        Returns:
            Session object or None if invalid
        """
        config_file = path / "config.json"
        if not config_file.exists():
            return None

        try:
            data = json.loads(config_file.read_text())
            config = SessionConfig.from_dict(data)
            return cls(config, path)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    @classmethod
    def create(cls, session_id: str = None) -> "Session":
        """Create a new session.

        Args:
            session_id: Optional session ID (generated if not provided)

        Returns:
            New Session object
        """
        config = SessionConfig.from_environment(session_id)
        root = get_session_root()
        path = root / config.session_id
        path.mkdir(parents=True, exist_ok=True)

        session = cls(config, path)
        session.save()

        # Update 'current' symlink
        current_link = root / "current"
        if current_link.is_symlink() or current_link.exists():
            current_link.unlink()
        current_link.symlink_to(path.name)

        return session


def get_current_session() -> Optional[Session]:
    """Get the current active session, if any.

    Returns:
        Current Session or None if no active session
    """
    try:
        root = get_session_root()
    except PermissionError:
        return None

    current_link = root / "current"
    if not current_link.is_symlink():
        return None

    session_path = root / current_link.resolve().name
    if not session_path.exists():
        return None

    return Session.load(session_path)


def get_or_create_session() -> Session:
    """Get the current session or create a new one.

    Returns:
        Active Session object
    """
    session = get_current_session()
    if session is not None:
        return session
    return Session.create()


def clear_session():
    """Clear the current session (but don't delete files).

    Removes the 'current' symlink, indicating no active session.
    """
    try:
        root = get_session_root()
        current_link = root / "current"
        if current_link.is_symlink():
            current_link.unlink()
    except PermissionError:
        pass


def end_session(session: Session):
    """End a session after all services are stopped.

    Clears the current symlink if this is the current session.
    """
    try:
        root = get_session_root()
        current_link = root / "current"
        if current_link.is_symlink():
            # Check if this session is the current one
            if current_link.resolve().name == session.path.name:
                current_link.unlink()
    except PermissionError:
        pass


# =============================================================================
# Port-based Process Detection
# =============================================================================

def get_pids_on_port(port: int) -> list[int]:
    """Get list of PIDs listening on a specific port.

    Args:
        port: Port number to check

    Returns:
        List of PIDs using the port
    """
    import subprocess

    try:
        # Use lsof to find processes listening on the port
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0 and result.stdout.strip():
            return [
                int(pid.strip())
                for pid in result.stdout.strip().split("\n")
                if pid.strip().isdigit()
            ]
        return []

    except (FileNotFoundError, ValueError):
        return []


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use.

    Args:
        port: Port number to check

    Returns:
        True if port is in use
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("0.0.0.0", port))
            return result == 0
    except (socket.error, OSError):
        return False


def kill_processes_on_port(port: int, signal_num: int = 15) -> list[int]:
    """Kill processes listening on a port.

    Args:
        port: Port number
        signal_num: Signal to send (default: SIGTERM=15)

    Returns:
        List of PIDs that were signaled
    """
    import signal

    pids = get_pids_on_port(port)
    killed = []

    for pid in pids:
        try:
            os.kill(pid, signal_num)
            killed.append(pid)
        except (OSError, ProcessLookupError):
            pass

    return killed
