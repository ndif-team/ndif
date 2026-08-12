"""Rank 0's side of the shard group: spawn the other ranks and drive them.

The model actor process *is* rank 0 — it holds a shard of the weights and runs
the user's block itself — so this owns only the other N-1 ranks: launching them,
handing each request over, collecting what they report, and killing them.

Tensors never cross these sockets. NCCL carries the model's own traffic; this
channel exists to start a request, learn that a shard failed, and stop the group.

A request goes over it in two phases:

    PREPARE  ->  the shards deserialize the block and answer READY or ERROR
    GO       ->  everyone runs it, and the shards answer DONE or ERROR

Bringing the group up is split the same way, for a related reason: a shard says
HELLO as soon as it connects and READY only once its weights are loaded, because
loading is a rendezvous every rank has to reach — including rank 0, which cannot
be sitting here waiting for the shards while it does.

The split is what keeps a bad payload from hanging the group. Deserialization is
where a request most often dies (version drift between the client's block and the
server's tree), and it is the last point before any collective — so the shards
prove they can build the block *before* rank 0 commits to a forward pass. Once GO
is sent, a shard that dies leaves rank 0 blocked in NCCL, and only the execution
timeout and a restart get it back.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .common import DEFAULT_TP_SIZE, Channel, rank_env

logger = logging.getLogger("ndif.modeling")

# How long to wait for a shard process to start and connect. Short: it has done
# nothing but import and open a socket.
CONNECT_TIMEOUT_SECONDS = 300.0
# How long to wait for a shard to load its weights, measured from when rank 0
# finished its own. Generous: it is a cold read of a checkpoint from local disk.
START_TIMEOUT_SECONDS = 1800.0
# How long to wait for every shard to deserialize a block. Short — no model work
# happens here, and a hang means something is wrong.
PREPARE_TIMEOUT_SECONDS = 300.0
# How long to wait for the group to confirm it stood down. Very short: a released
# shard does nothing but loop, so this is a message in flight and nothing else.
RELEASE_TIMEOUT_SECONDS = 30.0


class ShardError(Exception):
    """A shard reported a failure, already formatted for the caller."""


def _free_port() -> int:
    """A port for the group's rendezvous, chosen by the OS."""
    with socket.socket() as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


