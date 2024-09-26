from __future__ import annotations

import io
from typing import Any

from nnsight.contexts.backends.RemoteBackend import RemoteBackend, RemoteMixin
from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel
import requests
import socketio
from tqdm import tqdm

class LocustBackend(RemoteBackend):

    def __init__(self, client=None, api_key=None, base_url=None, ws_address=None, ip_addr=None, logger=None):
        self.client = client
        self.api_key = api_key
        self.base_url = base_url
        self.ws_address = ws_address
        self.ip_addr = ip_addr
        self.request_model = None
        self.logger = logger

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

    def ndif_request(self, name: str, track_stats: bool=True) -> Any:
        """
        Performs the remote backend request flow using Locust's HTTP client to capture network stats.

        Args:
            name (str): Name of the type of request made
            track_stats (bool): If true, makes REST calls through locust (which automatically tracks network stats)

        Returns:
            Any: The processed result from the remote backend.
        """
        # Serialize request model to JSON
        request_json = self.request_model.model_dump(exclude=["id","received"])
        self.job_id = self.request_model.id
        
        client = self.client if track_stats else requests
        self.kwargs = {
            'catch_response': True,
            'name':name,
        } if track_stats else {}
        # Define headers
        self.headers = {"ndif-api-key": self.api_key}
        if self.ip_addr:
            self.headers["X-Forwarded-For"] = self.ip_addr

        #Define URLs
        request_url = f"{self.base_url}/request"

        # Create a socketio connection to the server.
        with socketio.SimpleClient(
            logger=self.logger, reconnection_attempts=10
        ) as sio:
            # Connect
            sio.connect(
                self.ws_address,
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=10,
            )

            request_json['session_id'] = sio.sid

            # Submit the request
            with client.post(request_url, json=request_json, headers=self.headers, **self.kwargs) as response:
                if response.status_code != 200:
                    client.failure(f"Failed to submit request: {response.text}")
                    raise Exception(f"Request submission failed: {response.text}")
                else:
                    data = response.json()
                    self.job_id = data['id']
                    self.handle_response(client, data)
            
            while True:
                data = sio.receive()[1]
                if (
                    self.handle_response(client, data).status
                    == ResponseModel.JobStatus.COMPLETED
                ):
                    break

            
    def handle_response(self, client, data: Any):

        result_url = f"{self.base_url}/result/{self.job_id}"
        try: 
            response_model = ResponseModel(**data)
        except Exception as e:
            raise ValueError(f"Failed to parse response: {e}")
        
        if response_model.status == ResponseModel.JobStatus.ERROR:
            raise Exception(f"Server returned error: {response_model}")
        
        # If the job is blocking and completed, download the result
        if response_model.status == ResponseModel.JobStatus.COMPLETED:
            with client.get(result_url, stream=True, **self.kwargs) as result_response:
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

                    
                    # We skip loading a result model as the stats aren't relevant.


        return response_model