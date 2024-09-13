from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from nnsight.contexts.backends.RemoteBackend import RemoteBackend, RemoteMixin
from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel, ResultModel
from locust import events
import time
import torch
import tqdm

class LocustBackend(RemoteBackend):

    def __init__(self, client=None, api_key=None, base_url=None):
        self.client = client
        self.api_key = api_key
        self.base_url = base_url
        self.request_model = None

    def request(self, obj: RemoteMixin):
        """Generates a RequestModel for the remote backend."""
        model_key = obj.remote_backend_get_model_key()
        self.obj = obj
        return RequestModel(object=obj, model_key=model_key)

    def __call__(self, obj: RemoteMixin):
        """Executes the remote backend request flow by creating and storing the request model."""
        self.handle_result = obj.remote_backend_handle_result_value
        self.request_model = self.request(obj)  # Store request model
        obj.remote_backend_cleanup()

    def ndif_request(self, locust_client, name: str) -> Any:
        """
        Performs the remote backend request flow using Locust's HTTP client to capture network stats.

        Args:
            request (RequestModel): The request model to send.

        Returns:
            Any: The processed result from the remote backend.
        """
        # Serialize request model to JSON
        request_json = self.request_model.model_dump(exclude=["id","received"])
        self.job_id = self.request_model.id

        # Define headers
        headers = {"ndif-api-key": self.api_key}

        #Define URLs
        request_url = f"{self.base_url}/request"
        response_url = f"{self.base_url}/response/{self.job_id}" if self.job_id else None
        result_url = f"{self.base_url}/result/{self.job_id}"

        # Submit the request
        with locust_client.client.post(request_url, json=request_json, headers=headers, catch_response=True, name=name) as response:
            if response.status_code != 200:
                response.failure(f"Failed to submit request: {response.text}")
                raise Exception(f"Request submission failed: {response.text}")
        
        # Parse the response
        try:
            response_data = response.json()
            response_model = ResponseModel(**response_data)
        except Exception as e:
            response.failure(f"Failed to parse response: {e}")
            raise

        if response_model.status == ResponseModel.JobStatus.ERROR:
            response.failure(f"Server returned error: {response_model}")
            raise Exception(str(response_model))
        
        # If the job is blocking and completed, download the result
        if response_model.status == ResponseModel.JobStatus.COMPLETED:
            with locust_client.client.get(result_url, stream=True, headers=headers, catch_response=True, name=name) as result_response:
                if result_response.status_code != 200:
                    result_response.failure(f"Failed to download result: {result_response.text}")
                    raise Exception(f"Result download failed: {result_response.text}")

                # Initialize BytesIO to store the downloaded bytes
                result_bytes = io.BytesIO()

                # Get total size for progress bar
                total_size = float(result_response.headers.get("Content-length", 0))
            
                # Use tqdm for progress tracking (optional in load tests)
                with tqdm(total=total_size, unit="B", unit_scale=True, desc="Downloading result") as progress_bar:
                    for chunk in result_response.iter_content(chunk_size=8192):
                        if chunk:
                            progress_bar.update(len(chunk))
                            result_bytes.write(chunk)

                    # Move cursor to the beginning
                    result_bytes.seek(0)

                    # Load the result using torch
                    try:
                        loaded_result = torch.load(result_bytes, map_location="cpu", weights_only=False)
                        result_model = ResultModel(**loaded_result)
                    except Exception as e:
                        raise Exception(f"Failed to load result: {e}")

                    # Handle the result
                    self.handle_result(result_model.value)

                    # Close the BytesIO stream
                    result_bytes.close()

        return response_model