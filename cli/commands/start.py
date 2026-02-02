"""Start command for NDIF services"""

import os
import subprocess
import sys
from pathlib import Path

import click

from ..lib.util import get_repo_root, print_logo
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
)
from ..lib.deps import start_redis as util_start_redis, start_object_store as util_start_object_store


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
    ['api', 'ray', 'broker', 'object-store', 'all'],
    case_sensitive=False
), default='all')
@click.option('--worker', is_flag=True, help='Start as Ray worker node (connects to existing head)')
@click.option('--verbose', is_flag=True, help='Run in foreground with logs visible (blocking mode)')
@click.option('--api-url', default=None, help='API URL (default: from NDIF_API_URL)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
@click.option('--object-store-url', default=None, help='Object store URL (default: from NDIF_OBJECT_STORE_URL)')
@click.option('--ray-address', default=None, help='Ray head address for worker mode (default: from NDIF_RAY_ADDRESS)')
@click.option('--ray-dashboard-port', type=int, default=None, help='Ray dashboard port (default: from NDIF_RAY_DASHBOARD_PORT)')
def start(service: str, worker: bool, verbose: bool, api_url: str, broker_url: str,
          object_store_url: str, ray_address: str, ray_dashboard_port: int):
    """Start NDIF services.

    SERVICE: Which service to start (api, ray, broker, object-store, or all). Default: all

    All pre-flight checks run before any services start. If any check fails,
    nothing is started. If a service fails to start, all started services are
    stopped and the session is removed.

    Examples:
        ndif start                              # Start all services (head node)
        ndif start api                          # Start API only
        ndif start broker                       # Start broker (Redis) only
        ndif start --verbose                    # Start with logs visible
        ndif start --worker                     # Start as Ray worker node
        ndif start --api-url http://localhost:8080  # Start with custom API URL
        ndif start --broker-url redis://host:6379   # Use custom broker

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

    repo_root = get_repo_root()

    # Handle worker mode
    if worker:
        _start_worker_mode(repo_root, verbose)
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

    click.echo(f"Services to start: {', '.join(services_to_start)}")
    click.echo()

    # Run ALL pre-flight checks before creating session
    click.echo("Running pre-flight checks...")
    all_checks = []

    if 'broker' in services_to_start:
        click.echo("  Broker:")
        checks = preflight_check_broker(config.broker_port)
        all_checks.extend(checks)
        if not run_preflight_checks(checks):
            _preflight_failed()

    if 'object-store' in services_to_start:
        click.echo("  Object store:")
        checks = preflight_check_object_store(config.object_store_port)
        all_checks.extend(checks)
        if not run_preflight_checks(checks):
            _preflight_failed()

    if 'ray' in services_to_start:
        click.echo("  Ray:")
        checks = preflight_check_ray(
            config.ray_temp_dir,
            config.object_store_url,
            config.ray_head_port,
            config.ray_dashboard_port,
            config.ray_object_manager_port,
            config.ray_dashboard_grpc_port,
            config.ray_serve_port,
            skip_object_store_check='object-store' in services_to_start,
        )
        all_checks.extend(checks)
        if not run_preflight_checks(checks):
            _preflight_failed()

    if 'api' in services_to_start:
        click.echo("  API:")
        checks = preflight_check_api(
            config.api_port,
            config.broker_url,
            config.object_store_url,
            skip_broker_check='broker' in services_to_start,
            skip_object_store_check='object-store' in services_to_start,
        )
        all_checks.extend(checks)
        if not run_preflight_checks(checks):
            _preflight_failed()

    click.echo("\n✓ All pre-flight checks passed")
    click.echo()

    # Now create or reuse session
    if existing_session:
        session = existing_session
    else:
        session = Session.create()
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
            proc = _start_ray(session, repo_root, verbose)
            if proc:
                processes.append(('ray', proc))
                started_services.append('ray')

        if 'api' in services_to_start:
            proc = _start_api(session, repo_root, verbose)
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
        click.echo("\n✓ Services started successfully.")
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
    else:
        raise RuntimeError(f"Failed to start broker: {message}")

    click.echo()


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
    else:
        raise RuntimeError(f"Failed to start object store: {message}")

    click.echo()


def _start_api(session: Session, repo_root: Path, verbose: bool):
    """Start the API service."""
    api_service_dir = repo_root / "src" / "services" / "api"
    start_script = api_service_dir / "start.sh"

    if not start_script.exists():
        raise RuntimeError(f"start.sh not found at {start_script}")

    click.echo("Starting NDIF API service...")
    click.echo(f"  Port: {session.config.api_port}")
    click.echo(f"  Workers: {session.config.api_workers}")
    click.echo(f"  Broker: {session.config.broker_url}")
    click.echo(f"  Object Store: {session.config.object_store_url}")
    click.echo(f"  Ray: {session.config.ray_address}")

    log_dir = session.get_service_log_dir('api')
    log_file = log_dir / "output.log"
    if not verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env = os.environ.copy()
    env.update({
        'NDIF_OBJECT_STORE_URL': session.config.object_store_url,
        'NDIF_BROKER_URL': session.config.broker_url,
        'NDIF_API_WORKERS': str(session.config.api_workers),
        'NDIF_RAY_ADDRESS': session.config.ray_address,
        'NDIF_API_PORT': str(session.config.api_port),
        'NDIF_API_URL': session.config.api_url,
        'NDIF_DEV_MODE': 'true',
    })

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


def _start_ray(session: Session, repo_root: Path, verbose: bool):
    """Start the Ray service."""
    ray_service_dir = repo_root / "src" / "services" / "ray"
    start_script = ray_service_dir / "start.sh"

    if not start_script.exists():
        raise RuntimeError(f"start.sh not found at {start_script}")

    click.echo("Starting NDIF Ray service...")
    click.echo(f"  Object Store: {session.config.object_store_url}")
    click.echo(f"  API: {session.config.api_url}")
    click.echo(f"  Temp Dir: {session.config.ray_temp_dir}")
    click.echo(f"  Head Port: {session.config.ray_head_port}")
    click.echo(f"  Dashboard: {session.config.ray_dashboard_port}")

    log_dir = session.get_service_log_dir('ray')
    log_file = log_dir / "output.log"
    if not verbose:
        click.echo(f"  Logs: {log_file}")
    click.echo()

    env = os.environ.copy()
    env.update({
        'OBJECT_STORE_URL': session.config.object_store_url,
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

    session.mark_service_running('ray', True)
    return proc


def _start_worker_mode(repo_root: Path, verbose: bool):
    """Handle starting as a Ray worker node."""
    from ..lib.checks import preflight_check_worker

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
        proc = _start_ray_worker(session, repo_root, verbose)
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


def _start_ray_worker(session: Session, repo_root: Path, verbose: bool):
    """Start the Ray worker service."""
    ray_service_dir = repo_root / "src" / "services" / "ray"
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
