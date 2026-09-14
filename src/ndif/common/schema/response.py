from nnsight.schema.response import ResponseModel, Status


class BackendResponseModel(ResponseModel):
    """Server-side response.

    Subclasses the client's ``ResponseModel`` so the wire format the backend
    publishes is exactly what the client expects to parse.
    """
