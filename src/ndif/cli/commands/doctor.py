"""``ndif doctor`` — report the health of an NDIF install.

Read-only: it reports versions, binaries, GPU, and connectivity, and exits
non-zero if anything required is missing. It never installs or changes
anything — a service being down is reported, not treated as a failure.
"""

import importlib.metadata
import platform
import shutil
import subprocess
import sys

import click

from .. import config
from ..lib.checks import check_api, check_minio, check_ray, check_redis


def _ok(msg: str) -> None:
    click.echo(f"  ✓ {msg}")


def _bad(msg: str, hint: str | None = None) -> None:
    click.echo(f"  ✗ {msg}")
    if hint:
        click.echo(f"      → {hint}")


def _check_environment() -> int:
    failures = 0
    if sys.version_info[:2] >= (3, 12):
        _ok(f"Python {platform.python_version()}")
    else:
        _bad(f"Python {platform.python_version()} — need 3.12+")
        failures += 1
    for pkg in ("ndif", "nnsight"):
        try:
            _ok(f"{pkg} {importlib.metadata.version(pkg)}")
        except importlib.metadata.PackageNotFoundError:
            _bad(f"{pkg} not installed", f"pip install {pkg}")
            failures += 1
    return failures


def _check_binaries() -> int:
    failures = 0
    for binary, hint in [
        ("ray", "pip install 'ray[default]'"),
        ("redis-server", "install redis (e.g. your package manager or conda)"),
        ("minio", "install the MinIO server binary"),
    ]:
        path = shutil.which(binary)
        if path:
            _ok(f"{binary} → {path}")
        else:
            _bad(f"{binary} not on PATH", hint)
            failures += 1
    return failures


def _check_gpu() -> int:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
            _ok(f"{len(lines)}× GPU ({lines[0]})")
            return 0
        _bad("nvidia-smi returned no GPUs", "check the NVIDIA driver")
        return 1
    except FileNotFoundError:
        _bad("nvidia-smi not found", "install the NVIDIA driver — NDIF needs a CUDA GPU")
        return 1
    except Exception as e:
        _bad(f"GPU detection failed: {e}")
        return 1


def _report_connectivity() -> None:
    """Reachability is informational — a stopped service is not a failure."""
    for name, var, fn in [
        ("redis", "NDIF_REDIS_URL", check_redis),
        ("minio", "NDIF_OBJECT_STORE_URL", check_minio),
        ("api", "NDIF_API_URL", check_api),
        ("ray", "NDIF_RAY_ADDRESS", check_ray),
    ]:
        url = config.get(var)
        if fn(url):
            _ok(f"{name} reachable at {url}")
        else:
            click.echo(f"  ○ {name} not reachable at {url} (start it with `ndif start {name}`)")


@click.command()
def doctor():
    """Report versions, binaries, GPU, and connectivity. Non-zero exit on failure."""
    failures = 0

    click.echo("Environment")
    failures += _check_environment()

    click.echo("\nBinaries")
    failures += _check_binaries()

    click.echo("\nCompute")
    failures += _check_gpu()

    click.echo("\nConnectivity")
    _report_connectivity()

    click.echo()
    if failures:
        click.echo(f"✗ {failures} issue{'s' if failures != 1 else ''} found.", err=True)
        sys.exit(1)
    click.echo("All checks passed.")
