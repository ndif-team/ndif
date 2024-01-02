from __future__ import annotations

import logging
import pickle
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Union

import gridfs
import requests
from bson.objectid import ObjectId
from pydantic import BaseModel, ConfigDict, field_serializer
from pymongo import MongoClient


class ResultModel(BaseModel):
    id: str
    output: Any = None
    saves: Dict[str, Any] = None

    @classmethod
    def load(cls, client: MongoClient, id: str, stream: bool = False) -> ResultModel:
        results_collection = gridfs.GridFS(
            client["ndif_database"], collection="results"
        )

        gridout = results_collection.find_one(ObjectId(id))

        if stream:
            return gridout

        result = ResultModel(**pickle.loads(gridout.read()))

        return result

    def save(self, client: MongoClient) -> ResultModel:
        results_collection = gridfs.GridFS(
            client["ndif_database"], collection="results"
        )

        id = ObjectId(self.id)

        results_collection.delete(id)

        results_collection.put(pickle.dumps(self.model_dump()), _id=id)

        return self


class ResponseModel(BaseModel):
    class JobStatus(Enum):
        RECEIVED = "RECEIVED"
        APPROVED = "APPROVED"
        SUBMITTED = "SUBMITTED"
        COMPLETED = "COMPLETED"
        ERROR = "ERROR"

    id: str
    status: JobStatus
    description: str

    received: datetime = None
    session_id: str = None
    blocking: bool = False

    result: Union[bytes, ResultModel] = None

    @classmethod
    def load(
        cls,
        client: MongoClient,
        id: str,
        result: bool = True,
    ) -> ResponseModel:
        responses_collection = client["ndif_database"]["responses"]

        response = ResponseModel(
            **responses_collection.find_one({"_id": ObjectId(id)}, {"_id": False})
        )

        if result and response.status == ResponseModel.JobStatus.COMPLETED:
            response.result = ResultModel.load(client, id, stream=False)

        return response

    def save(self, client: MongoClient) -> ResponseModel:
        responses_collection = client["ndif_database"]["responses"]

        if self.result is not None:
            self.result.save(client)

        responses_collection.replace_one(
            {"_id": ObjectId(self.id)},
            self.model_dump(
                exclude_defaults=True, exclude_none=True, exclude=["result"]
            ),
            upsert=True,
        )

        return self

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    def log(self, logger: logging.Logger) -> ResponseModel:
        if self.status == ResponseModel.JobStatus.ERROR:
            logger.error(str(self))
        else:
            logger.info(str(self))

        return self

    def blocking_response(self, api_url: str) -> ResponseModel:
        if self.blocking:
            requests.get(f"{api_url}/blocking_response/{self.id}")

        return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value

    @field_serializer("received")
    def sreceived(self, value, _info):
        return str(value)
