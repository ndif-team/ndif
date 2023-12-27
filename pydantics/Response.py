from __future__ import annotations

import logging
import pickle
from datetime import datetime
from enum import Enum
from typing import Any, Union

import requests
from pydantic import BaseModel, field_serializer


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
    output: Union[bytes, Any] = None
    saves: Union[bytes, Any] = None
    session_id: str = None
    blocking: bool = False

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    def log(self, logger: logging.Logger) -> ResponseModel:
        if self.status == ResponseModel.JobStatus.ERROR:
            logger.error(str(self))
        else:
            logger.info(str(self))

        return self

    def update_backend(self, client) -> ResponseModel:
        responses_collection = client["ndif_database"]["responses"]

        from bson.objectid import ObjectId

        responses_collection.replace_one(
            {"_id": ObjectId(self.id)}, self.model_dump(exclude_defaults=True, exclude_none=True), upsert=True
        )

        return self

    def blocking_response(self, api_url: str) -> ResponseModel:
        if self.blocking:
            requests.get(f"{api_url}/blocking_response/{self.id}")

        return self

    @field_serializer("output", "saves")
    def pickles(self, value, _info):
        return pickle.dumps(value)

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value
    
    @field_serializer("received")
    def sreceived(self, value, _info):
        return str(value)
