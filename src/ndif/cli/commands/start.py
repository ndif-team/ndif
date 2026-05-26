"""Start command for NDIF services"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import click

from ..lib.util import (
    get_service_dir,
    print_logo,
    spawn_service,
    terminate_process,
)
from ..lib.session import (
    Session,
    SessionConfig,
    clear_session,
    get_current_session,
    get_service_port,
    is_port_in_use,
    kill_processes_on_port,
)
from ..lib.checks import (
    CheckResult,
    check_minio,
    check_redis,
    preflight_check_api,
    preflight_check_broker,
    preflight_check_object_store,
    preflight_check_ray,
    preflight_check_worker,
    run_preflight_checks,
    wait_for_services,
)
from ..lib.deps import start_redis as util_start_redis
from ..lib.deps import start_object_store as util_start_object_store
from ..lib.model_config import config_exists, get_default_config_path


# =============================================================================
# Startup banner
# =============================================================================

def _print_startup_banner() -> None:
    """One-screen summary printed right after the logo. Mostly for orientation —
    "you're running NDIF X with Y GPUs and Z ports about to be bound"."""
    import importlib.metadata
    import platform
    import subprocess

    def _safe_version(pkg: str) -> str:
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            return "?"

    def _detect_gpus() -> list[tuple[str, str]]:
        """Returns a list of (name, memory) tuples, one per GPU."""
        try:
            r = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'],
                capture_output=True, text=True, check=False, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                out = []
                for line in r.stdout.strip().split('\n'):
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 2:
                        out.append((parts[0], parts[1]))
                return out
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    ndif_v = _safe_version('ndif')
    nns_v = _safe_version('nnsight')
    py_v = platform.python_version()
    gpus = _detect_gpus()
    dashboard_port = os.environ.get('NDIF_DASHBOARD_PORT')

    click.echo(f"NDIF {ndif_v}  (nnsight {nns_v}, python {py_v})")
    if gpus:
        name, mem = gpus[0]
        suffix = f"  (+ {len(gpus)-1} more)" if len(gpus) > 1 else ""
        click.echo(f"GPU: {len(gpus)}× {name} ({mem}){suffix}")
    else:
        click.echo("GPU: none detected (no nvidia-smi — model loads will fail or fall back to CPU)")

    api_port = os.environ.get('NDIF_API_PORT', '5001')
    obj_port = os.environ.get('NDIF_OBJECT_STORE_PORT', '27018')
    ray_dash = os.environ.get('NDIF_RAY_DASHBOARD_PORT', '8265')
    click.echo(f"Ports: API :{api_port}, MinIO :{obj_port}, Ray dashboard :{ray_dash}")

    if dashboard_port:
        click.echo(f"Dashboard: enabled on :{dashboard_port}")
    else:
        click.echo("Dashboard: disabled (set NDIF_DASHBOARD_PORT to enable)")
    click.echo()


# =============================================================================
# Per-service start helpers
# =============================================================================

def _start_deps_service(name: str, label: str, util_fn, port: int,
                        session: Session, verbose: bool) -> None:
    """Start a 'dependency' service (Redis, MinIO) via its util fn."""
    click.echo(f"Starting {label}...")
    success, pid, message = util_fn(port=port, verbose=verbose)
    if not success:
        raise RuntimeError(f"Failed to start {label}: {message}")

    session.mark_service_running(name, True)
    suffix = f" (PID: {pid})" if pid else ""
    click.echo(f"  ✓ {message}{suffix}")
    click.echo()


def _start_broker(session: Session, verbose: bool) -> None:
    _start_deps_service(
        'broker', 'broker (Redis)', util_start_redis,
        session.config.broker_port, session, verbose,
    )


def _start_object_store(session: Session, verbose: bool) -> None:
    _start_deps_service(
        'object-store', 'object store (MinIO)', util_start_object_store,
        session.config.object_store_port, session, verbose,
    )


def _start_api(session: Session, verbose: bool) -> subprocess.Popen:
    click.echo("Starting NDIF API service...")
    click.echo(f"  Port: {session.config.api_port}")
    click.echo(f"  Workers: {session.config.api_workers}")
    if verbose:
        click.echo(f"  Broker: {session.config.broker_url}")
        if session.config.object_store_url:
            click.echo(f"  Object Store: {session.config.object_store_url}")
        click.echo(f"  Ray: {session.config.ray_address}")

    log_file = session.get_service_log_dir('api') / "output.log"
    if verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env_updates = {
        'NDIF_BROKER_URL': session.config.broker_url,
        'NDIF_API_WORKERS': str(session.config.api_workers),
        'NDIF_RAY_ADDRESS': session.config.ray_address,
        'NDIF_API_PORT': str(session.config.api_port),
        'NDIF_API_URL': session.config.api_url,
        'NDIF_DEV_MODE': os.environ.get('NDIF_DEV_MODE', 'true'),
    }
    if session.config.object_store_url:
        env_updates['OBJECT_STORE_URL'] = session.config.object_store_url

    proc = spawn_service(
        get_service_dir("api"), "start.sh", env_updates, log_file, verbose,
    )
    session.mark_service_running('api', True)
    return proc


def _start_dashboard(session: Session, verbose: bool) -> subprocess.Popen:
    """Start the dashboard as part of the orchestrated stack.

    Unlike ``_start_dashboard_standalone`` (used by the explicit
    ``ndif start dashboard`` fast-path), this goes through the session
    machinery — captures logs, marks the service as running, and inherits
    localhost URLs from the session config so the dashboard's cron jobs
    can reach the local api/ray/broker.
    """
    port = os.environ.get('NDIF_DASHBOARD_PORT', '8081')
    click.echo("Starting NDIF dashboard service...")
    click.echo(f"  Port: {port}")
    if verbose:
        click.echo(f"  API: {session.config.api_url}")
        click.echo(f"  Ray: {session.config.ray_address}")
        click.echo(f"  Broker: {session.config.broker_url}")

    log_file = session.get_service_log_dir('dashboard') / "output.log"
    if verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env_updates = {
        'NDIF_DASHBOARD_PORT': port,
        'NDIF_API_URL': session.config.api_url,
        'NDIF_RAY_ADDRESS': session.config.ray_address,
        'NDIF_BROKER_URL': session.config.broker_url,
        'NDIF_DASHBOARD_MONITOR_URL': os.environ.get(
            'NDIF_DASHBOARD_MONITOR_URL', session.config.api_url,
        ),
    }

    proc = spawn_service(
        get_service_dir("dashboard"), "start.sh", env_updates, log_file, verbose,
    )
    session.mark_service_running('dashboard', True)
    return proc


def _start_ray(session: Session, verbose: bool) -> subprocess.Popen:
    click.echo("Starting NDIF Ray service...")
    if verbose:
        click.echo(f"  API: {session.config.api_url}")
        click.echo(f"  Temp Dir: {session.config.ray_temp_dir}")
    click.echo(f"  Head Port: {session.config.ray_head_port}")
    click.echo(f"  Dashboard: {session.config.ray_dashboard_port}")

    log_file = session.get_service_log_dir('ray') / "output.log"
    if verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env_updates = {
        'API_URL': session.config.api_url,
        'NDIF_RAY_TEMP_DIR': session.config.ray_temp_dir,
        'NDIF_RAY_HEAD_PORT': str(session.config.ray_head_port),
        'NDIF_RAY_OBJECT_MANAGER_PORT': str(session.config.ray_object_manager_port),
        'NDIF_RAY_DASHBOARD_PORT': str(session.config.ray_dashboard_port),
        'NDIF_RAY_DASHBOARD_GRPC_PORT': str(session.config.ray_dashboard_grpc_port),
        'NDIF_RAY_SERVE_PORT': str(session.config.ray_serve_port),
        'NDIF_CONTROLLER_IMPORT_PATH': session.config.controller_import_path,
        'NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS': str(session.config.minimum_deployment_time_seconds),
        'RAY_METRICS_GAUGE_EXPORT_INTERVAL_MS': '1000',
        'RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S': '10',
    }
    if session.config.object_store_url:
        env_updates['OBJECT_STORE_URL'] = session.config.object_store_url

    proc = spawn_service(
        get_service_dir("ray"), "start.sh", env_updates, log_file, verbose,
    )
    session.mark_service_running('ray', True)
    return proc


# =============================================================================
# Service registry
# =============================================================================
#
# Single source of truth that drives _determine_services_to_start, the
# preflight block, and the start block. Order is start order
# (broker -> object-store -> ray -> api).

@dataclass
class ServiceSpec:
    name: str
    # Whether this service needs starting given the current config + session.
    needs_start: Callable[[SessionConfig, Optional[Session]], bool]
    # Build the list of preflight checks for this service.
    preflight: Callable[[SessionConfig], list[CheckResult]]
    # Start the service. Returns a Popen for services whose lifetime we
    # track (ray, api), or None for dep services (broker, object-store).
    start: Callable[[Session, bool], Optional[subprocess.Popen]]


def _session_service_needs_start(port_attr: str, name: str):
    """Predicate: this service needs starting if there's no session, or if
    the session thinks it's running but its port isn't actually listening."""
    def _check(config: SessionConfig, sess: Optional[Session]) -> bool:
        if sess is None:
            return True
        port = getattr(config, port_attr)
        return not sess.is_service_running(name) or not is_port_in_use(port)
    return _check


def _dashboard_needs_start(config: SessionConfig, sess: Optional[Session]) -> bool:
    """Dashboard is opt-in: included only when NDIF_DASHBOARD_PORT is set.
    Same session-state check as ray/api on top, so re-running `ndif start all`
    doesn't try to double-start a healthy dashboard."""
    if not os.environ.get('NDIF_DASHBOARD_PORT'):
        return False
    if sess is None:
        return True
    port = config.dashboard_port
    return not sess.is_service_running('dashboard') or not is_port_in_use(port)


