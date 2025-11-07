"""Ray Serve handle wrapper for model deployment interactions.

`Handle` resolves the Ray Serve application and actor namespace for a given
`model_key`, checks readiness, and provides `execute` to submit requests.
"""

import os

from ray.serve._private.client import *
from ray.util.client import ray as client_ray
from ray.util.client.common import return_refs

from ..types import MODEL_KEY, RAY_APP_NAME
from ..schema import BackendRequestModel


class Handle:
    """Encapsulates interaction with a single model Ray Serve deployment."""

    def __init__(self, model_key: MODEL_KEY):
        """Look up the deployment handle and namespace for `model_key`.

        Raises:
            RayServeException: If the application cannot be found yet.
        """

        self.model_key = model_key

        try:
            actor = ray.get_actor(self.actor_name, namespace="NDIF")
            self.ready_future = actor.__ray_ready__.remote()
        except Exception:
            raise RayServeException(f"Actor '{self.actor_name}' does not exist.")

    @property
    def actor_name(self) -> RAY_APP_NAME:
        """Canonical Ray actor name for the model deployment actor."""
        return f"ModelActor:{self.model_key}"

    @property
    def ready(self):
        """Whether the Ray Serve deployment reports readiness."""
        try:
            # Poll the namespace future until it completes.
            ray.get(self.ready_future, timeout=0)
            return True
        except TimeoutError:
            return False

    def execute(self, request: BackendRequestModel):
        """Submit `request` to the model actor and return a Ray future."""

        try:
            actor = ray.get_actor(self.actor_name, namespace="NDIF")
        except ValueError as e:

            if str(e).startswith("Failed to look up actor"):
                raise LookupError("Model deployment evicted.")

            raise

        # We reimplement `handle.remote()` to avoid an issue about looking up the signature for the remote call.
        # I imagine this has something to do with getting a named actor from a client not in the namespace.
        return return_refs(client_ray.call_remote(actor.__call__, request))
