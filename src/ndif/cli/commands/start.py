"""Start command for NDIF services"""

import os
import subprocess
import sys
from pathlib import Path

import click

from ..lib.util import get_service_dir, print_logo
from ..lib.session import (
    Session,
    SessionConfig,
    get_current_session,
    get_session_root,
    is_port_in_use,
    kill_processes_on_port,
)
from ..lib.checks import (
    check_redis,
    check_minio,
    preflight_check_api,
    preflight_check_ray,
    preflight_check_broker,
    preflight_check_object_store,
    run_preflight_checks,
    wait_for_services,
    preflight_check_worker,
)
from ..lib.deps import start_redis as util_start_redis, start_object_store as util_start_object_store
from ..lib.model_config import config_exists, get_default_config_path


def _apply_cli_overrides(api_url: str = None, broker_url: str = None,
                         object_store_url: str = None, ray_address: str = None,
                         ray_dashboard_port: int = None):
    """Apply CLI argument overrides to environment variables.

    CLI arguments take precedence over environment variables.
    """
    if api_url is not None:
        os.environ['NDIF_API_URL'] = api_url
    if broker_url is not None:
        os.environ['NDIF_BROKER_URL'] = broker_url
    if object_store_url is not None:
        os.environ['NDIF_OBJECT_STORE_URL'] = object_store_url
    if ray_address is not None:
        os.environ['NDIF_RAY_ADDRESS'] = ray_address
    if ray_dashboard_port is not None:
        os.environ['NDIF_RAY_DASHBOARD_PORT'] = str(ray_dashboard_port)


