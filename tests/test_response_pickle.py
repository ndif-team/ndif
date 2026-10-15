"""The COMPLETED envelope pickles a result blob at its own size.

A result small enough to ride inline is a ``bytes`` blob in ``data``, and the
envelope around it is ``torch.save`` bytes the client reads with
``ResponseModel.unpickle``. At ``torch.save``'s default protocol 2 a ``bytes``
value is written through a latin-1 ``str``, inflating it about 1.5x.
"""

from __future__ import annotations

import os

from nnsight.schema.response import ResponseModel, Status

from ndif.common.schema.response import BackendResponseModel


def test_client_unpickles_the_envelope_byte_for_byte():
    blob = os.urandom(1 << 20)
    response = BackendResponseModel(
        id="req-1", status=Status.COMPLETED, description="done", data=blob
    )

    wire = response.pickle()
    back = ResponseModel.unpickle(wire)

    assert back.id == "req-1"
    assert back.status == Status.COMPLETED
    assert back.data == blob
    assert len(wire) < len(blob) + 4096