SERVICES: list[ServiceSpec] = [
    ServiceSpec(
        name='broker',
        needs_start=lambda c, _s: not check_redis(c.broker_url),
        preflight=lambda c: preflight_check_broker(c.broker_port),
        start=_start_broker,
    ),
    ServiceSpec(
        name='object-store',
        needs_start=lambda c, _s: not check_minio(c.object_store_url),
        preflight=lambda c: preflight_check_object_store(c.object_store_port),
        start=_start_object_store,
    ),
    ServiceSpec(
        name='ray',
        needs_start=_session_service_needs_start('ray_head_port', 'ray'),
        preflight=lambda c: preflight_check_ray(
            c.ray_temp_dir,
            c.ray_head_port,
            c.ray_dashboard_port,
            c.ray_object_manager_port,
            c.ray_dashboard_grpc_port,
            c.ray_serve_port,
        ),
        start=_start_ray,
    ),
    ServiceSpec(
        name='api',
        needs_start=_session_service_needs_start('api_port', 'api'),
        preflight=lambda c: preflight_check_api(c.api_port),
        start=_start_api,
    ),
    # Dashboard is opt-in via the ``NDIF_DASHBOARD_PORT`` env var. When it's
    # set (in shell, .env, or container ENV), ``ndif start all`` includes
    # the dashboard. ``.env.example`` deliberately leaves it commented out so
    # default invocations don't pull it in. The explicit ``ndif start
    # dashboard`` invocation still uses the fast-path below (no session,
    # no preflight, no env var required).
    ServiceSpec(
        name='dashboard',
        needs_start=_dashboard_needs_start,
        preflight=lambda _c: [],
        start=_start_dashboard,
    ),
]

