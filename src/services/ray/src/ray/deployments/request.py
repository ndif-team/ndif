import asyncio
from functools import wraps
import traceback

import ray
from ray import serve
from ray.serve import Application
from ray.serve.handle import DeploymentHandle

try:
    from slugify import slugify
except:
    pass

from nnsight.schema.response import ResponseModel

from ...schema import BackendRequestModel
from .base import BaseDeployment, BaseDeploymentArgs


@serve.deployment(max_ongoing_requests=200, max_queued_requests=200)
class RequestDeployment(BaseDeployment):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def __call__(self, request: BackendRequestModel):

        if not self.sio.connected:
            self.sio.connect(
                self.api_url,
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=100000,
            )

        try:

            model_key = f"Model:{slugify(request.model_key)}"

            app_handle = self.get_ray_app_handle(model_key)

            request.create_response(
                status=ResponseModel.JobStatus.APPROVED,
                description="Your job was approved and is waiting to be run.",
                logger=self.logger,
            ).respond(self.sio, self.object_store)
            
            app_handle.remote(request)

        except Exception as exception:
            
            description = traceback.format_exc()

            request.create_response(
                status=ResponseModel.JobStatus.ERROR,
                description=f"{description}\n{str(exception)}",
                logger=self.logger,
            ).respond(self.sio, self.object_store)

    def get_ray_app_handle(self, name: str) -> DeploymentHandle:

        return serve.get_app_handle(name)


def app(args: BaseDeploymentArgs) -> Application:
    return RequestDeployment.bind(**args.model_dump())
