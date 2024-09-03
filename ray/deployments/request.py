import logging

from pydantic import BaseModel
from ray import serve
from ray.serve import Application
from ray.serve.handle import DeploymentHandle

try:
    from slugify import slugify
except:
    pass
from minio import Minio

from nnsight.schema.Request import RequestModel

from ...schema.Response import ResponseModel
from ...logging import load_logger
from ...metrics import NDIFGauge

@serve.deployment()
class RequestDeployment:
    def __init__(
        self,
        ray_dashboard_url: str,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
    ):

        self.ray_dashboard_url = ray_dashboard_url
        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.object_store = Minio(
            self.object_store_url,
            access_key=self.object_store_access_key,
            secret_key=self.object_store_secret_key,
            secure=False,
        )

        self.logger = load_logger(service_name="ray_request", logger_name="ray.serve")
        self.gauge = NDIFGauge(service='ray')

    async def __call__(self, request: RequestModel):

        try:

            model_key = f"Model:{slugify(request.model_key)}"

            app_handle = self.get_ray_app_handle(model_key)

            app_handle.remote(request)

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.APPROVED,
                description="Your job was approved and is waiting to be run.",
            ).log(self.logger).update_gauge(self.gauge, request).respond(self.api_url, self.object_store)

        except Exception as exception:

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
            ).log(self.logger).update_gauge(self.gauge, request).respond(self.api_url, self.object_store).backup_request(self.object_store, request)

    def get_ray_app_handle(self, name: str) -> DeploymentHandle:

        return serve.get_app_handle(name)


class RequestDeploymentArgs(BaseModel):

    ray_dashboard_url: str
    api_url: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str


def app(args: RequestDeploymentArgs) -> Application:
    return RequestDeployment.bind(**args.model_dump())