@click.command()
@click.argument('service', type=click.Choice(
    ['api', 'ray', 'broker', 'object-store', 'dashboard', 'all'],
    case_sensitive=False
), default='all')
@click.option('--worker', is_flag=True, help='Start as Ray worker node (connects to existing head)')
@click.option('--verbose', is_flag=True, help='Run in foreground with logs visible (blocking mode)')
@click.option('--timeout', type=int, default=120, help='Timeout in seconds for services to become ready (default: 120)')
@click.option('--api-url', default=None, help='API URL (default: from NDIF_API_URL)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
@click.option('--object-store-url', default=None, help='Object store URL (default: from NDIF_OBJECT_STORE_URL)')
@click.option('--ray-address', default=None, help='Ray head address for worker mode (default: from NDIF_RAY_ADDRESS)')
@click.option('--ray-dashboard-port', type=int, default=None, help='Ray dashboard port (default: from NDIF_RAY_DASHBOARD_PORT)')
def start(service: str, worker: bool, verbose: bool, timeout: int, api_url: str, broker_url: str,
          object_store_url: str, ray_address: str, ray_dashboard_port: int):
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

    # Apply CLI overrides to environment (takes precedence over env vars)
    _apply_cli_overrides(
        api_url=api_url,
        broker_url=broker_url,
        object_store_url=object_store_url,
        ray_address=ray_address,
        ray_dashboard_port=ray_dashboard_port,
    )

    # Handle worker mode
    if worker:
        _start_worker_mode(verbose)
        return

    # Fast-path: dashboard is independent of the SessionConfig / preflight
    # machinery. It's a leaf service that just exec's its start.sh.
    if service == 'dashboard':
        proc = _start_dashboard_standalone(verbose)
        if verbose and proc is not None:
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait(timeout=5)
        return

    # Check if there's already an active session
    existing_session = get_current_session()
    if existing_session:
        click.echo(f"Existing session: {existing_session.config.session_id}")
        click.echo()

    # Build config from environment (don't create session yet)
    config = SessionConfig.from_environment()

    # Determine which services need to start
    services_to_start = _determine_services_to_start(service, config, existing_session)

    if not services_to_start:
        click.echo("All requested services are already running.")
        click.echo("\nUse 'ndif info' to see session status.")
        return

    if verbose:
        click.echo(f"Services to start: {', '.join(services_to_start)}")
        click.echo()

    # Run ALL pre-flight checks before creating session
    click.echo("Running pre-flight checks...")
    all_checks = []

    if 'broker' in services_to_start:
        if verbose:
            click.echo("  Broker:")
        checks = preflight_check_broker(config.broker_port)
        all_checks.extend(checks)
        if not run_preflight_checks(checks, verbose=verbose):
            _preflight_failed()

    if 'object-store' in services_to_start:
        if verbose:
            click.echo("  Object store:")
        checks = preflight_check_object_store(config.object_store_port)
        all_checks.extend(checks)
        if not run_preflight_checks(checks, verbose=verbose):
            _preflight_failed()

    if 'ray' in services_to_start:
        if verbose:
            click.echo("  Ray:")
        checks = preflight_check_ray(
            config.ray_temp_dir,
            config.ray_head_port,
            config.ray_dashboard_port,
            config.ray_object_manager_port,
            config.ray_dashboard_grpc_port,
            config.ray_serve_port,
        )
        all_checks.extend(checks)
        if not run_preflight_checks(checks, verbose=verbose):
            _preflight_failed()

    if 'api' in services_to_start:
        if verbose:
            click.echo("  API:")
        checks = preflight_check_api(
            config.api_port,
        )
        all_checks.extend(checks)
        if not run_preflight_checks(checks, verbose=verbose):
            _preflight_failed()

    if verbose:
        click.echo("\n✓ All pre-flight checks passed")
    else:
        click.echo("  ✓ All pre-flight checks passed")
    click.echo()

    # Now create or reuse session
    if existing_session:
        session = existing_session
    else:
        session = Session.create()
        if verbose:
            click.echo(f"Session: {session.config.session_id}")
            click.echo(f"  Logs: {session.logs_dir}")
            click.echo()

    # Track what we've started for rollback
    started_services = []
    processes = []

    try:
        # Start services in order
        if 'broker' in services_to_start:
            _start_broker(session, verbose)
            started_services.append('broker')

        if 'object-store' in services_to_start:
            _start_object_store(session, verbose)
            started_services.append('object-store')

        if 'ray' in services_to_start:
            proc = _start_ray(session, verbose)
            if proc:
                processes.append(('ray', proc))
                started_services.append('ray')

        if 'api' in services_to_start:
            proc = _start_api(session, verbose)
            if proc:
                processes.append(('api', proc))
                started_services.append('api')

    except Exception as e:
        click.echo(f"\n✗ Failed to start services: {e}", err=True)
        _rollback(session, started_services, processes, existing_session is None)
        sys.exit(1)

    # Handle verbose mode (blocking) vs background mode
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
    else:
        # Wait for services to be ready
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

        # Auto-deploy from models.yaml if it exists
        if 'ray' in started_services and config_exists():
            _auto_deploy_models(session)

        click.echo("\nTo view logs:")
        for name in started_services:
            if name in ('api', 'ray'):
                click.echo(f"  ndif logs {name}")
        click.echo("\nTo view session info: ndif info")
        click.echo("To stop services: ndif stop")


def _determine_services_to_start(service: str, config: SessionConfig, existing_session) -> list[str]:
    """Determine which services need to be started."""
    services = []

    if service == 'all':
        # Check what's not already running
        if not check_redis(config.broker_url):
            services.append('broker')
        if not check_minio(config.object_store_url):
            services.append('object-store')
        if existing_session:
            if not existing_session.is_service_running('ray') or not is_port_in_use(config.ray_head_port):
                services.append('ray')
            if not existing_session.is_service_running('api') or not is_port_in_use(config.api_port):
                services.append('api')
        else:
            services.append('ray')
            services.append('api')
    elif service == 'broker':
        if not check_redis(config.broker_url):
            services.append('broker')
    elif service == 'object-store':
        if not check_minio(config.object_store_url):
            services.append('object-store')
    elif service == 'ray':
        if existing_session:
            if not existing_session.is_service_running('ray') or not is_port_in_use(config.ray_head_port):
                services.append('ray')
        else:
            services.append('ray')
    elif service == 'api':
        if existing_session:
            if not existing_session.is_service_running('api') or not is_port_in_use(config.api_port):
                services.append('api')
        else:
            services.append('api')
    elif service == 'dashboard':
        # No port-in-use guard: the dashboard is the only service that can
        # run inside its own docker container, where the host-side port
        # check would be wrong. start.sh refuses if its port is taken.
        services.append('dashboard')

    return services


def _preflight_failed():
    """Exit after pre-flight check failure."""
    click.echo("\n✗ Pre-flight checks failed. Fix the issues above and try again.", err=True)
    sys.exit(1)


def _rollback(session: Session, started_services: list[str], processes: list, delete_session: bool):
    """Roll back after a failure - stop all started services."""
    click.echo("Rolling back...")

    # Terminate processes
    for name, proc in processes:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Stop Ray cluster if it was started
    if 'ray' in started_services:
        try:
            subprocess.run(['ray', 'stop'], capture_output=True, check=False)
        except Exception:
            pass

    # Kill processes on ports for services we started
    for svc in started_services:
        port = _get_service_port(session, svc)
        if port and is_port_in_use(port):
            kill_processes_on_port(port)

    # Mark services as not running
    for svc in started_services:
        session.mark_service_running(svc, False)

    # Delete session if we created it
    if delete_session:
        try:
            current_link = get_session_root() / "current"
            if current_link.is_symlink():
                current_link.unlink()
            click.echo("Session removed due to startup failure.")
        except Exception:
            pass


def _get_service_port(session: Session, service: str) -> int:
    """Get the port for a service."""
    port_map = {
        'api': session.config.api_port,
        'ray': session.config.ray_head_port,
        'broker': session.config.broker_port,
        'object-store': session.config.object_store_port,
    }
    return port_map.get(service)


def _start_broker(session: Session, verbose: bool):
    """Start the broker (Redis) service."""
    click.echo("Starting broker (Redis)...")

    success, pid, message = util_start_redis(
        port=session.config.broker_port,
        verbose=verbose
    )

    if success:
        session.mark_service_running('broker', True)
        if pid:
            click.echo(f"  ✓ {message} (PID: {pid})")
        else:
            click.echo(f"  ✓ {message}")
        click.echo()
    else:
        raise RuntimeError(f"Failed to start broker: {message}")


def _start_object_store(session: Session, verbose: bool):
    """Start the object store (MinIO) service."""
    click.echo("Starting object store (MinIO)...")

    success, pid, message = util_start_object_store(
        port=session.config.object_store_port,
        verbose=verbose
    )

    if success:
        session.mark_service_running('object-store', True)
        if pid:
            click.echo(f"  ✓ {message} (PID: {pid})")
        else:
            click.echo(f"  ✓ {message}")
        click.echo()
    else:
        raise RuntimeError(f"Failed to start object store: {message}")


def _start_api(session: Session, verbose: bool):
    """Start the API service."""
    api_service_dir = get_service_dir("api")
    start_script = api_service_dir / "start.sh"

    if not start_script.exists():
        raise RuntimeError(f"start.sh not found at {start_script}")

    click.echo("Starting NDIF API service...")
    click.echo(f"  Port: {session.config.api_port}")
    click.echo(f"  Workers: {session.config.api_workers}")
    if verbose:
        click.echo(f"  Broker: {session.config.broker_url}")
        if session.config.object_store_url:
            click.echo(f"  Object Store: {session.config.object_store_url}")
        click.echo(f"  Ray: {session.config.ray_address}")

    log_dir = session.get_service_log_dir('api')
    log_file = log_dir / "output.log"
    if verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env = os.environ.copy()
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
    env.update(env_updates)

    if verbose:
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=api_service_dir,
            start_new_session=True
        )
    else:
        log_handle = open(log_file, 'w')
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=api_service_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    session.mark_service_running('api', True)
    return proc