SERVICES_BY_NAME = {s.name: s for s in SERVICES}


# =============================================================================
# Main command
# =============================================================================

@click.command()
@click.argument('service', type=click.Choice(
    ['api', 'ray', 'broker', 'object-store', 'dashboard', 'all'],
    case_sensitive=False
), default='all')
@click.option('--worker', is_flag=True, help='Start as Ray worker node (connects to existing head)')
@click.option('--verbose', is_flag=True, help='Run in foreground with logs visible (blocking mode)')
@click.option('--timeout', type=int, default=120, help='Timeout in seconds for services to become ready (default: 120)')
@click.option('--api-url', envvar='NDIF_API_URL', show_envvar=True, default=None, help='API URL')
@click.option('--broker-url', envvar='NDIF_BROKER_URL', show_envvar=True, default=None, help='Broker URL')
@click.option('--object-store-url', envvar='NDIF_OBJECT_STORE_URL', show_envvar=True, default=None, help='Object store URL')
@click.option('--ray-address', envvar='NDIF_RAY_ADDRESS', show_envvar=True, default=None, help='Ray head address for worker mode')
@click.option('--ray-dashboard-port', envvar='NDIF_RAY_DASHBOARD_PORT', show_envvar=True, type=int, default=None, help='Ray dashboard port')
def start(service: str, worker: bool, verbose: bool, timeout: int,
          api_url: str, broker_url: str, object_store_url: str, ray_address: str,
          ray_dashboard_port: int):
    """Start NDIF services.

    SERVICE: Which service to start (api, ray, broker, object-store, or all). Default: all

    All pre-flight checks run before any services start. If any check fails,
    nothing is started. If a service fails to start, all started services are
    stopped and the session is removed.

    \b
    Examples:
        ndif start                    # Start all services (head node)
        ndif start api                # Start API only
        ndif start broker             # Start broker (Redis) only
        ndif start --verbose          # Start with logs visible
        ndif start --worker           # Start as Ray worker node

    CLI arguments take precedence over environment variables.
    """
    print_logo()
    _print_startup_banner()

    overrides = dict(
        broker_url=broker_url,
        object_store_url=object_store_url,
        api_url=api_url,
        ray_address=ray_address,
        ray_dashboard_port=ray_dashboard_port,
    )

    if worker:
        _start_worker_mode(verbose, ray_address=ray_address)
        return

    # Dashboard is a leaf service — independent of the SessionConfig /
    # preflight machinery. It just exec's its start.sh.
    if service == 'dashboard':
        proc = _start_dashboard_standalone(verbose)
        if verbose and proc is not None:
            try:
                proc.wait()
            except KeyboardInterrupt:
                terminate_process(proc)
        return

    existing_session = get_current_session()
    if existing_session:
        click.echo(f"Existing session: {existing_session.config.session_id}")
        click.echo()

    config = SessionConfig.from_environment(**overrides)

    services_to_start = _determine_services_to_start(service, config, existing_session)

    if not services_to_start:
        click.echo("All requested services are already running.")
        click.echo("\nUse 'ndif info' to see session status.")
        return

    if verbose:
        click.echo(f"Services to start: {', '.join(services_to_start)}")
        click.echo()

    # Pre-flight: run every check, fail fast on the first service that errors.
    click.echo("Running pre-flight checks...")
    for name in services_to_start:
        if verbose:
            click.echo(f"  {name.capitalize()}:")
        if not run_preflight_checks(SERVICES_BY_NAME[name].preflight(config), verbose=verbose):
            _preflight_failed()

    if verbose:
        click.echo("\n✓ All pre-flight checks passed")
    else:
        click.echo("  ✓ All pre-flight checks passed")
    click.echo()

    # Create or reuse session.
    if existing_session:
        session = existing_session
    else:
        session = Session.create(config)
        if verbose:
            click.echo(f"Session: {session.config.session_id}")
            click.echo(f"  Logs: {session.logs_dir}")
            click.echo()

    started_services: list[str] = []
    processes: list[tuple[str, subprocess.Popen]] = []

    try:
        for name in services_to_start:
            proc = SERVICES_BY_NAME[name].start(session, verbose)
            started_services.append(name)
            if proc is not None:
                processes.append((name, proc))
    except Exception as e:
        click.echo(f"\n✗ Failed to start services: {e}", err=True)
        _rollback(session, started_services, processes, existing_session is None)
        sys.exit(1)

    if verbose and processes:
        click.echo("\n✓ Services started in verbose mode. Press Ctrl+C to stop.")
        click.echo("=" * 60)
        try:
            for _, proc in processes:
                proc.wait()
        except KeyboardInterrupt:
            click.echo("\n\nStopping services...")
            _rollback(session, started_services, processes, existing_session is None)
            sys.exit(0)
        return

    click.echo("Waiting for services to be ready...")
    success, failed = wait_for_services(
        broker_url=session.config.broker_url if 'broker' in started_services else None,
        minio_url=session.config.object_store_url if 'object-store' in started_services else None,
        ray_address=session.config.ray_address if 'ray' in started_services else None,
        api_url=session.config.api_url if 'api' in started_services else None,
        timeout=timeout,
    )

    if not success:
        click.echo(f"\n✗ Services failed to become ready: {', '.join(failed)}", err=True)
        _rollback(session, started_services, processes, existing_session is None)
        sys.exit(1)

    click.echo("\n✓ All services ready.")
    click.echo()
    click.echo(f"Session: {session.config.session_id}")
    click.echo(f"  Logs: {session.logs_dir}")

    if 'ray' in started_services and config_exists():
        _auto_deploy_models(session)

    click.echo("\nTo view logs:")
    for name in started_services:
        if name in ('api', 'ray'):
            click.echo(f"  ndif logs {name}")
    click.echo("\nTo view session info: ndif info")
    click.echo("To stop services: ndif stop")


