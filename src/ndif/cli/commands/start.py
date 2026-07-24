"""``ndif start`` — bring NDIF services up.

Detached by default (each service in its own session/process group, tracked by
a PID file), or in the foreground with ``--foreground`` for containers and
process supervisors. Which services: the args, else ``$NDIF_SERVICE``, else all
of them (redis, minio, ray, api).
"""

import os
import signal
import subprocess
import sys
import time

import click

from .. import config
from ..service import SERVICE_MAP, SERVICES, Service, env_services, resolve_targets
from ..state import State
from ..util import print_logo, terminate_pid


def _proc_env(svc: Service, env: dict) -> dict:
    """Shared environment plus whatever extra env this service needs."""
    return {**env, **svc.build_env(env)}


def _spawn(svc: Service, env: dict, state: State) -> None:
    """Launch ``svc`` detached, logging to its file and recording its PID."""
    command = svc.build_command(env)
    log_path = state.log_file(svc.name)
    log = open(log_path, "ab")
    try:
        # start_new_session detaches the child into its own session/process
        # group so it survives this CLI exiting and can be signalled as a unit.
        proc = subprocess.Popen(
            command,
            env=_proc_env(svc, env),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise click.ClickException(f"{svc.name}: cannot run {command[0]!r}: {e}")
    finally:
        log.close()
    state.write_pid(svc.name, proc.pid)
    click.echo(f"  ✓ {svc.name}: started (pid {proc.pid}) → {log_path}")


def _run_foreground(targets: list[Service], env: dict) -> None:
    """Run services in the foreground (for containers / process supervisors).

    A single service replaces this process via ``exec`` so it becomes PID 1 and
    receives signals directly. Multiple services run as children with signal
    forwarding; the first to exit brings the rest down and sets the exit code.
    """
    if len(targets) == 1:
        svc = targets[0]
        command = svc.build_command(env)
        click.echo(f"  ▸ {svc.name}: running in foreground")
        try:
            os.execvpe(command[0], command, _proc_env(svc, env))
        except FileNotFoundError as e:
            raise click.ClickException(f"{svc.name}: cannot run {command[0]!r}: {e}")

    procs: list[tuple[Service, subprocess.Popen]] = []

    def _forward(signum, _frame):
        for _, proc in procs:
            proc.send_signal(signum)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    for svc in targets:
        command = svc.build_command(env)
        try:
            proc = subprocess.Popen(command, env=_proc_env(svc, env))
        except FileNotFoundError as e:
            raise click.ClickException(f"{svc.name}: cannot run {command[0]!r}: {e}")
        procs.append((svc, proc))
        click.echo(f"  ▸ {svc.name}: started (pid {proc.pid})")

    # Wait for the first service to exit, then tear the rest down.
    while all(proc.poll() is None for _, proc in procs):
        time.sleep(0.5)
    exit_code = 0
    for svc, proc in procs:
        rc = proc.poll()
        if rc is not None:
            click.echo(f"  ▪ {svc.name}: exited ({rc})")
            exit_code = rc or exit_code
    for _, proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for _, proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    sys.exit(exit_code)


@click.command()
@click.argument("services", nargs=-1)
@click.option("-e", "--env", "env_pairs", multiple=True, metavar="KEY=VALUE",
              help="Env var injected into started services (repeatable).")
@click.option("--redis-url", help="Sets NDIF_REDIS_URL.")
@click.option("--ray-address", help="Sets NDIF_RAY_ADDRESS.")
@click.option("--ray-head-address", help="Sets NDIF_RAY_HEAD_ADDRESS — join this head's "
              "HOST:PORT as a worker (default target becomes ray only).")
@click.option("--api-port", type=int, help="Sets NDIF_API_PORT.")
@click.option("--restart", is_flag=True,
              help="Restart services that are already running instead of skipping them.")
@click.option("-f", "--foreground", is_flag=True,
              help="Run in the foreground (for containers/supervisors) instead of detaching.")
def start(services, env_pairs, redis_url, ray_address, ray_head_address, api_port,
          restart, foreground):
    """Start NDIF services (default: all — redis, minio, ray, api).

    Detached by default; --foreground runs them attached for a container or
    supervisor. Deploy models separately with `ndif deploy` once services are
    up. CLI arguments take precedence over environment variables.

    Worker node: set NDIF_RAY_HEAD_ADDRESS (or --ray-head-address HOST:PORT) and
    `ndif start` brings up just Ray, joined to that head as a worker.
    """
    env = config.build_env(env_pairs, {"redis_url": redis_url, "ray_address": ray_address,
                                       "ray_head_address": ray_head_address,
                                       "api_port": api_port})
    # A worker node runs only Ray (joined to the head); a head runs everything.
    default = [SERVICE_MAP["ray"]] if env.get("NDIF_RAY_HEAD_ADDRESS") else list(SERVICES)
    targets = resolve_targets(services or env_services(), default=default)
    print_logo()

    if foreground:
        _run_foreground(targets, env)
        return

    state = State.from_env()
    state.ensure()

    for svc in targets:
        pid = state.running_pid(svc.name)
        if pid is not None:
            if restart:
                click.echo(f"  ↻ {svc.name}: restarting (was pid {pid})")
                terminate_pid(pid)
                state.clear_pid(svc.name)
            else:
                click.echo(f"  • {svc.name}: already running (pid {pid}), skipping")
                continue
        _spawn(svc, env, state)
