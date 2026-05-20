"""`ndif doctor` — comprehensive health check for an NDIF install."""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import click

from ..lib.checks import (
    CheckResult,
    check_api,
    check_directory_writable,
    check_port_available,
    check_redis,
    check_ray,
)
from ..lib.session import get_current_session, get_env, is_port_in_use


@click.command()
def doctor():
    """Run health checks on the NDIF install.

    Reports the state of:
      - Python / ndif / nnsight versions
      - Required binaries (redis-server, minio, ray, nvidia-smi)
      - GPU detection
      - Filesystem (session + HF cache writability)
      - Ports (free or owned by our session)
      - Active session + service reachability

    Each check shows ✓ or ✗ with a suggestion when it fails. Exits with a
    non-zero status if any check fails — handy in CI / pre-deploy gates.
    """
    sections: list[tuple[str, list[CheckResult]]] = [
        ("Environment",   _check_environment()),
        ("System",        _check_system()),
        ("Compute",       _check_compute()),
        ("Filesystem",    _check_filesystem()),
        ("Ports",         _check_ports()),
        ("Session",       _check_session_state()),
    ]

    failures = 0
    for name, results in sections:
        click.echo(f"\n{name}")
        for r in results:
            mark = "✓" if r.success else "✗"
            click.echo(f"  {mark} {r.message}")
            if not r.success:
                failures += 1
            if r.details:
                click.echo(f"      {r.details}")
            if r.suggestion and not r.success:
                click.echo(f"      → {r.suggestion}")

    click.echo()
    if failures == 0:
        click.echo("All checks passed.")
    else:
        click.echo(f"✗ {failures} issue{'s' if failures != 1 else ''} found.", err=True)
        sys.exit(1)


# =============================================================================
# Check sections
# =============================================================================

def _check_environment() -> list[CheckResult]:
    results: list[CheckResult] = []

    py_v = tuple(sys.version_info[:2])
    if py_v >= (3, 12):
        results.append(CheckResult(True, f"Python {platform.python_version()}"))
    else:
        results.append(CheckResult(
            False, f"Python {platform.python_version()} — too old",
            suggestion="ndif requires Python 3.12 or newer.",
        ))

    for pkg, optional in [('ndif', False), ('nnsight', False)]:
        try:
            v = importlib.metadata.version(pkg)
            results.append(CheckResult(True, f"{pkg} {v} installed"))
        except importlib.metadata.PackageNotFoundError:
            results.append(CheckResult(
                False, f"{pkg} not installed",
                suggestion=f"pip install {pkg}",
            ))
    return results


def _check_system() -> list[CheckResult]:
    """Required binaries. Only `ray` is hard-required; the CLI auto-bootstraps
    redis/minio via micromamba on first start."""
    results: list[CheckResult] = []

    ray_path = shutil.which('ray')
    if ray_path:
        results.append(CheckResult(True, f"ray found at {ray_path}"))
    else:
        results.append(CheckResult(
            False, "ray not on PATH",
            suggestion="`pip install ray[serve]` or install ndif's deps with `pip install ndif`.",
        ))

    for binary in ('redis-server', 'minio'):
        path = shutil.which(binary)
        if path:
            results.append(CheckResult(True, f"{binary} found at {path}"))
        else:
            # Not a failure — the CLI auto-installs via micromamba on first start.
            results.append(CheckResult(
                True, f"{binary} not on PATH (will auto-install via micromamba on first start)",
            ))
    return results


def _check_compute() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
             '--format=csv,noheader'],
            capture_output=True, text=True, check=False, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(r.stderr.strip() or 'nvidia-smi returned no GPUs')

        # Each line is "name, memory, driver_version". Collapse if all GPUs
        # are identical (the common case) — long-form list otherwise.
        lines = [tuple(p.strip() for p in l.split(',')) for l in r.stdout.strip().split('\n') if l.strip()]
        if len(set(lines)) == 1:
            name, mem, driver = lines[0]
            results.append(CheckResult(True, f"{len(lines)}× {name} ({mem}), driver {driver}"))
        else:
            results.append(CheckResult(
                True, f"{len(lines)} GPUs detected",
                details=" | ".join(", ".join(g) for g in lines),
            ))
    except FileNotFoundError:
        results.append(CheckResult(
            False, "nvidia-smi not found",
            suggestion="Install the NVIDIA driver — ndif requires a CUDA GPU.",
        ))
    except Exception as e:
        results.append(CheckResult(
            False, "GPU detection failed",
            details=str(e),
            suggestion="Check `nvidia-smi` manually; the driver may be broken.",
        ))
    return results


def _check_filesystem() -> list[CheckResult]:
    return [
        _label(check_directory_writable(get_env("NDIF_SESSION_ROOT")),
               "Session dir"),
        _label(check_directory_writable(Path.home() / ".cache" / "huggingface"),
               "HF cache"),
    ]


def _check_ports() -> list[CheckResult]:
    """Distinguish 'free' from 'in use by us' (session port that's live)."""
    results: list[CheckResult] = []
    session = get_current_session()

    ports = [
        ('API',            int(get_env("NDIF_API_PORT") or 5001)),
        ('Broker',         int(get_env("NDIF_BROKER_PORT") or 6379)),
        ('Object store',   int(get_env("NDIF_OBJECT_STORE_PORT") or 27018)),
        ('Ray head',       int(get_env("NDIF_RAY_HEAD_PORT") or 6385)),
        ('Ray dashboard',  int(get_env("NDIF_RAY_DASHBOARD_PORT") or 8265)),
    ]
    if (dport := get_env("NDIF_DASHBOARD_PORT")):
        ports.append(('Dashboard', int(dport)))

    session_ports = {
        session.config.api_port,
        session.config.ray_head_port,
        session.config.broker_port,
        session.config.object_store_port,
    } if session else set()

    for label, port in ports:
        if is_port_in_use(port):
            if port in session_ports:
                results.append(CheckResult(True, f"{label} (:{port}): in use by this session"))
            else:
                r = check_port_available(port, label)
                r.success = False  # in-use by *something else* is a problem
                results.append(r)
        else:
            results.append(CheckResult(True, f"{label} (:{port}): free"))
    return results


def _check_session_state() -> list[CheckResult]:
    session = get_current_session()
    if session is None:
        return [CheckResult(True, "No active session (nothing running)")]

    results: list[CheckResult] = [
        CheckResult(True, f"Active session: {session.config.session_id}"),
    ]

    checks = [
        ('Broker',   check_redis, session.config.broker_url),
        ('API',      check_api,   session.config.api_url),
        ('Ray',      check_ray,   session.config.ray_address),
    ]
    for label, fn, url in checks:
        if fn(url):
            results.append(CheckResult(True, f"{label} reachable at {url}"))
        else:
            results.append(CheckResult(
                False, f"{label} not reachable at {url}",
                suggestion=f"Run `ndif start {label.lower()}` (or `ndif logs {label.lower()}` for diagnostics).",
            ))
    return results


# =============================================================================
# Helpers
# =============================================================================

def _label(r: CheckResult, prefix: str) -> CheckResult:
    """Prepend a category label to a check's message."""
    r.message = f"{prefix}: {r.message}"
    return r
