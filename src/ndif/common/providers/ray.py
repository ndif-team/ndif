"""Ray control-plane provider for the queue.

The queue touches Ray only through this module: the connection lifecycle
(RayProvider) and lookups for the controller actor and per-replica model
actors. Actor names mirror the controller's: the controller is ``Controller``
and a replica is ``{replica_id}:ModelActor:{model_key}``, both in the ``NDIF``
namespace.
"""

import logging
import threading
from concurrent.futures import InvalidStateError

import ray
from ray.util.client import ray as client_ray
from ray.util.client.common import ClientActorHandle, ClientObjectRef

from ..types import MODEL_KEY, REPLICA_ID
from .base import Provider
from .util import verify_connection

logger = logging.getLogger("ndif")

NAMESPACE = "NDIF"


class CachedActorError(Exception):
    """A ModelActor that has been moved to CPU cache (WARM).

    The actor process is still alive, but it is no longer serving on GPU, so a
    dispatch must be treated the same as hitting an evicted/dead replica.

    **Do not catch this by type on the caller's side.** Raised inside the actor,
    it reaches the caller wrapped in a ``ray.exceptions.RayTaskError``, and the
    dual RayTaskError-plus-cause class that would satisfy
    ``isinstance(e, CachedActorError)`` is only built when Ray applies
    ``as_instanceof_cause()`` — which it does not over Ray Client, the way the
    dispatcher connects. The wrapper arrives plain and the cause has to be read
    off ``.cause``; see the eviction check in ``queue.replica.Replica.dispatch``.

    Getting this wrong is expensive and quiet: a bare ``except EVICTED_ERRORS``
    matches nothing, and every HOT->WARM demotion errors the in-flight request
    instead of re-queueing it, on a cluster that otherwise looks healthy.
    """

    pass


class RayProvider(Provider):
    """Manages the process's Ray connection.

    ``connected()`` is true only once the cluster is reachable *and* the
    Controller actor exists, so the dispatcher's connect loop won't proceed
    until the control plane is actually serving.
    """

    CONFIG = {"ray_url": ("NDIF_RAY_ADDRESS", "ray://localhost:10001", str)}

    ray_url: str

    @classmethod
    def get_host_port(cls):
        """Parse ``(host, port)`` from ``ray_url`` (defaulting the port to 6379)."""
        if not getattr(cls, "ray_url", None):
            raise ValueError("ray_url is not set on RayProvider")
        if "://" in cls.ray_url:
            _, addr = cls.ray_url.split("://", 1)
        else:
            addr = cls.ray_url
        if "/" in addr:
            addr = addr.split("/", 1)[0]
        if ":" in addr:
            host, port = addr.split(":")
            port = int(port)
        else:
            logger.warning(
                f"NDIF_RAY_ADDRESS ({cls.ray_url}) does not specify a port, "
                f"using default port 6379"
            )
            host = addr
            port = 6379
        return host, port

    @classmethod
    def is_listening(cls) -> bool:
        """Whether the Ray address is accepting connections."""
        try:
            host, port = cls.get_host_port()
            return verify_connection(host, port)
        except Exception:
            return False

    @classmethod
    def connect(cls):
        host, port = cls.get_host_port()
        if not verify_connection(host, port):
            raise ConnectionError(f"Ray is not listening on {host}:{port}")
        ray.init(logging_level="error", address=cls.ray_url, namespace=NAMESPACE)

    @classmethod
    def connected(cls, call: bool = False) -> bool:
        """Whether the cluster is reachable and the Controller actor exists.

        ``call`` also waits for a call to the Controller to return, over the
        lookup, ``.remote()`` and result path a request takes.
        """
        connected = ray.is_initialized() and cls.is_listening()

        if connected:
            try:
                handle = get_named_actor("Controller", namespace=NAMESPACE)
                if call:
                    ready = handle.__ray_ready__.remote()
                    ready.future().result(RAY_CLIENT_TIMEOUT_S)
            except Exception:
                return False
            else:
                return True

        return False

    @classmethod
    def reset(cls):
        # ray.shutdown() waits for the client's data thread before it closes the
        # gRPC channel, and on a dead head that thread never finishes. Closing
        # the channel first ends it.
        worker = client_ray.get_context().client_worker
        if worker is not None and worker.channel is not None:
            worker.channel.close()
        ray.shutdown()

    # Error patterns that indicate a broken Ray connection.
    CONNECTION_ERROR_PATTERNS = (
        "Ray client has already been disconnected",
        "Unrecoverable error in data channel",
        "_MultiThreadedRendezvous",
        "Failed to reconnect",
        "session that has already been cleaned up",
        "Channel for client",
        "grpc._channel",
        "Failed during this or a previous request",
    )

    @classmethod
    def is_connection_error(cls, error: Exception) -> bool:
        """Whether ``error`` indicates the Ray connection itself is broken.

        Used reactively: when an error occurs, the dispatcher checks this and
        forces a reconnect if it matches.
        """
        error_str = str(error)
        return any(pattern in error_str for pattern in cls.CONNECTION_ERROR_PATTERNS)


RayProvider.from_env()


