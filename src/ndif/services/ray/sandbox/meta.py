"""The runner's weightless model, and resolving a request's ids against it.

A request names the model, its modules and its tokenizer by persistent id.
`load_meta_model` builds a meta model (structure, never weights) when the runner
starts, `set_env` reshapes it the way a request's environment reshapes the
host's, and `IPCCloudUnpickler` resolves the ids against it. The one id that does
not come from the model is the interleaver, which resolves to an
``IPCInterleaver`` bound to the request's link.
"""

import sys
import warnings

from nnsight.intervention.serialization import CustomCloudUnpickler
from nnsight.modeling.mixins.remotable import Remotable

from .worker import IPCInterleaver

# This runner's weightless model, and the persistent-id map built from it. The
# map is what `IPCCloudUnpickler` resolves a payload's ids against; the model is
# kept because it is the thing that produced the map.
META_MODEL = None
PERSISTENT_OBJECTS = {}


def load_meta_model(model_key: str) -> None:
    """Build this runner's meta model and register its persistent-id map.

    Called once as a runner process starts (``runner.build``, before the socket
    binds), so every request this process or its forks serve resolves
    ``Module:<path>`` / ``Tokenizer`` / ``Pipeline`` against a tree with the same
    paths as the host's real model. The build is undispatched — structure only,
    no weights — and its cost is paid inside ``spawn``'s readiness window rather
    than in a request.

    Nothing here dispatches: ``from_model_key`` defaults to the meta build, and the
    tracer the runner later deserializes is a *different* wrapper object whose
    ``__setstate__`` marks it dispatched, so no ``interleave`` in this process can
    decide it needs real weights. This object is only ever read for its map.

    ``trust_remote_code`` is in the key, so this build is the actor's build.

    No hub round trip is required: the actor loads its model before it opens its
    pool, so the config and tokenizer this reads are already in the shared cache,
    and huggingface_hub serves a cached file even when its HEAD call fails — which
    is what happens here for a gated repo, since a runner deliberately inherits no
    `HF_TOKEN`.
    """
    global META_MODEL

    # `from_model_key` resolves the wrapper class from the key itself, so this is
    # the same build the actor does (base.py) minus the weights -- and it is right
    # for a non-HuggingFace wrapper too.
    META_MODEL = Remotable.from_model_key(model_key)
    PERSISTENT_OBJECTS.update(META_MODEL._remoteable_persistent_objects())


def set_env(env: "dict | None") -> None:
    """Apply a request's environment to this runner's meta model, like the host.

    The host applies the same dict to the loaded model before the block runs
    (``base.py``), and for a PEFT adapter that *changes the module tree* — every
    path gains a ``base_model.model`` prefix. Without this the runner's map would
    describe the unadapted tree and every ``Module:`` id in an adapted request
    would miss it, so the runner would fall back to the pre-meta-model behaviour
    for exactly the requests that ask for something unusual.

    The adapter's weights land on meta parameters, which torch warns about once
    per tensor and is precisely what is wanted here: the paths are the point, the
    values are the host's. The warnings are swallowed because this process's
    stderr is the client's log.

    A no-op when the model already matches (the common case: no adapter at all),
    so an ordinary request pays a dict comparison.

    Best-effort, deliberately: the map is an optimization, and a runner that
    cannot follow the host into some environment still runs the request correctly
    with those ids resolving to ``None`` — which is all any of them did before
    meta models. Failing the request instead would turn a missing optional
    package in the *runner* into a user-facing error for a request the actor
    itself accepted.
    """
    if META_MODEL is None:
        return

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            META_MODEL._remoteable_set_env(env)
    except Exception as error:
        # The runner's stderr, not the client's: this is the actor's business.
        print(f"runner: could not apply the request env: {error!r}", file=sys.__stderr__)
        return

    PERSISTENT_OBJECTS.clear()
    PERSISTENT_OBJECTS.update(META_MODEL._remoteable_persistent_objects())


class IPCCloudUnpickler(CustomCloudUnpickler):
    """nnsight unpickler that resolves the interleaver to an IPC one.

    Passed to ``BackendRequestModel.deserialize(..., unpickler=...)`` with the
    request's link bound (``execute._run``), which instantiates it as
    ``unpickler(file, persistent_objects)`` — here the map ``load_meta_model``
    built from this runner's meta model, so
    ``Module:<path>``, ``Tokenizer`` and ``Pipeline`` ids resolve to real
    (weightless) objects in this process. Unlike the base class an unknown id is not
    an error: it resolves to ``None``, since the payload can mention objects only the
    host has (and a runner launched without a model key has an empty map, which is
    every id). Resolving to ``None`` is safe precisely because nothing local reads
    a module — every read is a socket round trip — so an id the meta tree lacks
    costs an ``AttributeError`` in the block rather than
    ``BackendRequestModel.deserialize``'s ``ArchitectureMismatchError``, which this
    path has never raised.
    """

    def __init__(self, file, persistent_objects, link) -> None:
        super().__init__(file, persistent_objects)
        self.link = link

    def persistent_load(self, pid):
        # One interleaver is shared across the envoy tree; bind it to this
        # request's socket. Checked before the map so the meta model's own
        # interleaver (also keyed "Interleaver") can't shadow the IPC one. The model
        # weights themselves are never in this process — a resolved module is the
        # meta build's, and reads of its activations still cross the socket.
        if pid == "Interleaver":
            return IPCInterleaver(self.link)
        elif pid in self.persistent_objects:
            return self.persistent_objects[pid]

        return None
