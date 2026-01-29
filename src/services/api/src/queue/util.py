import time
import ray

from ray.util.client import ray as client_ray
from ray.util.client.common import return_refs


def patch():
    # We patch the _async_send method to avoid a nasty deadlock bug in Ray.
    # If a seperate thread derefernces a ClientObjectRef during an async send, both are vying for the same lock.
    # Therefore we prevent deleting the ClientObjectRef until the async send is complete.
    # Should probably just `delay` the deletion until the async send is complete, not prevent it entirely.
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


def submit(actor: ray.actor.ActorHandle, method: str, *args, **kwargs):
    return return_refs(client_ray.call_remote(getattr(actor, method), *args, **kwargs))


def get_actor_handle(name: str) -> ray.actor.ActorHandle:
    return ray.get_actor(name, namespace="NDIF")


def controller_handle():
    return get_actor_handle("Controller")

def model_actor_name(model_key: str, replica_id: int = 0) -> str:
    return f"ModelActor:{model_key}:{replica_id}"

def get_model_actor_handle(
    model_key: str, replica_id: int = 0
) -> ray.actor.ActorHandle:
    try:
        return get_actor_handle(model_actor_name(model_key, replica_id))
    except Exception:
        raise RuntimeError(f"ModelActor {model_actor_name(model_key, replica_id)} not found")