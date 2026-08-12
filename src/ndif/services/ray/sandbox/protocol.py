"""Shared wire protocol for the sandbox: framing, a type-tagged codec, and the
message catalog both ends speak.

Imported by both ends of the socket (the host and the runner process). Keep it
dependency-light: standard library plus cloudpickle only. The framing and codec
are transport-agnostic — they assume only an ordered byte stream — while the
message set below is specific to the sandbox's interleaving flow.

Framing: every message is length-prefixed (``send_frame`` / ``recv_frame``). The
codec is type-tagged (``encode`` / ``decode``): ``str`` and ``bytes`` pass through
raw (a 1-byte tag), everything else cloudpickles — so a ``torch.save`` blob or a
tensor rides without a second pickle pass.

The two directions are not symmetric:

* runner -> host uses ``pack`` / ``unpack`` — a tuple of values plus kwargs, read
  on the host as ``(values, kwargs)``.
* host -> runner uses a single ``encode``d value, read back with ``decode``.
  Multi-field messages are sent as one tuple (e.g. ``("HOOK", provider, value)``).

The interleaver is split at the mediator level: the workers (greenlets) run in
the runner, one host proxy per worker owns the parent side. A *park* is a worker
waiting on a location, ``(event, location, pin, *rest)`` — ``event`` is VALUE /
SWAP / SKIP, ``rest`` carries the swap value for SWAP/SKIP, ``pin`` is the worker's
``tracer.iter`` pointer (or None); ``None`` instead of a park means the worker
finished. The host tags/matches the occurrence, so parks cross untagged.

Message catalog (the event name is the first element):

host -> runner
  (blob, compress)            the request payload; first message, starts nns.run
  ("RESUME", id, args, pin)   switch worker ``id`` in with ``args`` (a read's value,
                              or empty for a swap); ``pin`` pushes the host's pin so
                              tracer.iter relaxation stays in step. Reply: PARK
  ("THROW", id, requester, is_iter)
                              worker ``id`` is still parked on ``requester`` the
                              model never reached; throw OutOfOrderError into it
                              (warn instead when ``is_iter``). No reply
  ("DONE", result)            the forward pass returned ``result``; ends the pump

runner -> host
  ("INTERLEAVE", fn, parks, *args) + kwargs
                              run wrapper method ``fn`` on the model with these
                              inputs; ``parks`` is one initial park per worker
  ("PARK", id, park)          worker ``id``'s next park (or None if it finished),
                              in reply to RESUME
  ("STOP",)                   a worker halted the run (tracer.stop()); unwind the
                              model (its interleaver swallows the early stop)
  ("PRINT", text)             forwarded stdout from the traced block
  ("END", blob, deserialize_ms)
                              finished; ``blob`` is torch.save of the block's
                              nnsight.save()-marked values, for the host to upload;
                              ``deserialize_ms`` is the runner's deserialize time
  ("EXCEPTION", error)        the block raised ``error``
"""

import select
import struct
import time

import cloudpickle


# -- length-prefixed framing -------------------------------------------------

def send_frame(sock, data: bytes) -> None:
    sock.sendall(struct.pack(">Q", len(data)) + data)


def recv_frame(sock) -> bytes:
    size = struct.unpack(">Q", recvn(sock, 8))[0]
    return recvn(sock, size)


