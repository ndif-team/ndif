from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Callable

from nnsight.contexts.backends.LocalBackend import LocalBackend, LocalMixin
from nnsight.contexts.backends.RemoteBackend import RemoteBackend, RemoteMixin
from nnsight.schema.Request import RequestModel


class LocustBackend(RemoteBackend):
    """Backend to execute a context object via a remote service.

    Context object must inherit from RemoteMixin and implement its methods.

    Attributes:

        url (str): Remote host url. Defaults to that set in CONFIG.API.HOST.
    """

    def request(self, obj: RemoteMixin):

        model_key = obj.remote_backend_get_model_key()

        self.obj = obj
        # Create request using pydantic to parse the object itself.
        return RequestModel(object=obj, model_key=model_key)

    def __call__(self, obj: RemoteMixin):

        self.handle_result = obj.remote_backend_handle_result_value

        request = self.request(obj)
        self.request_model = request

        obj.remote_backend_cleanup()

    '''
    def submit_request(self, request: "RequestModel") -> "ResponseModel":
        """Sends request to the remote endpoint and handles the response object.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        response = requests.post(
            f"{self.address}/request",
            json=request.model_dump(exclude=["id", "received"]),
            headers={"ndif-api-key": self.api_key},
        )

        if response.status_code == 200:

            return self.handle_response(response.json())

        else:

            raise Exception(response.reason)

    '''