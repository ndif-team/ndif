"""Errors whose text reaches the caller, shared by both execution paths.

Kept here, rather than beside either implementation, because the trusted and
untrusted paths deserialize in different processes — the model actor and the
sandbox runner — and only *text* crosses the runner's socket. A shared message
builder is what keeps the two paths saying the same thing.

This module must stay import-cheap and side-effect free: the sandbox runner
imports it, and the runner deliberately avoids pulling in ray or the telemetry
providers (which connect at import).
"""


class PayloadError(Exception):
    """The request's serialized payload could not be read.

    Distinct from a failure *inside* the user's block. Both are the caller's
    problem rather than a broken actor, but they want opposite presentation: a
    block that raises should come back as the user's own traceback, whereas a
    payload that won't deserialize has no user frames to show — the failure
    happens before their code exists. Reporting it as a traceback exposed
    NDIF's own module layout and told the caller nothing they could act on.
    """


def payload_error_message(exception: BaseException) -> str:
    """The sentence a caller gets when their payload can't be deserialized.

    Names the underlying exception class (``ZstdError``, ``UnpicklingError``,
    ...) because that is the one genuinely diagnostic token, while leaving out
    the traceback, which is entirely server-side frames at this point.
    """
    return (
        "Your request payload could not be read "
        f"({type(exception).__name__}: {exception}). It may be truncated or "
        "corrupted in transit, or compressed differently than the request "
        "declared. Re-sending it usually resolves this; if it persists, check "
        "that the client and server nnsight versions match."
    )
