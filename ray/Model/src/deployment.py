from typing import Dict

import ray
from pydantic import BaseModel
from ray import serve

import os
class ModelDeploymentArgs(BaseModel):

    model_type: str | type
    model_id: str
    kwargs: Dict[str, str]


@serve.deployment(name="model")
class ModelDeployment:
    def __init__(self):
        pass
    def __call__(self):
        return os.getpid()


def app(args: Dict[str,str]):
    return ModelDeployment.bind()


