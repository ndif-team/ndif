import io
import pickle
from typing import Any

from ..pydantics import RequestModel
from ..celery import customconfig


class Unpickler(pickle.Unpickler):
    def find_class(self, __module_name: str, __global_name: str) -> Any:
        disallowed = True

        fullname = f"{__module_name}.{__global_name}"

        for allowed_module in customconfig.allowed_modules:
            if fullname.startswith(allowed_module):
                disallowed = False

                break

        if disallowed:
            raise pickle.UnpicklingError(f"Type {fullname} not allowed.")

        return super().find_class(__module_name, __global_name)


def validate_request(request: RequestModel):
    request.intervention_graph = Unpickler(
        io.BytesIO(request.intervention_graph)
    ).load()

    request.batched_input = Unpickler(io.BytesIO(request.batched_input)).load()
