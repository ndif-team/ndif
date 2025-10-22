"""Ray Serve handle wrapper for model deployment interactions.

`Handle` resolves the Ray Serve application and actor namespace for a given
`model_key`, checks readiness, and provides `execute` to submit requests.
"""

import os

from ray.serve._private.client import *
from ray.serve.api import _get_global_client
from ray.util.client import ray as client_ray
from ray.util.client.common import return_refs
from slugify import slugify

from ..schema import BackendRequestModel


class Handle:
    """Encapsulates interaction with a single model Ray Serve deployment."""

    def __init__(self, model_key: str):
        """Look up the deployment handle and namespace for `model_key`.

        Raises:
            RayServeException: If the application cannot be found yet.
        """

        self.model_key = model_key

        app_name = self.app_name

        # We reimplement `serve.get_app_handle` to manually look up the ingress name.
        # That way we can add a timeout and avoid a deadlock.
        client = _get_global_client()
        deployment_name = ray.get(
            client._controller.get_ingress_deployment_name.remote(app_name),
            timeout=int(os.environ.get("COORDINATOR_HANDLE_TIMEOUT_S", "5")),
        )

        if deployment_name is None:
            raise RayServeException(f"Application '{app_name}' does not exist.")

        self.handle = DeploymentHandle(deployment_name, app_name)
        # We need the namespace of the deployment as otherwise we can't lookup its model actor.
        # We use completion of this call to determine the deployment is finished initializing.
        self.namespace = self.handle.ready.remote()

    @property
    def app_name(self):
        """Canonical Ray Serve app name derived from `model_key`."""
        return f"Model:{slugify(self.model_key)}"

    @property
    def actor_name(self):
        """Canonical Ray actor name for the model deployment actor."""
        return f"ModelActor:{self.model_key}"

    @property
    def ready(self):
        """Whether the Ray Serve deployment reports readiness."""
        try:
            # Poll the namespace future until it completes.
            self.namespace = self.namespace.result(timeout_s=0)
            return True
        except TimeoutError:
            return False

    def execute(self, request: BackendRequestModel):
        """Submit `request` to the model actor and return a Ray future."""

        actor = ray.get_actor(self.actor_name, namespace=self.namespace)

        # We reimplement `handle.remote()` to avoid an issue about looking up the signature for the remote call.
        # I imagine this has something to do with getting a named actor from a client not in the namespace.
        return return_refs(client_ray.call_remote(actor.__call__, request))
