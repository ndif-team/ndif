import logging
import os

import ray
from ray.util.client import ray as client_ray
from ray.util.client.common import ClientActorHandle, return_refs

from . import Provider
from .util import verify_connection

logger = logging.getLogger("ndif")


class RayProvider(Provider):
    ray_url: str

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.ray_url = os.environ.get("NDIF_RAY_ADDRESS", "ray://localhost:10001")

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "NDIF_RAY_ADDRESS": cls.ray_url,
        }

    @classmethod
    def get_host_port(cls):
        """
        Returns a tuple (host, port) parsed from the ray_url.
        If port is not specified, defaults to 6379.
        """
        if not hasattr(cls, "ray_url") or not cls.ray_url:
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
                f"NDIF_RAY_ADDRESS ({cls.ray_url}) does not specify a port, using default port 6379"
            )
            host = addr
            port = 6379  # Default Ray port if not specified
        return host, port

    @classmethod
    def is_listening(cls) -> bool:
        """Check if the Ray address is listening."""
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
        ray.init(logging_level="error", address=cls.ray_url)

    @classmethod
    def connected(cls) -> bool:
        connected = ray.is_initialized() and cls.is_listening()

        if connected:
            try:
                ray.get_actor("Controller", namespace="NDIF")
            except:
                return False
            else:
                return True

        return False

    @classmethod
    def reset(cls):
        ray.shutdown()

    # Error patterns that indicate a broken Ray connection
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
        """Check if an exception indicates a broken Ray connection.

        This is used reactively - when an error occurs, we check if it's
        a connection error and force reconnection if so.

        Args:
            error: The exception to check.

        Returns:
            True if this error indicates the Ray connection is broken.
        """
        error_str = str(error)
        return any(pattern in error_str for pattern in cls.CONNECTION_ERROR_PATTERNS)


RayProvider.from_env()


# ===========================================================================
# Lean Ray client actor handle.
# ===========================================================================
#
# Stock ``ClientActorHandle.__getattr__`` does an RPC on first attribute
# access to fetch every method's signature, then unpickles them client-side
# for arg-binding validation. The annotations are live class refs, so the
# unpickle resolves ``BackendRequestModel`` → ``common/schema/mixins.py`` →
# ``botocore`` (and ``opentelemetry``, etc.) on this side. Fine on api+ray
# which install the full dep set; broken on the dashboard image (slim
# ``--no-deps`` install of ndif).
#
# We don't use the client-side signatures — callers pass hardcoded method
# names with known-shape args. Override ``__getattr__`` to return our own
# remote-method stub that constructs the wire task directly, skipping both
# the descriptor RPC and ``signature.bind``. The standard
# ``handle.method.remote(...)`` syntax keeps working unchanged.


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
        return return_refs(client_ray.call_remote(self, *args, **kwargs))

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


def get_named_actor(name: str, namespace: str = "NDIF") -> NDIFActorHandle:
    """Like ``ray.get_actor`` but returns an :class:`NDIFActorHandle`.

    No-ops the class swap when not in Ray-client mode (native
    ``ray.actor.ActorHandle`` doesn't have the descriptor-prefetch behavior,
    so the standard ``remote()`` syntax already just works).
    """
    handle = ray.get_actor(name, namespace=namespace)
    if isinstance(handle, ClientActorHandle):
        handle.__class__ = NDIFActorHandle
    return handle


def get_controller_actor_handle(namespace: str = "NDIF") -> NDIFActorHandle:
    return get_named_actor("Controller", namespace=namespace)


def get_model_actor_handle(
    model_key: str, replica_id: str, namespace: str = "NDIF"
) -> NDIFActorHandle:
    return get_named_actor(
        f"{replica_id}:ModelActor:{model_key}", namespace=namespace
    )


# api/queue used a no-arg ``controller_handle()`` form — keep an alias so
# those imports don't have to change shape.
controller_handle = get_controller_actor_handle


# ===========================================================================
# Ray client deadlock patch.
# ===========================================================================


def patch():
    """Work around a Ray client deadlock between async-send and ref-deletion.

    If a separate thread dereferences a ``ClientObjectRef`` during an async
    send, both contend for the same lock. Disable ref deletion for the
    duration of the send. Should probably *delay* the deletion until the
    send completes, not block it entirely.
    """
    from ray.util.client import dataclient, common

    original_async_send = dataclient.DataClient._async_send

    def _async_send(_self, req, callback=None):
        original_ref_deletion = common.ClientObjectRef.__del__
        common.ClientObjectRef.__del__ = lambda self: None
        try:
            original_async_send(_self, req, callback)
        finally:
            common.ClientObjectRef.__del__ = original_ref_deletion

    dataclient.DataClient._async_send = _async_send
