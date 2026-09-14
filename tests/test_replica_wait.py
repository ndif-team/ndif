"""Waiting for a replica: what counts as "not yet" and what counts as "never".

The distinction is the whole job. Ray reports both through the same exception
family — ``ActorUnavailableError`` and ``ActorDiedError`` are each a
``RayActorError`` — so catching the parent makes a model that refused to load
indistinguishable from one that is merely slow. With ``max_restarts=-1`` the
actor is respawned to raise the identical error again, forever, and the operator
gets a deadline instead of a cause.

That is not hypothetical: deploying gpt2 tensor-parallel raises
``UnshardableCheckpoint`` in the actor's constructor, and the CLI reported
"initialization timed out" five minutes later while the real message sat in the
actor log.

The exceptions here are built the way Ray builds them — an ``ActorDiedError``
wrapping the ``RayTaskError`` from a creation task that raised — because the
point being asserted is that the reason *survives* the trip, and a hand-made
stand-in could not show that.

Pure unit tests over fakes: no Ray cluster, no server, so they run anywhere.
"""

from __future__ import annotations

import pytest

pytest.importorskip("ray", reason="the exception classes under test are Ray's")

from ray.exceptions import (
    ActorDiedError,
    ActorUnavailableError,
    RayActorError,
    RayTaskError,
)

#: The message the tensor-parallel refusal actually produces, abbreviated.
REFUSAL = (
    "UnshardableCheckpoint: this checkpoint cannot be split tensor-parallel, "
    "so tp_size=2 would load a whole copy of it onto every rank"
)


def died(reason: str = REFUSAL) -> ActorDiedError:
    """What a constructor that raised looks like from the waiting side."""
    return ActorDiedError(RayTaskError("ModelActor.__init__", reason, ValueError))


def unavailable() -> ActorUnavailableError:
    """What an actor mid-restart looks like."""
    return ActorUnavailableError("restarting", None)


class FakeHandle:
    """An actor handle whose ``__ray_ready__`` replays a scripted sequence."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    class _Ready:
        def __init__(self, handle):
            self.handle = handle

        def remote(self):
            self.handle.calls += 1
            outcome = self.handle.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    @property
    def __ray_ready__(self):
        return self._Ready(self)


@pytest.fixture
def wait(monkeypatch):
    """`wait_for_replica_ready` with Ray's lookup and the sleep replaced.

    `ray.get` is the identity: the fake raises from `remote()` instead, which
    puts the exception on the same side of the call the real one does. A
    `ValueError` in the script is raised from the *lookup* rather than the
    readiness call, because that is where the real one comes from — the actor's
    name has not resolved yet.
    """
    import ray

    from ndif.cli.lib import models

    slept = []
    monkeypatch.setattr(models.time, "sleep", lambda seconds: slept.append(seconds))
    # `ray.get` on the real module, not a stub module: Ray's own exception
    # constructors reach back into the package, so replacing it wholesale breaks
    # the very objects these tests are built from.
    monkeypatch.setattr(ray, "get", lambda value: value)

    state = {}

    def run(outcomes):
        handle = FakeHandle(outcomes)
        state["handle"] = handle

        def lookup(model_key, replica_id):
            if handle.outcomes and isinstance(handle.outcomes[0], ValueError):
                handle.outcomes.pop(0)
                handle.calls += 1
                raise ValueError("actor not registered")
            return handle

        monkeypatch.setattr("ndif.common.providers.ray.get_model_actor_handle", lookup)
        models.wait_for_replica_ready("model", "replica")
        return handle, slept

    run.state = state
    return run


class TestStillLoading:
    """The two failures that mean "not yet" have to be polled through."""

    def test_an_unavailable_actor_is_polled_through(self, wait):
        handle, slept = wait([unavailable(), None])

        assert handle.calls == 2, "gave up while the actor was still restarting"
        assert slept, "spun without sleeping between polls"

    def test_a_lookup_that_has_not_resolved_is_polled_through(self, wait):
        # The controller creates the actor asynchronously after deploy returns.
        handle, _ = wait([ValueError("actor not registered"), None])

        assert handle.calls == 2

    def test_the_two_transient_states_interleave(self, wait):
        # The real sequence: not registered, then registered but restarting.
        handle, _ = wait([ValueError("nope"), unavailable(), None])

        assert handle.calls == 3

    def test_it_waits_as_long_as_it_takes(self, wait):
        # No deadline: a large model across many GPUs has no sensible upper
        # bound, and a deadline cannot tell slow from broken anyway.
        handle, _ = wait([unavailable()] * 200 + [None])

        assert handle.calls == 201

    def test_it_returns_once_the_actor_answers(self, wait):
        handle, _ = wait([None])

        assert handle.calls == 1


class TestNeverLoading:
    """A constructor that raised must come back out, not be polled through."""

    def test_a_dead_actor_propagates(self, wait):
        with pytest.raises(ActorDiedError):
            wait([died()])

    def test_the_reason_survives(self, wait):
        # The operator has to read *why* off what escapes; that is the entire
        # difference from a timeout, which can only report how long it waited.
        with pytest.raises(ActorDiedError, match="cannot be split tensor-parallel"):
            wait([died()])

    def test_it_does_not_poll_a_dead_actor(self, wait):
        # Retrying is what made this look like a slow start: max_restarts=-1
        # respawns the actor to raise the identical error again.
        with pytest.raises(ActorDiedError):
            wait([died(), None])

        assert wait.state["handle"].calls == 1, "polled on past a permanent failure"

    def test_an_unrelated_error_propagates(self, wait):
        # A connection failure is not "still loading" either; the caller reports
        # it rather than waiting forever on something that will never answer.
        with pytest.raises(ConnectionError):
            wait([ConnectionError("lost the cluster")])

    def test_a_dead_actor_after_a_transient_one_still_propagates(self, wait):
        # The ordinary shape of a real failure: it looks like it is coming up,
        # and then it dies.
        with pytest.raises(ActorDiedError, match="cannot be split"):
            wait([unavailable(), ValueError("nope"), died()])


class TestTheDistinctionIsLoadBearing:
    """Why the parent class must not be caught."""

    def test_both_states_share_a_parent(self):
        # The reason the original code was wrong, stated once.
        assert issubclass(ActorUnavailableError, RayActorError)
        assert issubclass(ActorDiedError, RayActorError)

    def test_the_wait_does_not_catch_the_parent(self):
        import inspect

        from ndif.cli.lib.models import wait_for_replica_ready

        source = inspect.getsource(wait_for_replica_ready)
        # "except (" rather than "except": the docstring explains the bug, and
        # the word "exception's" in it is a prefix match that lands the slice in
        # the prose -- where the class name legitimately appears.
        caught = source[source.index("except (") :]

        assert "RayActorError" not in caught, (
            "catching RayActorError swallows ActorDiedError, which is the whole "
            "bug: a model that refused to load looks like one that is slow"
        )

    def test_there_is_no_deadline(self):
        import inspect

        from ndif.cli.lib.models import wait_for_replica_ready

        source = inspect.getsource(wait_for_replica_ready)
        body = source[source.rindex('"""') :]

        assert "timeout" not in body, "a deadline reports the wait, not the cause"

    def test_a_creation_failure_carries_its_traceback(self):
        # Not testing our code -- testing the premise it rests on. If Ray ever
        # stopped attaching the cause, propagating would buy nothing and this
        # would say so.
        assert "cannot be split tensor-parallel" in str(died())
