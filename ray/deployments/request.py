import asyncio

import ray
from ray import serve
from ray.serve import Application
from ray.serve.handle import DeploymentHandle

try:
    from slugify import slugify
except:
    pass

from nnsight.schema.Response import ResponseModel

from ...schema import BackendRequestModel
from .base import BaseDeployment, BaseDeploymentArgs


@serve.deployment()
class RequestDeployment(BaseDeployment):
    """
    A deployment class for handling requests in a Ray Serve application.

    This class extends BaseDeployment and provides functionality to process
    incoming requests, interact with model deployments, and manage responses.
    """

    async def __call__(self, request: BackendRequestModel):
        """
        Asynchronously process an incoming request.

        This method handles the request by attempting to get a handle to the
        appropriate model deployment, submitting the request for processing,
        and managing the response.

        Args:
            request (BackendRequestModel): The incoming request to be processed.

        Raises:
            Exception: Any exception that occurs during request processing.
        """
        try:
            model_key = f"Model:{slugify(request.model_key)}"

            app_handle = self.get_ray_app_handle(model_key)

            result = app_handle.remote(request)

            request.create_response(
                status=ResponseModel.JobStatus.APPROVED,
                description="Your job was approved and is waiting to be run.",
                logger=self.logger,
                gauge=self.gauge,
            ).respond(self.api_url, self.object_store)
            
            try:
                ray.wait(result, timeout=0)
            except:
                pass

        except Exception as exception:
            request.create_response(
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
                logger=self.logger,
                gauge=self.gauge,
            ).respond(self.api_url, self.object_store)

    def get_ray_app_handle(self, name: str) -> DeploymentHandle:
        """
        Get a handle to a Ray Serve application deployment.

        Args:
            name (str): The name of the deployment to get a handle for.

        Returns:
            DeploymentHandle: A handle to the specified Ray Serve deployment.
        """
        return serve.get_app_handle(name)


def app(args: BaseDeploymentArgs) -> Application:
    """
    Create and bind the RequestDeployment with the provided arguments.

    Args:
        args (BaseDeploymentArgs): Arguments for the RequestDeployment.

    Returns:
        Application: The created and bound RequestDeployment.
    """
    return RequestDeployment.bind(**args.model_dump())
