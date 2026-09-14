"""Filesystem state for the CLI's background services.

Each service the CLI launches is detached (its own session/process group) and
tracked by a PID file under ``$NDIF_HOME`` (default ``~/.ndif``):

    $NDIF_HOME/run/<name>.pid    the tracked process id
    $NDIF_HOME/logs/<name>.log   combined stdout+stderr

Liveness is the PID file plus a ``kill(pid, 0)`` probe; a PID file whose
process is gone is treated as stopped and cleared lazily on read.
"""

import os
from pathlib import Path

from .util import pid_alive


class State:
    """Owns the run/log directories and PID-file bookkeeping."""

    def __init__(self, home: Path):
        self.home = home
        self.run_dir = home / "run"
        self.log_dir = home / "logs"

    @classmethod
    def from_env(cls) -> "State":
        home = Path(os.environ.get("NDIF_HOME", "~/.ndif")).expanduser()
        return cls(home)

    def ensure(self) -> None:
        """Create the run/log directories if they don't exist yet."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def pid_file(self, name: str) -> Path:
        return self.run_dir / f"{name}.pid"

    def log_file(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def read_pid(self, name: str) -> int | None:
        try:
            return int(self.pid_file(name).read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def write_pid(self, name: str, pid: int) -> None:
        self.pid_file(name).write_text(f"{pid}\n")

    def clear_pid(self, name: str) -> None:
        self.pid_file(name).unlink(missing_ok=True)

    def running_pid(self, name: str) -> int | None:
        """Live PID for ``name``, or ``None`` — clearing a stale PID file."""
        pid = self.read_pid(name)
        if pid is not None and pid_alive(pid):
            return pid
        if pid is not None:
            self.clear_pid(name)
        return None
