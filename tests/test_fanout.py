"""The barrier that lets one runner drive several ranks.

Under tensor parallelism the ranks run one program between them, joined by
collectives. A single runner holding the user's workers therefore cannot treat
them as independent callers: resuming a worker once per rank would run the block
N times over, and letting one rank proceed while another waits puts them on
opposite sides of a collective.

`Fanout` is that constraint as transport — send to all, wait for all, hand back
one. It is deliberately channel-shaped so the runner's interleaver, written
against a single connection, needs no changes at all.

No GPUs, no model, no server: real socket pairs and scripted peers. That is the
whole point of testing it here rather than discovering the ordering rules from a
hang on eight cards.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

# No guard, unlike the suites beside it: `protocol.py` is pure transport — sockets,
# framing and cloudpickle — so this collects and runs in a client-only environment
# too, which is where someone debugging the wire is most likely to be.
from ndif.services.ray.sandbox.protocol import Channel, Fanout, RanksDiverged, pack, unpack


class RunnerEnd(Channel):
    """The runner's end of one rank's socket, with the runner's codec.

    Mirrors `sandbox.runner.Connection` without importing it — that module patches
    nnsight globally on import, which a test process should not inherit.
    """

    def send(self, event, *values, **kwargs):
        self.send_raw(pack((event, *values), kwargs))


class Peer(Channel):
    """A stand-in rank, with a rank's codec.

    Mirrors `sandbox.host.Connection`: a rank sends the runner one encoded value
    and receives the runner's ``(values, kwargs)``. The two directions differ
    deliberately, so a Fanout has to sit correctly on one of them.
    """

    def recv(self, timeout=None):
        return unpack(self.recv_raw(timeout))

    def reply(self, event, *values, after: float = 0.0):
        message = (event, *values)

        if after:
            timer = threading.Timer(after, lambda: self.send(message))
            timer.daemon = True
            timer.start()
        else:
            self.send(message)


def group(count=3, **kwargs):
    """A Fanout over ``count`` peers, plus the peers' own ends."""
    ours, theirs = [], []
    for _ in range(count):
        a, b = socket.socketpair()
        ours.append(RunnerEnd(a))
        theirs.append(Peer(b))
    return Fanout(ours, **kwargs), theirs


class TestTheBarrier:
    """A serve completes only when every rank has arrived."""

    def test_it_waits_for_every_peer(self):
        fanout, peers = group()
        peers[0].reply("RESUME", 0, ("value",), 0)
        peers[1].reply("RESUME", 0, ("value",), 0)
        peers[2].reply("RESUME", 0, ("value",), 0, after=0.25)

        started = time.time()
        message = fanout.recv()

        assert time.time() - started >= 0.2, "returned before the last peer arrived"
        assert message[0] == "RESUME"

    def test_arrival_order_does_not_matter(self):
        fanout, peers = group()
        peers[2].reply("RESUME", 0, (), 0)
        peers[0].reply("RESUME", 0, (), 0, after=0.15)
        peers[1].reply("RESUME", 0, (), 0)

        assert fanout.recv()[0] == "RESUME"

    def test_the_first_peers_message_is_the_one_returned(self):
        # Where the ranks hold pieces of a sharded tensor their values differ and
        # one has to be chosen. Where the value was made whole first they are
        # identical and the choice does not matter.
        fanout, peers = group()
        peers[0].reply("RESUME", 0, ("rank0",), 0)
        peers[1].reply("RESUME", 0, ("rank1",), 0)
        peers[2].reply("RESUME", 0, ("rank2",), 0)

        message = fanout.recv()

        assert message[2] == ("rank0",)

    def test_one_send_reaches_every_peer(self):
        fanout, peers = group()

        fanout.send("PARK", 7)

        for peer in peers:
            values, _ = peer.recv()
            # `unpack` yields a list, not the tuple that went in.
            assert list(values) == ["PARK", 7]

    def test_a_worker_is_served_once_per_serve_not_once_per_rank(self):
        # The property the whole design rests on. Three ranks arrive; the caller
        # sees one message, so it switches its worker one time.
        fanout, peers = group()
        for peer in peers:
            peer.reply("RESUME", 0, (), 0)

        served = 0
        message = fanout.recv()
        if message is not None:
            served += 1

        assert served == 1


