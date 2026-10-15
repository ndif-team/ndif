import io

import torch
from nnsight.schema.response import ResponseModel, Status


class BackendResponseModel(ResponseModel):
    """Server-side response.

    Subclasses the client's ``ResponseModel`` so the wire format the backend
    publishes is exactly what the client expects to parse.
    """

    def pickle(self) -> bytes:
        """``ResponseModel.pickle`` at pickle protocol 4.

        ``torch.save`` defaults to protocol 2, which has no bytes opcode and
        writes the result blob in ``data`` through a latin-1 ``str``: 1.5x the
        bytes, and about 10x slower to save and load. The client reads it back
        with the same ``torch.load``, which accepts any protocol.
        """
        with io.BytesIO() as file:
            torch.save(self.model_dump(exclude_unset=True), file, pickle_protocol=4)
            return file.getvalue()