def _start_dashboard_standalone(verbose: bool):
    """Run the dashboard's start.sh — no SessionConfig, no preflight.

    The dashboard is a leaf service that depends only on env vars (see
    ``services/dashboard/start.sh`` for the full list). It runs primarily
    inside docker-compose where the rest of the ``ndif start`` machinery
    (Ray ports, broker checks, model_config auto-deploy) is irrelevant.
    For the standalone non-Docker path we still want the same script to
    work, just without dragging the NDIF session in.
    """
    dashboard_service_dir = get_service_dir("dashboard")
    start_script = dashboard_service_dir / "start.sh"

    if not start_script.exists():
        raise RuntimeError(f"start.sh not found at {start_script}")

    click.echo("Starting NDIF dashboard service...")
    if verbose:
        click.echo(f"  Port: {os.environ.get('NDIF_DASHBOARD_PORT', '8081')}")
        click.echo(f"  Data dir: {os.environ.get('NDIF_DASHBOARD_DATA_DIR', '~/ndif_dashboard')}")
    click.echo()

    proc = subprocess.Popen(
        ['bash', str(start_script)],
        cwd=dashboard_service_dir,
        start_new_session=True,
    )
    return proc


def _start_ray(session: Session, verbose: bool):
    """Start the Ray service."""
    ray_service_dir = get_service_dir("ray")
    start_script = ray_service_dir / "start.sh"

    if not start_script.exists():
        raise RuntimeError(f"start.sh not found at {start_script}")

    click.echo("Starting NDIF Ray service...")
    if verbose:
        click.echo(f"  API: {session.config.api_url}")
        click.echo(f"  Temp Dir: {session.config.ray_temp_dir}")
    click.echo(f"  Head Port: {session.config.ray_head_port}")
    click.echo(f"  Dashboard: {session.config.ray_dashboard_port}")

    log_dir = session.get_service_log_dir('ray')
    log_file = log_dir / "output.log"
    if verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env = os.environ.copy()
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
    env.update(env_updates)
    if verbose:
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=ray_service_dir,
            start_new_session=True
        )
    else:
        log_handle = open(log_file, 'w')
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=ray_service_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    session.mark_service_running('ray', True)
    return proc