# ===========================================================================
# Lean Ray client actor handle.
# ===========================================================================
#
# Stock ``ClientActorHandle.__getattr__`` does an RPC on first attribute access
# to fetch every method's signature, then unpickles them client-side for
# arg-binding validation. The annotations are live class refs, so the unpickle
# resolves ``BackendRequestModel`` and its transitive deps on this side. Fine on
# api+ray which install the full dep set; broken on a slim ``--no-deps`` install.
#
# We don't use the client-side signatures — callers pass hardcoded method names
# with known-shape args. Override ``__getattr__`` to return our own remote-method
# stub that builds the wire task directly, skipping both the descriptor RPC and
# ``signature.bind``. The standard ``handle.method.remote(...)`` syntax keeps
# working unchanged.


class NDIFClientRemoteMethod:
    """Drop-in for Ray's ``ClientRemoteMethod`` that skips ``signature.bind``.

    Built on demand by :meth:`NDIFActorHandle.__getattr__`. Carries just the
    method name and a back-pointer to the actor handle — no signature, no
    descriptor cache.
    """

    __slots__ = ("_actor_handle", "_method_name", "_method_num_returns")

    def __init__(self, handle: "NDIFActorHandle", method_name: str):
        self._actor_handle = handle
        self._method_name = method_name
        self._method_num_returns = 1

    def remote(self, *args, **kwargs):
        # Awaiting a ref waits for its id with no timeout, on the caller's event
        # loop. Wait for it here instead, bounded.
        def call():
            (id_future,) = client_ray.call_remote(self, *args, **kwargs)
            ref = NDIFObjectRef(id_future)
            ref.binary()
            return ref

        return bounded(f"{self._method_name}.remote()", call)

    def _prepare_client_task(self):
        from ray.core.generated import ray_client_pb2

        t = ray_client_pb2.ClientTask()
        t.type = ray_client_pb2.ClientTask.METHOD
        t.name = self._method_name
        t.payload_id = self._actor_handle.actor_ref.id
        return t

    def _num_returns(self) -> int:
        return 1


class NDIFObjectRef(ClientObjectRef):
    """Ray ``ClientObjectRef`` that tolerates a wait on it having been cancelled.

    Ray gives a waiter its result, or the error that the connection closed, by
    setting it on the waiter's future, and that raises when the wait was
    cancelled (a ``wait_for`` timeout, a cancelled task). Raised from a result,
    it leaves the waiter registered with Ray for the life of the connection.
    Raised when the connection closes, it ends Ray's loop over the waiters
    (``DataClient._shutdown``), and those registered after the cancelled one are
    never told: they wait forever.
    """

    def _on_completed(self, py_callback):
        def deliver(data):
            try:
                py_callback(data)
            except InvalidStateError:
                pass

        super()._on_completed(deliver)


class NDIFActorHandle(ClientActorHandle):
    """Ray ``ClientActorHandle`` that returns :class:`NDIFClientRemoteMethod`
    for any unknown attribute.

    No ``_init_class_info`` ever fires, so we never RPC for method signatures.
    Standard ``handle.method.remote(...)`` works exactly like in stock Ray —
    just routed through our minimal remote-method stub.
    """

    def __getattr__(self, key):
        # Match the base-class recursion guards: these can be probed during
        # deserialization before instance state is populated, and must
        # AttributeError rather than fall through to method dispatch.
        if key in ("_method_num_returns", "_method_signatures"):
            raise AttributeError(key)
        return NDIFClientRemoteMethod(self, key)


# Most calls return in milliseconds. The first lookup of an actor class on a new
# connection takes seconds: Ray's per-client server imports the class.
RAY_CLIENT_TIMEOUT_S = 120


def bounded(what: str, fn, *args, **kwargs):
    """Call ``fn`` on a daemon thread; raise ``TimeoutError`` if it outlives
    ``RAY_CLIENT_TIMEOUT_S``.

    Over Ray Client, actor lookups and calls wait on futures with no timeout,
    so one in flight when the head dies never returns, and the dispatcher makes
    them on its event loop. A thread that times out is abandoned with whatever
    it holds (a ref dropped on the loop would wait for its id too).
    """
    outcome: list = []

    def run() -> None:
        try:
            outcome.append(fn(*args, **kwargs))
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(RAY_CLIENT_TIMEOUT_S)
    if not outcome:
        raise TimeoutError(
            f"{what} did not return within {RAY_CLIENT_TIMEOUT_S}s; "
            "the Ray connection is probably dead"
        )
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def get_named_actor(name: str, namespace: str = NAMESPACE) -> NDIFActorHandle:
    """Like ``ray.get_actor`` but bounded (see :func:`bounded`), and returns an
    :class:`NDIFActorHandle`.

    No-ops the class swap when not in Ray-client mode (native
    ``ray.actor.ActorHandle`` has no descriptor-prefetch behavior, so the
    standard ``remote()`` syntax already just works).
    """
    handle = bounded(
        f"ray.get_actor({name!r})", ray.get_actor, name, namespace=namespace
    )
    if isinstance(handle, ClientActorHandle):
        handle.__class__ = NDIFActorHandle
    return handle


def get_controller_actor_handle(namespace: str = NAMESPACE) -> NDIFActorHandle:
    """Handle to the singleton Controller actor."""
    return get_named_actor("Controller", namespace=namespace)


def get_model_actor_handle(
    model_key: MODEL_KEY, replica_id: REPLICA_ID, namespace: str = NAMESPACE
) -> NDIFActorHandle:
    """Handle to one replica's model actor."""
    return get_named_actor(
        f"{replica_id}:ModelActor:{model_key}", namespace=namespace
    )


# The queue uses a no-arg ``controller_handle()`` form — keep an alias so those
# imports don't have to change shape.
controller_handle = get_controller_actor_handle
