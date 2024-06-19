import logging
from ray import serve
from pydantic import BaseModel
from pymongo import MongoClient

from ray.serve import Application
from ray.serve.handle import DeploymentHandle
from nnsight.pydantics.Request import RequestModel

from ..pydantics.Response import ResponseModel


class RequestDeploymentArgs(BaseModel):

    api_url: str
    database_url: str


@serve.deployment()
class RequestDeployment:
    def __init__(self, api_url: str, database_url: str):

        self.api_url = api_url
        self.database_url = database_url

        self.db_connection = MongoClient(self.database_url)

        self.logger = logging.getLogger(__name__)

    async def __call__(self, request: RequestModel):

        try:
            
            app_handle = self.get_ray_app_handle(request.model_key)
            
            app_handle.remote(request)

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.COMPLETED,
                description="Your job has been completed."
            ).log(self.logger).save(self.db_connection).blocking_response(self.api_url)

        except Exception as exception:
            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
            ).log(self.logger).save(self.db_connection).blocking_response(self.api_url)
            
            
    def get_ray_app_handle(self, name:str) -> DeploymentHandle:
        
        return serve.get_app_handle(name)


def app(args: RequestDeploymentArgs) -> Application:
    return RequestDeployment.bind(**args.model_dump())