def _wait_for_controller(ray_address: str, timeout: int = 120) -> bool:
    """Wait for the controller actor to be available.

    Args:
        ray_address: Ray address to connect to
        timeout: Maximum seconds to wait

    Returns:
        True if controller is available, False if timeout
    """
    import ray
    import time

    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            ray.get_actor("Controller", namespace="NDIF")
            return True
        except ValueError:
            time.sleep(2)

    return False


def _auto_deploy_models(session: Session):
    """Auto-deploy models from ~/.ndif/models.yaml if it exists."""
    from .deploy import deploy as deploy_cmd
    from click.testing import CliRunner

    config_path = get_default_config_path()
    click.echo()
    click.echo(f"Auto-deploying models from {config_path}...")

    # Wait for controller to be available
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

    # Echo the output (excluding the first line which repeats the file path)
    if result.output:
        lines = result.output.strip().split('\n')
        # Skip the "Loaded N model(s)" line since we already said we're auto-deploying
        for line in lines[1:]:
            click.echo(line)

    if result.exit_code != 0:
        click.echo("Warning: Auto-deploy encountered errors", err=True)


def _start_worker_mode(verbose: bool):
    """Handle starting as a Ray worker node."""

    # Check for existing session
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

    # Build config to get ray_address and temp_dir
    config = SessionConfig.from_environment(node_type="worker")

    click.echo("Starting Ray worker node...")
    click.echo(f"  Connecting to: {config.ray_address}")
    click.echo(f"  Temp dir: {config.ray_temp_dir}")
    click.echo()

    # Run pre-flight checks
    click.echo("Running pre-flight checks...")
    checks = preflight_check_worker(config.ray_temp_dir, config.ray_address)
    if not run_preflight_checks(checks):
        _preflight_failed()

    click.echo("\n✓ All pre-flight checks passed")
    click.echo()

    # Create worker session
    session = Session.create(node_type="worker")
    click.echo(f"Session: {session.config.session_id}")
    click.echo(f"  Logs: {session.logs_dir}")
    click.echo()

    # Start the worker
    try:
        proc = _start_ray_worker(session, verbose)
    except Exception as e:
        click.echo(f"\n✗ Failed to start worker: {e}", err=True)
        # Clean up session
        try:
            current_link = get_session_root() / "current"
            if current_link.is_symlink():
                current_link.unlink()
        except Exception:
            pass
        sys.exit(1)

    # Handle verbose mode
    if verbose and proc:
        click.echo("\n✓ Worker started in verbose mode. Press Ctrl+C to stop.")
        click.echo("=" * 60)
        try:
            proc.wait()
        except KeyboardInterrupt:
            click.echo("\n\nStopping worker...")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            subprocess.run(['ray', 'stop'], capture_output=True, check=False)
            session.mark_service_running('ray-worker', False)
            try:
                current_link = get_session_root() / "current"
                if current_link.is_symlink():
                    current_link.unlink()
            except Exception:
                pass
            sys.exit(0)
    else:
        click.echo("\n✓ Worker started successfully.")
        click.echo("\nTo view logs:")
        click.echo("  ndif logs ray")
        click.echo("\nTo stop: ndif stop")


def _start_ray_worker(session: Session, verbose: bool):
    """Start the Ray worker service."""
    ray_service_dir = get_service_dir("ray")
    start_script = ray_service_dir / "start-worker.sh"

    if not start_script.exists():
        raise RuntimeError(f"start-worker.sh not found at {start_script}")

    log_dir = session.get_service_log_dir('ray')
    log_file = log_dir / "output.log"
    if not verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env = os.environ.copy()
    env.update({
        'NDIF_RAY_TEMP_DIR': session.config.ray_temp_dir,
        'NDIF_RAY_ADDRESS': session.config.ray_address,
    })

    if verbose:
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=ray_service_dir,
            start_new_session=True
        )
    else:
        log_handle = open(log_file, 'w')
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=ray_service_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    session.mark_service_running('ray-worker', True)
    return proc
