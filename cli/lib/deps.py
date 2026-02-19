"""Dependency management for NDIF CLI - micromamba, Redis, MinIO"""

import os
import shutil
import subprocess
import time
from pathlib import Path

from .checks import check_redis, check_minio
from .session import get_or_create_session


# Micromamba configuration
MICROMAMBA_DIR = Path(os.environ.get("MICROMAMBA_DIR", Path.home() / ".local" / "bin"))
MICROMAMBA_BIN = MICROMAMBA_DIR / "micromamba"
MAMBA_ROOT = Path.home() / ".ndif" / "micromamba"
NDIF_ENV_NAME = "ndif-deps"

# Data directories for services
REDIS_DATA_DIR = Path.home() / ".ndif" / "data" / "redis"
OBJECT_STORE_DATA_DIR = Path.home() / ".ndif" / "data" / "object-store"


# =============================================================================
# Micromamba management
# =============================================================================


def is_micromamba_installed() -> bool:
    """Check if micromamba is installed."""
    return MICROMAMBA_BIN.exists() and os.access(MICROMAMBA_BIN, os.X_OK)


def install_micromamba() -> bool:
    """Download and install micromamba.

    Returns:
        True if installation succeeded, False otherwise
    """
    import platform
    import tempfile

    arch = platform.machine()
    if arch == "x86_64":
        plat = "linux-64"
    elif arch == "aarch64":
        plat = "linux-aarch64"
    else:
        return False

    url = f"https://micro.mamba.pm/api/micromamba/{plat}/latest"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tarball = tmpdir / "micromamba.tar.bz2"

        # Download using curl or wget
        if shutil.which("curl"):
            result = subprocess.run(
                ["curl", "-fsSLo", str(tarball), url],
                capture_output=True
            )
        elif shutil.which("wget"):
            result = subprocess.run(
                ["wget", "-qO", str(tarball), url],
                capture_output=True
            )
        else:
            return False

        if result.returncode != 0:
            return False

        # Extract
        result = subprocess.run(
            ["tar", "-xjf", str(tarball), "-C", str(tmpdir)],
            capture_output=True
        )
        if result.returncode != 0:
            return False

        # Move binary to install location
        MICROMAMBA_DIR.mkdir(parents=True, exist_ok=True)
        extracted_bin = tmpdir / "bin" / "micromamba"
        shutil.move(str(extracted_bin), str(MICROMAMBA_BIN))
        MICROMAMBA_BIN.chmod(0o755)

    return True


def ensure_micromamba() -> bool:
    """Ensure micromamba is installed, installing if necessary.

    Returns:
        True if micromamba is available, False otherwise
    """
    if is_micromamba_installed():
        return True

    return install_micromamba()


# =============================================================================
# Package management
# =============================================================================


def is_package_installed(package: str) -> bool:
    """Check if a package is installed in the micromamba environment.

    Args:
        package: Package/binary name to check

    Returns:
        True if package is available
    """
    # First check if it's available system-wide
    if shutil.which(package):
        return True

    # Check in micromamba environment
    if not is_micromamba_installed():
        return False

    result = subprocess.run(
        [str(MICROMAMBA_BIN), "run", "-r", str(MAMBA_ROOT), "-n", NDIF_ENV_NAME,
         "bash", "-c", f"command -v {package}"],
        capture_output=True
    )
    return result.returncode == 0


def install_package(package: str, conda_package: str = None) -> bool:
    """Install a package in the micromamba environment.

    Args:
        package: Binary name to check for
        conda_package: Conda package name (if different from binary name)

    Returns:
        True if installation succeeded
    """
    if not ensure_micromamba():
        return False

    conda_pkg = conda_package or package

    # Create environment if it doesn't exist
    env_path = MAMBA_ROOT / "envs" / NDIF_ENV_NAME
    if not env_path.exists():
        result = subprocess.run(
            [str(MICROMAMBA_BIN), "create", "-y", "-r", str(MAMBA_ROOT),
             "-n", NDIF_ENV_NAME],
            capture_output=True
        )
        if result.returncode != 0:
            return False

    # Install package
    result = subprocess.run(
        [str(MICROMAMBA_BIN), "install", "-y", "-r", str(MAMBA_ROOT),
         "-n", NDIF_ENV_NAME, "-c", "conda-forge", conda_pkg],
        capture_output=True
    )
    return result.returncode == 0


def ensure_package(package: str, conda_package: str = None) -> bool:
    """Ensure a package is available, installing via micromamba if needed.

    Args:
        package: Binary name to check for
        conda_package: Conda package name (if different from binary name)

    Returns:
        True if package is available
    """
    if is_package_installed(package):
        return True

    return install_package(package, conda_package)