def _package_root() -> str:
    """The directory holding the top-level ``ndif`` package.

    A shard runs as ``python -m ndif.services.ray.tp.shard``, so it needs this on
    ``PYTHONPATH`` to import ndif's helpers rather than duplicate them. tp -> ray
    -> services -> ndif -> <root>.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


class Shard(Channel):
    """One child rank: its process, and the channel rank 0 talks to it on."""

    def __init__(self, rank: int, process: subprocess.Popen, sock: socket.socket) -> None:
        super().__init__(sock)
        self.rank = rank
        self.process = process

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        self.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class ShardGroup:
    """The N-1 shard processes rank 0 runs alongside itself.

    Deliberately free of Ray and of the actor's request types, so the whole
    process-management path can be exercised on its own.
    """

    def __init__(
        self,
        model_key: str,
        gpu_ids: List[int],
        dtype: str,
        tp_size: int = DEFAULT_TP_SIZE,
        load_kwargs: Optional[Dict[str, Any]] = None,
        gpu_mem_bytes_by_id: Optional[Dict[int, int]] = None,
    ) -> None:
        self.model_key = model_key
        self.gpu_ids = sorted(gpu_ids)
        self.gpu_mem_bytes_by_id = gpu_mem_bytes_by_id or {}
        self.dtype = dtype
        self.tp_size = tp_size
        self.load_kwargs = load_kwargs or {}
        self.master_port = _free_port()
        self.shards: List[Shard] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = CONNECT_TIMEOUT_SECONDS) -> None:
        """Launch ranks 1..N-1 and wait only until each has *connected*.

        Deliberately not "until each has loaded". Loading is where transformers
        forms the process group, and it blocks until every rank arrives — rank 0
        included. Waiting here for the shards to finish loading would mean each
        side waiting for the other, so this returns as soon as the shards are up
        and identified. The caller loads its own shard next, which is what
        releases them, and then calls :meth:`wait_ready`.
        """
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = f"/tmp/ndif-tp-{os.getpid()}-{self.master_port}.sock"
        self._unlink(path)
        listener.bind(path)
        listener.listen(self.tp_size)
        listener.settimeout(timeout)

        started = time.time()
        try:
            processes = [
                self._spawn(rank, path) for rank in range(1, self.tp_size)
            ]
            for process in processes:
                connection, _ = listener.accept()
                shard = Shard(rank=-1, process=process, sock=connection)
                # Each shard names itself the moment it connects — before it
                # loads anything — so a slow starter isn't matched to the wrong
                # process, and so this wait stays short.
                message = shard.recv(timeout=timeout)
                if not (isinstance(message, tuple) and message[0] == "HELLO"):
                    raise ShardError(f"shard did not come up: {message!r}")
                shard.rank = message[1]
                self.shards.append(shard)
        except Exception:
            self.stop()
            raise
        finally:
            listener.close()
            self._unlink(path)

        self.shards.sort(key=lambda shard: shard.rank)
        logger.info(
            f"TP shards connected: ranks {[s.rank for s in self.shards]} "
            f"on GPUs {self.gpu_ids}, in {time.time() - started:.1f}s"
        )

    def wait_ready(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        """Block until every shard has its weights loaded.

        Call *after* loading rank 0's own shard: the ranks load together, so this
        only reports what has already happened by the time rank 0 is done.
        """
        started = time.time()
        for shard in self.shards:
            try:
                message = shard.recv(timeout=timeout)
            except Exception as exception:
                self.stop()
                raise ShardError(f"rank {shard.rank} never loaded: {exception}")
            if not (isinstance(message, tuple) and message[0] == "READY"):
                self.stop()
                raise ShardError(
                    f"rank {shard.rank} failed to load: {self._describe(message)}"
                )
        logger.info(
            f"TP group up: {self.tp_size} ranks, weights loaded in "
            f"{time.time() - started:.1f}s after rank 0"
        )

    def _spawn(self, rank: int, path: str) -> subprocess.Popen:
        # Each rank caps only its own card; the rest belong to other processes.
        ordered = sorted(self.gpu_mem_bytes_by_id)
        budget = self.gpu_mem_bytes_by_id[ordered[rank]] if rank < len(ordered) else 0
        env = {
            **os.environ,
            **rank_env(self.gpu_ids, rank, self.master_port),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [_package_root(), os.environ.get("PYTHONPATH")])
            ),
        }
        return subprocess.Popen(
            [
                sys.executable, "-m", "ndif.services.ray.tp.shard",
                "--socket", path,
                "--rank", str(rank),
                "--model-key", self.model_key,
                "--dtype", self.dtype,
                "--tp-size", str(self.tp_size),
                "--load-kwargs", repr(self.load_kwargs),
                "--mem-bytes", str(budget),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
        )

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def stop(self) -> None:
        for shard in self.shards:
            shard.stop()
        self.shards.clear()

    @property
    def healthy(self) -> bool:
        return bool(self.shards) and all(shard.alive for shard in self.shards)

    # -- one request -------------------------------------------------------

    def prepare(
        self,
        payload: bytes,
        compress: bool,
        env: Dict[str, Any],
        seed: int,
        timeout: float = PREPARE_TIMEOUT_SECONDS,
    ) -> None:
        """Have every shard build the block, before rank 0 commits to a forward.

        Raises:
            ShardError: if any shard could not prepare — with its message, since
                the same payload is about to fail on rank 0 for the same reason.
        """
        for shard in self.shards:
            shard.send(("PREPARE", payload, compress, env, seed))

        failures = []
        for shard in self.shards:
            try:
                message = shard.recv(timeout=timeout)
            except Exception as exception:
                failures.append(f"rank {shard.rank}: {exception}")
                continue
            if isinstance(message, tuple) and message[0] == "READY":
                continue
            failures.append(f"rank {shard.rank}: {self._describe(message)}")

        if failures:
            raise ShardError("; ".join(failures))

    def sandbox(
        self,
        path: str,
        env: Dict[str, Any],
        seed: int,
        timeout: float = PREPARE_TIMEOUT_SECONDS,
    ) -> None:
        """Point every shard at the runner holding this request's block.

        The untrusted counterpart to :meth:`prepare`, and the same shape: the
        shards answer READY once they can reach the runner, which is the last
        point before any collective, so a shard that cannot get there is still a
        failure rank 0 can report without leaving anyone blocked.

        What is *not* the same: no shard builds the block. One runner holds it for
        the whole group. Rank 0 must already be connected to that runner before
        calling this — the runner treats its first connection as the one that
        sends the payload.

        Raises:
            ShardError: if any shard could not reach the runner.
        """
        for shard in self.shards:
            shard.send(("SANDBOX", path, env, seed))

        received, lost = self._drain(time.time() + timeout)
        failures = list(lost)
        failures.extend(
            f"rank {rank}: {self._describe(message)}"
            for rank, message in sorted(received.items())
            if not (isinstance(message, tuple) and message[0] == "READY")
        )
        if failures:
            raise ShardError("; ".join(failures))

    def go(self) -> None:
        """Release the shards into the forward. Rank 0 must run it too."""
        for shard in self.shards:
            shard.send(("GO",))

    def release(self, timeout: float = RELEASE_TIMEOUT_SECONDS) -> List[str]:
        """Send prepared shards back to idle without running anything.

        For when rank 0 cannot go ahead after the shards have already prepared —
        it failed to build the same block, or the request died between the two.
        Without this they sit waiting for a ``GO`` until their idle timeout, and
        the replica looks alive while being unable to serve anything.

        Waits for each shard to *acknowledge* being back at idle, and returns the
        ranks that didn't. A stand-down has to be observable or it is worth
        nothing: the caller's next question is "can this group serve the next
        request", and a shard that was sent ``SKIP`` and said nothing is not an
        answer to it. Sent to every shard before waiting on any, so a slow one
        doesn't serialize the rest.
        """
        for shard in self.shards:
            try:
                shard.send(("SKIP",))
            except OSError:
                logger.warning(f"could not release rank {shard.rank}")

        received, lost = self._drain(time.time() + timeout)
        lost.extend(
            f"rank {rank}: {self._describe(message)}"
            for rank, message in sorted(received.items())
            if not (isinstance(message, tuple) and message[0] == "IDLE")
        )
        return lost

    # How long a shard gets to finish a frame it has already started. Small and
    # fixed: `select` said the socket was readable, so the header is there and the
    # body is a memcpy behind it. Never zero — `socket.settimeout(0.0)` is
    # *non-blocking mode*, not "no wait", so a body not yet in the buffer would
    # raise BlockingIOError and the shard would be called lost.
    FRAME_TIMEOUT_SECONDS = 5.0

    def _drain(self, deadline: float) -> "Tuple[Dict[int, Any], List[str]]":
        """One message from every shard, or as many as arrive before ``deadline``.

        Waits on all the sockets at once rather than each in turn. Serializing
        them with a shrinking per-shard timeout looks equivalent and is not: the
        ranks leave a collective together, so their replies land together, and a
        budget spent on the first shard left the rest with a timeout of zero —
        which is non-blocking mode, so they raised immediately and were reported
        *lost*. That reads as a wedged group and restarts the replica, which is
        the opposite of what a deadline was added to do.

        Returns ``({rank: message}, [descriptions of the ones that never
        answered])``.
        """
        waiting = {shard.sock.fileno(): shard for shard in self.shards}
        received: Dict[int, Any] = {}
        lost: List[str] = []

        while waiting:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            ready, _, _ = select.select(list(waiting), [], [], remaining)
            if not ready:
                break
            for fileno in ready:
                shard = waiting.pop(fileno)
                try:
                    received[shard.rank] = shard.recv(
                        timeout=self.FRAME_TIMEOUT_SECONDS
                    )
                except Exception as exception:
                    lost.append(f"rank {shard.rank}: {exception}")

        lost.extend(
            f"rank {shard.rank}: no answer within the deadline"
            for shard in waiting.values()
        )
        return received, lost

    def collect(self, timeout: float) -> Tuple[List[str], List[str]]:
        """Wait for every shard to finish; return ``(raised, lost)``.

        Rank 0 has already finished its own run by the time this is called — the
        ranks leave the last collective together, so a shard that has not
        answered shortly after is one that died rather than one still working.

        The two lists mean very different things and the caller must not merge
        them:

        * **raised** — the shard reported an exception and went back to its idle
          loop. It is alive and reusable. This is the *normal* outcome when the
          user's block raises: every rank runs that block, so every rank raises.
        * **lost** — the shard never answered. Whatever state it is in, it is not
          one this group can start another request from.
        """
        # One deadline for the whole group, not `timeout` each. The ranks leave
        # the last collective together, so they answer together; charging the
        # full timeout per shard would make the real bound `timeout * (tp - 1)`
        # -- 210s at degree 8 against a 30s constant that was chosen precisely
        # because this runs on the actor's event loop.
        received, lost = self._drain(time.time() + timeout)

        raised = [
            f"rank {rank}: {self._describe(message)}"
            for rank, message in sorted(received.items())
            if not (isinstance(message, tuple) and message[0] == "DONE")
        ]
        return raised, lost

    @staticmethod
    def _describe(message: Any) -> str:
        if isinstance(message, tuple) and len(message) > 1:
            return str(message[1])
        return repr(message)
