"""Errors whose text reaches the caller, shared by both execution paths.

Kept here, rather than beside either implementation, because the trusted and
untrusted paths deserialize in different processes — the model actor and the
sandbox runner — and only *text* crosses the runner's socket. Sharing the
exception classes is what keeps the two paths saying the same thing; the runner
builds one and sends ``str(...)`` of it.

The runner cannot go further and share the *call* (``BackendRequestModel``'s
deserialize): importing ``ndif.common.schema.request`` connects Redis and loads
the InfluxDB client at import, which would happen in every pooled runner
process. So this module must stay import-cheap and free of side effects.

Each class builds its own message from the facts it is given, so there is one
place per failure that decides both what it *is* and how it *reads*.
"""


class RequestError(Exception):
    """A request that cannot be run, reported to the caller as a sentence.

    Never fatal: the request is unusable but the actor is healthy. And never a
    traceback — every frame at these failure points is server-side, so a
    traceback would leak module layout while telling the caller nothing. That is
    also what keeps the triage rule in
    ``docs/runbooks/trace-a-users-failed-job.md`` honest: a traceback means the
    caller's code failed, a sentence means we rejected the request.
    """


class PayloadError(RequestError):
    """The serialized payload could not be read at all.

    Distinct from a failure *inside* the user's block: that has user frames and
    should come back as their own traceback. This happens before their code
    exists.
    """

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(
            "Your request payload could not be read "
            f"({type(cause).__name__}: {cause}). It may be truncated or "
            "corrupted in transit, or compressed differently than the request "
            "declared. Re-sending it usually resolves this; if it persists, "
            "check that the client and server nnsight versions match."
        )


class ArchitectureMismatchError(RequestError):
    """The caller's model tree and the server's disagree.

    nnsight ships the traced block with each referenced module recorded as a
    persistent id, ``Module:<path>``, resolved server-side against the live
    model. A path the server's tree doesn't have means the two trees were built
    differently — most often a ``transformers`` version difference, since that
    decides the module layout for a given checkpoint.

    A sibling of :class:`PayloadError` rather than a subclass: the payload is
    perfectly readable, the *environment* is what differs.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            "The model architecture on this server doesn't match the one your "
            f"code was traced against: it has no module at '{path}'.\n\n"
            "This usually means the package versions differ between your "
            "machine and the server — most often `transformers`, which "
            "determines a checkpoint's module layout. Compare the two "
            "environments with:\n\n"
            "    from nnsight import ndif\n"
            "    print(ndif.compare())\n\n"
            "then align any mismatch it flags under nnsight / transformers / "
            "torch and re-run."
        )