class TestDivergence:
    """Ranks that stop agreeing are reported, not waited on."""

    def test_peers_asking_different_things_raise(self):
        fanout, peers = group()
        peers[0].reply("RESUME", 0, (), 0)
        peers[1].reply("RESUME", 0, (), 0)
        peers[2].reply("DONE", None)  # took a different path

        with pytest.raises(RanksDiverged, match="disagree"):
            fanout.recv()

    def test_the_message_names_who_disagreed(self):
        fanout, peers = group()
        peers[0].reply("RESUME", 0, (), 0)
        peers[1].reply("DONE", None)
        peers[2].reply("RESUME", 0, (), 0)

        with pytest.raises(RanksDiverged) as caught:
            fanout.recv()

        assert "peer 1" in str(caught.value)

    def test_a_peer_that_never_arrives_raises_rather_than_hanging(self):
        # A rank that has genuinely diverged will never arrive. Waiting forever is
        # a worse way to learn that than an error naming it.
        fanout, peers = group(timeout=0.4)
        peers[0].reply("RESUME", 0, (), 0)
        peers[1].reply("RESUME", 0, (), 0)

        with pytest.raises(RanksDiverged, match="did not answer"):
            fanout.recv()

    def test_the_late_peer_is_named(self):
        fanout, peers = group(timeout=0.3)
        peers[0].reply("RESUME", 0, (), 0)
        peers[2].reply("RESUME", 0, (), 0)

        with pytest.raises(RanksDiverged) as caught:
            fanout.recv()

        assert "[1]" in str(caught.value)

    def test_a_dropped_peer_raises(self):
        fanout, peers = group()
        peers[0].reply("RESUME", 0, (), 0)
        peers[1].reply("RESUME", 0, (), 0)
        peers[2].close()  # the process died mid-request

        with pytest.raises(RanksDiverged, match="dropped"):
            fanout.recv()

    def test_a_stricter_signature_catches_finer_disagreement(self):
        # The default compares the event name, which catches a rank finishing while
        # others resume. A caller that knows the protocol can compare the control
        # fields too -- here, which worker is being resumed.
        def worker_and_event(message):
            return message[0], message[1]

        fanout, peers = group(signature=worker_and_event)
        peers[0].reply("RESUME", 0, (), 0)
        peers[1].reply("RESUME", 1, (), 0)  # a different worker
        peers[2].reply("RESUME", 0, (), 0)

        with pytest.raises(RanksDiverged):
            fanout.recv()


class TestItLooksLikeAChannel:
    """The runner's interleaver is written against one connection; it must not care."""

    def test_it_offers_the_calls_a_channel_does(self):
        fanout, _ = group()

        for name in ("send", "recv", "close"):
            assert callable(getattr(fanout, name))

    def test_a_group_of_one_behaves_like_a_plain_channel(self):
        # Which is what makes a single-GPU sandbox and a tensor-parallel one the
        # same code path with a different number of peers.
        fanout, peers = group(count=1)
        peers[0].reply("END", b"blob", 1.5)

        message = fanout.recv()

        assert message[0] == "END" and message[1] == b"blob"

    def test_close_closes_every_peer(self):
        fanout, peers = group()

        fanout.close()

        for peer in peers:
            with pytest.raises(Exception):
                peer.sock.sendall(b"x" * 8)
                peer.sock.sendall(b"x" * 8)


class TestItDropsIntoThePumpLoop:
    """The runner's loop is `while True: message = connection.recv()`.

    Nothing about it knows how many peers exist, and that is the property that
    makes a sandboxed tensor-parallel request the same code as a sandboxed
    single-GPU one. Driving a pump-shaped loop over a real Fanout is the check;
    asserting `hasattr(fanout, "recv")` would not be.
    """

    @staticmethod
    def _pump(connection, worker):
        """The shape of `IPCInterleaver.pump`, with a counter for a worker."""
        while True:
            message = connection.recv()
            kind = message[0]
            if kind == "DONE":
                return message[1]
            if kind == "RESUME":
                _, mediator_id, args, pin = message
                park = worker(*args)
                connection.send("PARK", mediator_id, park)

    def test_three_ranks_serve_a_worker_once_each_time(self):
        fanout, peers = group()
        switches = []

        def worker(*args):
            switches.append(args)
            return ("PARKED", len(switches))

        def rank(peer, index):
            # Each rank drives its own forward and reaches the same two locations.
            for step in range(2):
                peer.send(("RESUME", 0, (f"value-{index}-{step}",), 0))
                assert peer.recv()[0][0] == "PARK"
            peer.send(("DONE", f"result-{index}"))

        threads = [
            threading.Thread(target=rank, args=(peer, index), daemon=True)
            for index, peer in enumerate(peers)
        ]
        for thread in threads:
            thread.start()

        result = self._pump(fanout, worker)
        for thread in threads:
            thread.join(timeout=5)

        # Two locations, three ranks: the worker ran twice, not six times.
        assert len(switches) == 2, f"worker ran {len(switches)} times, expected 2"
        # And it was handed the first rank's value each time.
        assert switches[0] == ("value-0-0",)
        assert result == "result-0"

    def test_a_reply_reaches_every_rank_so_none_is_left_waiting(self):
        fanout, peers = group()
        replies = []

        def rank(peer):
            peer.send(("RESUME", 0, (), 0))
            replies.append(peer.recv()[0][0])
            peer.send(("DONE", None))

        threads = [threading.Thread(target=rank, args=(peer,), daemon=True) for peer in peers]
        for thread in threads:
            thread.start()

        self._pump(fanout, lambda *a: ("PARKED",))
        for thread in threads:
            thread.join(timeout=5)

        assert replies == ["PARK"] * 3
