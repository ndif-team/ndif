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
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.sio.connect(
            self.api_url,
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            wait_timeout=10,
        )

    async def __call__(self, request: BackendRequestModel):

        try:

            model_key = f"Model:{slugify(request.model_key)}"

            app_handle = self.get_ray_app_handle(model_key)

            app_handle.remote(request)

            request.create_response(
                status=ResponseModel.JobStatus.APPROVED,
                description="Your job was approved and is waiting to be run.",
                logger=self.logger,
                gauge=self.gauge,
            ).respond(self.sio, self.object_store)

        except Exception as exception:

            request.create_response(
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
                logger=self.logger,
                gauge=self.gauge,
            ).respond(self.sio, self.object_store)

    def get_ray_app_handle(self, name: str) -> DeploymentHandle:

        return serve.get_app_handle(name)


def app(args: BaseDeploymentArgs) -> Application:
    return RequestDeployment.bind(**args.model_dump())
