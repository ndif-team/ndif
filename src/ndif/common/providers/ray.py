"""Ray control-plane provider.

Lookups for the controller actor and per-replica model actors. The queue runs
inside the cluster and uses Ray's own handles (``controller_handle``,
``model_handle``). The CLI and the dashboard connect over Ray Client
(``ray://``) and get ``RayProvider`` and handles with bounded lookups and calls.

Actor names mirror the controller's: the controller is ``Controller``, the
dispatcher is ``Dispatcher`` and a replica is
``{replica_id}:ModelActor:{model_key}``, all in the ``NDIF`` namespace.
"""

import logging
import threading
from concurrent.futures import Future, wait

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

    Raised inside the actor, it reaches the dispatcher as a
    ``RayTaskError(CachedActorError)``, which is an instance of both classes, so
    ``isinstance(e, CachedActorError)`` holds there. Over Ray Client the wrapper
    arrives plain and the cause has to be read off ``.cause``.
    """

    pass


class RayProvider(Provider):
    """Manages a Ray Client connection, for the CLI and the dashboard.

    ``connected()`` is true only once the cluster is reachable *and* the
    Controller actor exists.
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
    def connected(cls) -> bool:
        """Whether the cluster is reachable and the Controller actor exists.

        The lookup goes to the head over the channel every call uses, and does
        not wait on the Controller itself, which may be busy.
        """
        try:
            # On a new connection ray.is_initialized() asks the head, and raises
            # if the head has gone.
            if not (ray.is_initialized() and cls.is_listening()):
                return False
            get_named_actor("Controller", namespace=NAMESPACE)
        except Exception:
            return False
        return True

    @classmethod
    def reset(cls):
        # ray.shutdown() waits for the client's data thread before it closes the
        # gRPC channel, and on a dead head that thread can take minutes to give
        # up. Closing the channel first ends it. Marked as shutting down before that, or
        # Ray's threads take the closed channel for a fault and try to reconnect.
        worker = client_ray.get_context().client_worker
        if worker is not None and worker.channel is not None:
            worker._in_shutdown = True
            worker.channel.close()
        ray.shutdown()


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
        # A ref whose id has not arrived waits for it on first use, with no
        # timeout, so it is waited for here, bounded.
        def call() -> ClientObjectRef:
            (id_future,) = client_ray.call_remote(self, *args, **kwargs)
            ref = ClientObjectRef(id_future)
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
RAY_CLIENT_TIMEOUT_S = 60


def bounded(what: str, fn, *args, **kwargs):
    """Call ``fn`` on a daemon thread; raise ``TimeoutError`` if it outlives
    ``RAY_CLIENT_TIMEOUT_S``.

    Over Ray Client, actor lookups and calls wait on futures with no timeout:
    one in flight when the head dies waits until Ray gives up on the
    connection, and for good if Ray then fails to tell it. A thread that times
    out is abandoned with whatever it holds.
    """
    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as e:
            future.set_exception(e)

    threading.Thread(target=run, daemon=True).start()
    wait([future], RAY_CLIENT_TIMEOUT_S)
    if not future.done():
        raise TimeoutError(
            f"{what} did not return within {RAY_CLIENT_TIMEOUT_S}s; "
            "the Ray connection is probably dead"
        )
    return future.result()


def get_named_actor(name: str, namespace: str = NAMESPACE) -> NDIFActorHandle:
    """``ray.get_actor``, bounded (see :func:`bounded`), as an
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


def model_actor_name(model_key: MODEL_KEY, replica_id: REPLICA_ID) -> str:
    return f"{replica_id}:ModelActor:{model_key}"


def get_model_actor_handle(
    model_key: MODEL_KEY, replica_id: REPLICA_ID, namespace: str = NAMESPACE
) -> NDIFActorHandle:
    """Handle to one replica's model actor."""
    return get_named_actor(model_actor_name(model_key, replica_id), namespace=namespace)


# ===========================================================================
# Handles inside the cluster.
# ===========================================================================
#
# The dispatcher is an actor, so it is already part of the cluster and Ray's own
# handles do everything: ``await handle.method.remote(...)`` yields to the event
# loop, a missing actor raises ``ValueError`` from the lookup, and a dead one
# raises ``ActorDiedError`` from the call.


def controller_handle() -> "ray.actor.ActorHandle":
    """The Controller actor, for the queue."""
    return ray.get_actor("Controller", namespace=NAMESPACE)


def model_handle(model_key: MODEL_KEY, replica_id: REPLICA_ID) -> "ray.actor.ActorHandle":
    """One replica's model actor, for the queue."""
    return ray.get_actor(model_actor_name(model_key, replica_id), namespace=NAMESPACE)