def get_binary_path(binary: str) -> str:
    """Get the path to a binary, checking system PATH first, then micromamba.

    Args:
        binary: Binary name

    Returns:
        Path to binary, or None if not found
    """
    # Check system PATH first
    system_path = shutil.which(binary)
    if system_path:
        return system_path

    # Check micromamba environment
    if not is_micromamba_installed():
        return None

    result = subprocess.run(
        [str(MICROMAMBA_BIN), "run", "-r", str(MAMBA_ROOT), "-n", NDIF_ENV_NAME,
         "bash", "-c", f"command -v {binary}"],
        capture_output=True,
        text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return None


def run_in_micromamba_env(command: list, **kwargs) -> subprocess.Popen:
    """Run a command in the micromamba environment.

    Args:
        command: Command and arguments as list
        **kwargs: Additional arguments to pass to Popen

    Returns:
        Popen object
    """
    full_command = [
        str(MICROMAMBA_BIN), "run", "-r", str(MAMBA_ROOT), "-n", NDIF_ENV_NAME
    ] + command

    return subprocess.Popen(full_command, **kwargs)


# =============================================================================
# Redis management
# =============================================================================


def ensure_redis() -> bool:
    """Ensure Redis is available."""
    return ensure_package("redis-server", "redis-server")


def start_redis(port: int = 6379, verbose: bool = False) -> tuple:
    """Start Redis server.

    Args:
        port: Port to listen on
        verbose: If True, show output

    Returns:
        Tuple of (success: bool, pid: int or None, message: str)
    """
    # Ensure Redis is available
    if not ensure_redis():
        return False, None, "Failed to install Redis"

    # Create data directory
    REDIS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running on this port
    if check_redis(f"redis://localhost:{port}/"):
        return True, None, f"Redis already running on port {port}"

    # Build command
    redis_bin = get_binary_path("redis-server")
    if redis_bin:
        # System Redis
        cmd = [redis_bin, "--port", str(port), "--dir", str(REDIS_DATA_DIR)]
    else:
        # Micromamba Redis
        cmd = ["redis-server", "--port", str(port), "--dir", str(REDIS_DATA_DIR)]

    # Set up output handling
    session = get_or_create_session()
    log_dir = session.get_service_log_dir("broker")
    log_file = log_dir / "output.log"

    if verbose:
        stdout = None
        stderr = None
    else:
        log_handle = open(log_file, "w")
        stdout = log_handle
        stderr = subprocess.STDOUT

    # Start Redis
    if redis_bin:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True
        )
    else:
        proc = run_in_micromamba_env(
            cmd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True
        )

    # Wait a moment and check if it started
    time.sleep(1)
    if proc.poll() is not None:
        return False, None, "Redis failed to start"

    # Verify it's responding
    time.sleep(0.5)
    if not check_redis(f"redis://localhost:{port}/"):
        proc.terminate()
        return False, None, "Redis started but not responding"

    return True, proc.pid, f"Redis started on port {port}"


# =============================================================================
# Object Store management (MinIO)
# =============================================================================


def ensure_object_store() -> bool:
    """Ensure object store (MinIO) server is available."""
    # conda-forge package 'minio-server' provides the 'minio' binary
    return ensure_package("minio", "minio-server")


def start_object_store(port: int = 27018, console_port: int = None,
                       verbose: bool = False) -> tuple:
    """Start object store (MinIO) server.

    Args:
        port: S3 API port
        console_port: Web console port (defaults to port + 1)
        verbose: If True, show output

    Returns:
        Tuple of (success: bool, pid: int or None, message: str)
    """
    # Default console port to API port + 1 to avoid conflicts
    if console_port is None:
        console_port = port + 1
    # Ensure MinIO is available
    if not ensure_object_store():
        return False, None, "Failed to install object store (MinIO)"

    # Create data directory
    OBJECT_STORE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running on this port
    if check_minio(f"http://localhost:{port}"):
        return True, None, f"Object store already running on port {port}"

    # Get MinIO binary path (system PATH or micromamba)
    minio_bin = get_binary_path("minio")

    # Set environment for MinIO
    env = os.environ.copy()
    env["MINIO_ROOT_USER"] = env.get("MINIO_ROOT_USER", "minioadmin")
    env["MINIO_ROOT_PASSWORD"] = env.get("MINIO_ROOT_PASSWORD", "minioadmin")

    cmd_args = [
        "server", str(OBJECT_STORE_DATA_DIR),
        "--address", f":{port}",
        "--console-address", f":{console_port}"
    ]

    # Set up output handling
    session = get_or_create_session()
    log_dir = session.get_service_log_dir("object-store")
    log_file = log_dir / "output.log"

    if verbose:
        stdout = None
        stderr = None
    else:
        log_handle = open(log_file, "w")
        stdout = log_handle
        stderr = subprocess.STDOUT

    # Start MinIO
    if minio_bin:
        proc = subprocess.Popen(
            [minio_bin] + cmd_args,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True
        )
    else:
        proc = run_in_micromamba_env(
            ["minio"] + cmd_args,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True
        )

    # Wait a moment and check if it started
    time.sleep(1)
    if proc.poll() is not None:
        return False, None, "Object store failed to start"

    # Verify it's responding (give it a bit more time)
    for _ in range(5):
        time.sleep(0.5)
        if check_minio(f"http://localhost:{port}"):
            break
    else:
        proc.terminate()
        return False, None, "Object store started but not responding"

    return True, proc.pid, f"Object store started on port {port}"