def _determine_services_to_start(service: str, config: SessionConfig,
                                 existing_session: Optional[Session]) -> list[str]:
    """Walk SERVICES in order and pick the ones that aren't already up."""
    return [
        spec.name for spec in SERVICES
        if (service == 'all' or service == spec.name)
        and spec.needs_start(config, existing_session)
    ]


def _preflight_failed():
    """Exit after pre-flight check failure."""
    click.echo("\n✗ Pre-flight checks failed. Fix the issues above and try again.", err=True)
    sys.exit(1)


def _rollback(session: Session, started_services: list[str],
              processes: list[tuple[str, subprocess.Popen]], delete_session: bool):
    """Roll back after a failure - stop all started services."""
    click.echo("Rolling back...")

    for _name, proc in processes:
        terminate_process(proc)

    if 'ray' in started_services:
        try:
            subprocess.run(['ray', 'stop'], capture_output=True, check=False)
        except Exception:
            pass

    for svc in started_services:
        port = get_service_port(session, svc)
        if port and is_port_in_use(port):
            kill_processes_on_port(port)

    for svc in started_services:
        session.mark_service_running(svc, False)

    if delete_session:
        clear_session()
        click.echo("Session removed due to startup failure.")


# =============================================================================
# Dashboard (standalone) + auto-deploy + worker mode
# =============================================================================

