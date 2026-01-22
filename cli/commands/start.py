"""Start command for NDIF services"""

import os
import subprocess
import sys
from pathlib import Path

import click

from .util import get_repo_root, print_logo
from .session import (
    Session,
    SessionConfig,
    get_current_session,
    get_session_root,
    is_port_in_use,
    kill_processes_on_port,
)
from .checks import (
    check_redis,
    check_minio,
    preflight_check_api,
    preflight_check_ray,
    preflight_check_broker,
    preflight_check_object_store,
    run_preflight_checks,
)
from .deps import start_redis as util_start_redis, start_object_store as util_start_object_store


@click.command()
@click.argument('service', type=click.Choice(
    ['api', 'ray', 'broker', 'object-store', 'all'],
    case_sensitive=False
), default='all')
@click.option('--verbose', is_flag=True, help='Run in foreground with logs visible (blocking mode)')
def start(service: str, verbose: bool):
    """Start NDIF services.

    SERVICE: Which service to start (api, ray, broker, object-store, or all). Default: all

    All pre-flight checks run before any services start. If any check fails,
    nothing is started. If a service fails to start, all started services are
    stopped and the session is removed.

    Examples:
        ndif start                              # Start all services
        ndif start api                          # Start API only
        ndif start broker                       # Start broker (Redis) only
        ndif start --verbose                    # Start with logs visible

    Key Environment Variables:
        NDIF_BROKER_URL          - Broker URL (default: redis://localhost:6379/)
        NDIF_OBJECT_STORE_URL    - Object store URL (default: http://localhost:27017)
        NDIF_API_PORT            - API port (default: 8001)
        NDIF_RAY_TEMP_DIR        - Ray temp directory (default: /tmp/ray)
        NDIF_SESSION_ROOT        - Session directory (default: ~/.ndif)
    """
    print_logo()

    repo_root = get_repo_root()

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
            config.api_host,
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
    click.echo(f"  Host: {session.config.api_host}:{session.config.api_port}")
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
        'OBJECT_STORE_URL': session.config.object_store_url,
        'BROKER_URL': session.config.broker_url,
        'WORKERS': str(session.config.api_workers),
        'RAY_ADDRESS': session.config.ray_address,
        'API_INTERNAL_PORT': str(session.config.api_port),
        'API_URL': session.config.api_url,
        'DEV_MODE': 'true',
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
    start_script = ray_service_dir / "start-cli.sh"

    if not start_script.exists():
        raise RuntimeError(f"start-cli.sh not found at {start_script}")

    click.echo("Starting NDIF Ray service...")
    click.echo(f"  Object Store: {session.config.object_store_url}")
    click.echo(f"  API: {session.config.api_url}")
    click.echo(f"  Temp Dir: {session.config.ray_temp_dir}")
    click.echo(f"  Head Port: {session.config.ray_head_port}")
    click.echo(f"  Dashboard: {session.config.ray_dashboard_host}:{session.config.ray_dashboard_port}")

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
        'NDIF_RAY_DASHBOARD_HOST': session.config.ray_dashboard_host,
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
