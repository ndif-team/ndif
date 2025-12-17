import os
from pathlib import Path

def get_repo_root() -> Path:
    """Get the repository root directory"""
    current_file = Path(__file__).resolve()
    # Go up from ndif/commands/start.py -> ndif/ -> repo_root/
    return current_file.parent.parent.parent

def get_pid_dir() -> Path:
    """Get directory for storing PIDs"""
    pid_dir = Path.home() / ".ndif" / "pids"
    return pid_dir


def get_pid(service: str) -> int:
    """Get saved PID for a service"""
    pid_file = get_pid_dir() / f"{service}.pid"
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_pid(service: str, pid: int):
    """Save a service PID to file"""
    pid_file = get_pid_dir() / f"{service}.pid"
    pid_file.write_text(str(pid))


def clear_pid(service: str):
    """Remove saved PID file"""
    pid_file = get_pid_dir() / f"{service}.pid"
    if pid_file.exists():
        pid_file.unlink()


def is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running"""
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
        return True
    except (OSError, ProcessLookupError):
        return False
