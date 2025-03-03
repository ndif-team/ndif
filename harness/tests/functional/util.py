from typing import TYPE_CHECKING, Dict, Any, Optional, Callable

import requests
import yaml
import uuid
import pytest
import itertools
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from nnsight.schema.response import ResponseModel
from nnsight.intervention.backends import RemoteBackend

if TYPE_CHECKING:
    from nnsight.modeling.mixins import RemoteableMixin

#### Parsing CONFIG.yaml ####

with open("CONFIG.yaml", "r") as file:
    config = yaml.safe_load(file)

CONFIG = dict()
CONFIG["tests"] = dict()
CONFIG["models"] = set()

for test_name, test in config.items():
    tests = list()
    for test_case in test:
        # model
        model_list = test_case["model"] if isinstance(test_case["model"], list) else [test_case["model"]]
        for model in model_list:
            CONFIG["models"].add(model)
        # params [optional]
        param_names = ", ".join(list(test_case["params"].keys())) if "params" in test_case.keys() else ""
        params = [value if isinstance(value, list) else [value] for value in test_case["params"].values()] if "params" in test_case.keys() else list()
            
        params = [model_list] + params
        params = list(itertools.product(*params))
        params = [(param[0], test_case["num_requests"]) + param[1:] for param in params]
        tests += params

    CONFIG["tests"][test_name] = ("model, num_requests, " + param_names, tests)

print(CONFIG)

##############################

INFLUXDB_CLIENT = InfluxDBClient(
                    url="http://localhost:8086",
                    token="njkldhsfbdsfkl2o32==",
                ).write_api(write_options=SYNCHRONOUS)

RUN_ID = uuid.uuid4()

class BaseTest:

    @classmethod
    def name(cls) -> str:
        """Test name convention."""
        return cls.__name__[4:]

    def run_test(self, test: Callable, model: "RemoteableMixin", num_requests: int, **tags) -> bool:
        """ Runs a test function and reports to the result storage database.

        Args:
            - test (Callable): nnsight request test function to run.
            - model (nnsight.modeling.mixins.RemotableMixin): nnsight model.
            - num_requests (int): number of times to run the test function.
            - tags (Dict[str: Any]): 

        Returns:
            - passed (bool): whether all the tests successfully passed.
        """

        test_id = uuid.uuid4()
        def log_result(result: bool, request_id: str) -> None:
            """ Constructs the logging call of the test result.
            
            Args:
                - result (int): test result, 1 is passed and 0 is failed.
                - request_id (str): request id, single iteration of the test.
            """

            self.log_test(
                result,
                run_id=RUN_ID,
                test_id=test_id, 
                request_id=request_id, 
                model_key=model.to_model_key(),
                model_size=get_model_size(model),
                **tags
            )

        passed = True
        for ii in range(num_requests):
            backend = BackendTest(model.to_model_key())
            try:
                response = test(backend) # running test
                log_result(1, backend.job_id) # logging passed
            except Exception as e:
                print(e)
                log_result(0, backend.job_id) # logging failed
                passed = False

        return passed
    
    def log_test(self, result: int, **tags) -> None:
        """
        
        Args: 
            - result (int): test result, 1 is passed and 0 is failed.
            - tags (Dict[str, Any]): test arguments to be included in the result report.

        """
        
        point = Point(self.__class__.name()).field("result", result)
        for tag, value in tags.items():
            point.tag(tag, value)

        INFLUXDB_CLIENT.write(bucket="TEST", org="NDIF", record=point)


class BackendTest(RemoteBackend):
    
    def submit_request(
        self, data: bytes, headers: Dict[str, Any]
    ) -> Optional[ResponseModel]:
        """Sends request to the remote endpoint and handles the response object.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        headers["Content-Type"] = "application/octet-stream"

        response = requests.post(
            f"{self.address}/request",
            data=data,
            headers=headers,
        )

        self.job_id = response.json()["id"]

        if response.status_code == 200:

            response = ResponseModel(**response.json())

            self.handle_response(response)

            return response

        else:
            msg = response.reason
            raise ConnectionError(msg)
        
def get_model_size(meta_model) -> int:
    """ Calculates the total size of the model parameters in bytes.

    Args:
        - meta_model (nnsight.modeling.mixins.RemoteableMixin)
    
    Returns:
        int: model size in bytes.
    """

    param_size = 0
    for param in meta_model.parameters():
        param_size += param.nelement() * param.element_size()
    buffer_size = 0
    for buffer in meta_model.buffers():
        buffer_size += buffer.nelement() * buffer.element_size()

    model_size_bytes = param_size + buffer_size

    return model_size_bytes

def skip_test(cls):
    """ Decorates a class with a pytest skip mark if the test case it represents is not specified in the configuration.

    Args:
        - cls (Class): Test class.

    Returns:
        Class: Test class.
    """

    if cls.name() not in CONFIG["tests"].keys():
        cls = pytest.mark.skip(reason="No configuration.")(cls)
    return cls