def _start_dashboard_standalone(verbose: bool) -> subprocess.Popen:
    """Run the dashboard's start.sh — no SessionConfig, no preflight.

    The dashboard is a leaf service that depends only on env vars (see
    ``services/dashboard/start.sh`` for the full list). It runs primarily
    inside docker-compose where the rest of the ``ndif start`` machinery
    (Ray ports, broker checks, model_config auto-deploy) is irrelevant.
    For the standalone non-Docker path we still want the same script to
    work, just without dragging the NDIF session in.
    """
    click.echo("Starting NDIF dashboard service...")
    if verbose:
        click.echo(f"  Port: {os.environ.get('NDIF_DASHBOARD_PORT', '8081')}")
        click.echo(f"  Data dir: {os.environ.get('NDIF_DASHBOARD_DATA_DIR', '~/ndif_dashboard')}")
    click.echo()

    return spawn_service(
        get_service_dir("dashboard"), "start.sh",
        env_updates=None, log_file=None, verbose=True,
    )


def _wait_for_controller(ray_address: str, timeout: int = 120) -> bool:
    """Wait for the controller actor to be available."""
    import ray
    import time

    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                ray.get_actor("Controller", namespace="NDIF")
                return True
            except ValueError:
                time.sleep(2)
        return False
    finally:
        try:
            ray.shutdown()
        except Exception:
            pass


