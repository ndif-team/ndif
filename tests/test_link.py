"""One end of the host-runner conversation, driven by a scripted peer.

`Link` receives by type: the caller names what it is waiting for, messages that
can arrive at any point are handed to their handlers on the way, and anything
else is an error where it arrives. Before it, a message of the wrong kind was
read as whatever the receiver expected, and failed later in an unrelated place
(#362).

No GPUs, no model, no runner: a socket pair and a thread that plays the peer,
like ``test_fanout.py``.
"""

from __future__ import annotations

import socket
import threading

import pytest

from ndif.services.ray.sandbox.link import Link, ProtocolError
from ndif.services.ray.sandbox.messages import Interleave, Park, Print, Reply, Resume, Source
from ndif.services.ray.sandbox.protocol import Channel, Fanout


def peer(sock, script):
    """Answer each message received on ``sock`` with the next list in ``script``."""

    def play():
        channel = Channel(sock)
        for answers in script:
            channel.recv()
            for answer in answers:
                channel.send(answer)

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    return thread


def linked(script, always=None):
    ours, theirs = socket.socketpair()
    return Link(Channel(ours), always), peer(theirs, script)


def test_a_request_returns_the_reply_it_named():
    link, thread = linked([[Park(0, None)]])

    answer = link.request(Resume(0, ("value",), None), Park)

    assert answer == Park(0, None)
    thread.join(timeout=5)


def test_what_can_arrive_at_any_point_is_served_on_the_way():
    printed = []
    link, thread = linked(
        [[Print("one"), Print("two"), Park(0, None)]],
        always={Print: lambda message: printed.append(message.text)},
    )

    answer = link.request(Resume(0, (), None), Park)

    assert printed == ["one", "two"]
    assert isinstance(answer, Park)
    thread.join(timeout=5)


def test_a_message_nobody_was_waiting_for_is_named():
    # The shape of #362: the host waits for a worker's park and the runner, out
    # of step, starts its next run.
    link, thread = linked([[Interleave("_call", [], [], [], {})]])

    with pytest.raises(ProtocolError, match="got Interleave while waiting for Park"):
        link.request(Resume(0, (), None), Park)
    thread.join(timeout=5)


def test_a_control_request_is_told_from_a_park_by_its_type():
    link, thread = linked([[Park(0, Source("model.h.0"))], [Park(0, None)]])

    first = link.request(Resume(0, ("value",), None), Park)
    assert isinstance(first.pending, Source) and first.pending.path == "model.h.0"
    assert link.request(Reply(0, (None,)), Park).pending is None
    thread.join(timeout=5)


def test_it_sits_on_a_fanout_as_on_a_channel():
    # The runner's end under tensor parallelism: one link over several hosts.
    ours, theirs = [], []
    for _ in range(2):
        a, b = socket.socketpair()
        ours.append(Channel(a))
        theirs.append(Channel(b))
    link = Link(Fanout(ours))
    for host in theirs:
        host.send(Resume(0, (), None))

    assert isinstance(link.receive(Resume), Resume)
    link.send(Park(0, None))
    assert [host.recv() for host in theirs] == [Park(0, None)] * 2