def recvn(sock, n: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < n:
        chunk = sock.recv(n - len(buffer))
        if not chunk:
            raise ConnectionError("connection closed before message complete")
        buffer.extend(chunk)
    return bytes(buffer)


# -- type-tagged codec: str/bytes pass through, everything else cloudpickles --

def encode(value) -> bytes:
    if isinstance(value, bytes):
        return b"\x01" + value
    if isinstance(value, str):
        return b"\x02" + value.encode()
    return b"\x00" + cloudpickle.dumps(value)


def decode(blob: bytes):
    tag, payload = blob[:1], blob[1:]
    if tag == b"\x01":
        return payload
    if tag == b"\x02":
        return payload.decode()
    return cloudpickle.loads(payload)


def pack(values, kwargs=None) -> bytes:
    """Encode positional values then keyword values, each tag-encoded."""
    kwargs = kwargs or {}
    out = struct.pack(">I", len(values))
    for value in values:
        item = encode(value)
        out += struct.pack(">I", len(item)) + item
    out += struct.pack(">I", len(kwargs))
    for key, value in kwargs.items():
        key_bytes = key.encode()
        out += struct.pack(">I", len(key_bytes)) + key_bytes
        item = encode(value)
        out += struct.pack(">I", len(item)) + item
    return out


def unpack(blob: bytes):
    """Inverse of pack: returns ``(values, kwargs)``."""
    offset = 0

    def take():
        nonlocal offset
        (n,) = struct.unpack(">I", blob[offset : offset + 4])
        offset += 4
        chunk = blob[offset : offset + n]
        offset += n
        return chunk

    (count,) = struct.unpack(">I", blob[offset : offset + 4])
    offset += 4
    values = [decode(take()) for _ in range(count)]

    kwargs = {}
    if offset < len(blob):
        (kcount,) = struct.unpack(">I", blob[offset : offset + 4])
        offset += 4
        kwargs = {take().decode(): decode(take()) for _ in range(kcount)}

    return values, kwargs


class Channel:
    """A framed message channel over one socket. Transport only.

    The framing, the timeout handling and the close are the same everywhere a
    process in this system talks to another one; only the *codec* differs by
    direction, so subclasses override `send`/`recv` and inherit the rest. Three
    near-identical copies of this existed — the sandbox host's, the runner's, and
    the tensor-parallel `Channel` — differing in which of `encode`/`pack` they
    reached for.

    A timeout applies to each `recv` **syscall**, not to a whole frame, so a
    timeout between a header and its body leaves the stream mid-frame and every
    later read misinterprets body bytes as a length. Treat a failed `recv` as
    fatal to the channel.
    """

    def __init__(self, sock) -> None:
        self.sock = sock

    def send_raw(self, data: bytes) -> None:
        send_frame(self.sock, data)

    def recv_raw(self, timeout=None) -> bytes:
        if timeout is None:
            return recv_frame(self.sock)
        self.sock.settimeout(timeout)
        try:
            return recv_frame(self.sock)
        finally:
            self.sock.settimeout(None)

    def send(self, value) -> None:
        """One value, cloudpickled unless it is already str/bytes."""
        self.send_raw(encode(value))

    def recv(self, timeout=None):
        """One value, as sent."""
        return decode(self.recv_raw(timeout))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

class RanksDiverged(Exception):
    """The peers behind a `Fanout` stopped agreeing about what is happening.

    Under tensor parallelism the ranks run one program between them, joined by
    collectives. If one asks to resume a worker while another asks something else,
    they have already taken different paths, and the next collective either hangs
    or combines tensors computed from different things. Raising here turns that
    into a diagnosable error at the moment it becomes knowable, instead of a stall
    that surfaces as an execution timeout with no cause attached.
    """


class Fanout:
    """One channel-shaped view over several peers that must agree.

    Presents `send`/`recv` so a caller written against a single
    [`Channel`][ndif.services.ray.sandbox.protocol.Channel] needs no changes:

    * **send** goes to every peer, so they all see the same instruction.
    * **recv** waits for *all* of them, checks they are asking the same thing, and
      returns one answer.

    That second half is the barrier. A worker driven through this runs **once** per
    serve however many peers there are, which is the point — the peers are ranks
    of one model, not independent callers, and resuming a worker once per rank
    would run the user's block N times over.

    Whose answer is returned: the first peer's. Where the peers hold pieces of a
    sharded tensor, their values differ and one has to be chosen; where the value
    was made whole before they sent it, they are identical and the choice does not
    matter.

    Args:
        channels: one per peer, in a fixed order. ``channels[0]`` is the peer whose
            reply is returned.
        signature: what must match across peers, given a received message.
            Defaults to the event name — enough to catch one peer resuming a worker
            while another finishes. A caller that knows the protocol can compare
            more.
        timeout: how long to wait for the group. Bounded on purpose: peers that
            have genuinely diverged will never all arrive, and a stall is a worse
            way to learn that than an error.
    """

    def __init__(self, channels, signature=None, timeout: float = 300.0) -> None:
        self.channels = list(channels)
        self._signature = signature or (lambda message: _event_of(message))
        self.timeout = timeout

    def send(self, *args, **kwargs) -> None:
        """Send the same message to every peer."""
        for channel in self.channels:
            channel.send(*args, **kwargs)

    def recv(self, timeout=None):
        """Wait for every peer, check they agree, and return the first one's message.

        Raises:
            RanksDiverged: if a peer never arrives, drops its connection, or asks
                for something different from the others.
        """
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        waiting = {channel.sock.fileno(): index for index, channel in enumerate(self.channels)}
        received: dict = {}

        while waiting:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RanksDiverged(
                    f"{len(waiting)} of {len(self.channels)} peers did not answer "
                    f"within {self.timeout}s: {sorted(waiting.values())}"
                )
            ready, _, _ = select.select(list(waiting), [], [], remaining)
            for fileno in ready:
                index = waiting.pop(fileno)
                try:
                    received[index] = self.channels[index].recv()
                except Exception as error:
                    raise RanksDiverged(f"peer {index} dropped: {error}") from error

        signatures = {index: self._signature(message) for index, message in received.items()}
        expected = signatures[0]
        disagreeing = {i: s for i, s in signatures.items() if s != expected}
        if disagreeing:
            raise RanksDiverged(
                f"peers disagree: peer 0 sent {expected!r}, "
                + ", ".join(f"peer {i} sent {s!r}" for i, s in sorted(disagreeing.items()))
            )
        return received[0]

    def close(self) -> None:
        for channel in self.channels:
            channel.close()


def _event_of(message):
    """The event name in a received message, whichever codec produced it.

    `unpack` yields ``(values, kwargs)`` and `decode` yields the value itself, so
    a Fanout can sit on either direction without being told which.
    """
    if isinstance(message, tuple) and len(message) == 2 and isinstance(message[0], (list, tuple)):
        values = message[0]
        return values[0] if values else None
    if isinstance(message, (list, tuple)):
        return message[0] if message else None
    return message