def _auto_deploy_models(session: Session):
    """Auto-deploy models from ~/.ndif/models.yaml if it exists."""
    from .deploy import deploy as deploy_cmd
    from click.testing import CliRunner

    config_path = get_default_config_path()
    click.echo()
    click.echo(f"Auto-deploying models from {config_path}...")

    click.echo("Waiting for controller...")
    if not _wait_for_controller(session.config.ray_address):
        click.echo("Warning: Controller not available, skipping auto-deploy", err=True)
        return

    runner = CliRunner()
    result = runner.invoke(deploy_cmd, [
        '-f', str(config_path),
        '--ray-address', session.config.ray_address,
        '--broker-url', session.config.broker_url,
    ])

    if result.output:
        # Skip the "Loaded N model(s)" line — we already said we're auto-deploying.
        for line in result.output.strip().split('\n')[1:]:
            click.echo(line)

    if result.exit_code != 0:
        click.echo("Warning: Auto-deploy encountered errors", err=True)


def _start_worker_mode(verbose: bool, ray_address: Optional[str] = None):
    """Handle starting as a Ray worker node."""

    existing_session = get_current_session()
    if existing_session:
        if existing_session.config.node_type == "head":
            click.echo("Error: A head node session already exists on this machine.", err=True)
            click.echo("Cannot run both head and worker on the same machine.", err=True)
            click.echo("\nUse 'ndif stop' to stop the head node first.", err=True)
            sys.exit(1)
        elif existing_session.config.node_type == "worker":
            if existing_session.is_service_running("ray-worker"):
                click.echo("Error: A worker session is already running.", err=True)
                sys.exit(1)

    config = SessionConfig.from_environment(node_type="worker", ray_address=ray_address)

    click.echo("Starting Ray worker node...")
    click.echo(f"  Connecting to: {config.ray_address}")
    click.echo(f"  Temp dir: {config.ray_temp_dir}")
    click.echo()

    click.echo("Running pre-flight checks...")
    checks = preflight_check_worker(config.ray_temp_dir, config.ray_address)
    if not run_preflight_checks(checks):
        _preflight_failed()

    click.echo("\n✓ All pre-flight checks passed")
    click.echo()

    session = Session.create(config)
    click.echo(f"Session: {session.config.session_id}")
    click.echo(f"  Logs: {session.logs_dir}")
    click.echo()

    try:
        proc = _start_ray_worker(session, verbose)
    except Exception as e:
        click.echo(f"\n✗ Failed to start worker: {e}", err=True)
        clear_session()
        sys.exit(1)

    if verbose and proc:
        click.echo("\n✓ Worker started in verbose mode. Press Ctrl+C to stop.")
        click.echo("=" * 60)
        try:
            proc.wait()
        except KeyboardInterrupt:
            click.echo("\n\nStopping worker...")
            terminate_process(proc)
            subprocess.run(['ray', 'stop'], capture_output=True, check=False)
            session.mark_service_running('ray-worker', False)
            clear_session()
            sys.exit(0)
    else:
        click.echo("\n✓ Worker started successfully.")
        click.echo("\nTo view logs:")
        click.echo("  ndif logs ray")
        click.echo("\nTo stop: ndif stop")


def _start_ray_worker(session: Session, verbose: bool) -> subprocess.Popen:
    """Start the Ray worker service."""
    log_file = session.get_service_log_dir('ray') / "output.log"
    if not verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env_updates = {
        'NDIF_RAY_TEMP_DIR': session.config.ray_temp_dir,
        'NDIF_RAY_ADDRESS': session.config.ray_address,
    }

    proc = spawn_service(
        get_service_dir("ray"), "start-worker.sh", env_updates, log_file, verbose,
    )
    session.mark_service_running('ray-worker', True)
    return proc
