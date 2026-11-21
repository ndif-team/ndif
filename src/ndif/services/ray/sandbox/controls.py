"""What the host does for each control request a worker parks on.

A worker that needs something only the host can do parks on a
``messages.Control`` request in place of a model location. The proxy for that
worker looks the request's type up in `HANDLERS`, calls the handler, and sends
what it returns back in a ``Reply`` (``MediatorProxy.settle_control``).

So a new kind of request is three things: its class in ``messages.py``, a
handler here, and the park in the runner.

A handler takes ``(driver, proxy, request)``: the ``SandboxDriver`` running the
model, the ``MediatorProxy`` of the worker that asked, and the request.
"""

from __future__ import annotations

import torch

from nnsight.intervention.cache import Cache

from .messages import CacheHit, Call, Observe, Source, SourceRefused, Writeback

#: The handler for each type of control request.
HANDLERS: dict = {}


def control(kind):
    """Register the function below as the handler for requests of type ``kind``."""

    def register(handler):
        HANDLERS[kind] = handler
        return handler

    return register


# ------------------------------------------------------------------ .source


def envoy_at(model, path: str):
    """The envoy at a dotted ``path`` (``model.transformer.h.0.mlp``); numeric
    parts index a ModuleList child."""
    envoy = model
    for part in path.split(".")[1:]:  # drop the leading root ("model")
        envoy = envoy[int(part)] if part.isdigit() else getattr(envoy, part)
    return envoy


def describe(compiled) -> dict:
    """The picklable description of a ``Compiled``: the operation names, each
    one's line, and the forward's source text — what a runner builds an
    ``IPCSource`` from. Everything else on ``Compiled`` stays host-side; its
    ``code`` is a code object and does not pickle."""
    return {
        "names": list(compiled.names),
        "lines": dict(compiled.lines),
        "source": compiled.source,
    }


@control(Source)
def source(driver, proxy, request):
    """Source-instrument the module at ``request.path`` (permanent, idempotent)
    and describe it for the runner — the runner's IPCSource asks for this
    because instrumenting its own copy would instrument nothing: the forward
    runs here. ``None`` when the ``forward`` can't be sourced, so the runner
    reports it like the local path would.

    Returns the :func:`describe` of the ``Compiled`` — what the runner's
    ``IPCSource`` builds its ``SourceEnvoy``s from.

    ``path`` can also be a **nested op path** (``...attn.source.
    attention_interface_1``): recursive ``.source``, where the drilled-into
    callable is a live value the forward resolves at run time, so there is
    nothing to describe yet. Arming ``interleaver.sourced[path] = None`` is
    what makes ``run_op`` serve that callable at ``{path}.fn`` when the op
    fires (the base ``SourceEnvoy.source`` marks the same ``None``; see
    nnsight ``intervention/source.py``) — a serve
    :func:`serve_recursive_source` intercepts, so the description crosses
    then instead of now. The reply here is ``None`` — and ``envoy_at``
    could not walk a ``.source`` segment anyway. The enclosing module's
    source is (re)installed first so the op fires at all; installing it is
    idempotent, and it cannot fail here — reaching a ``SourceEnvoy``
    already installed it.
    """
    from nnsight.intervention.source import SourceNotAvailable, install_source

    path = request.path
    module_path, _, _ = path.partition(".source.")
    try:
        compiled = install_source(envoy_at(driver.model, module_path))
    except SourceNotAvailable:
        return None
    if module_path != path:
        driver.model.interleaver.sourced.setdefault(path, None)
        driver.armed.add(path)
        return None
    return describe(compiled)


def serve_recursive_source(interleaver, path: str, fn):
    """The value to serve at an armed op ``path``'s ``{path}.fn`` park: a
    *description* of the live callable ``fn``, never the callable itself
    (#312) — the host half of recursive ``.source``.

    Called from ``MediatorProxy.switch`` at the one moment the callable
    exists on this side: ``run_op`` reached the drilled-into op and is
    serving it over ``{path}.fn`` to the runner's parked worker. The
    classification is the base ``SourceEnvoy.source``'s, run here against
    the live value: a submodule target or an assignment serves
    ``SourceRefused(message)`` with the base wording (kept in this one
    place), a callable with no Python source serves ``instrument``'s own
    ``SourceNotAvailable`` text — and nothing is built, so the forward runs
    the original callable while the runner raises from the user's frame.
    Otherwise the instrumented copy lands in ``interleaver.sourced[path]``
    before the handle returns, which is what makes ``run_op`` pick it up —
    its inner operations then fire under ``{path}.source.*``, ordinary
    locations the proxies serve — and the reply is the same shape the
    one-level reply to a `Source` request uses, from which the runner builds another
    ``IPCSource``. A second worker drilling the same path at the same fire
    is served the already-built copy's description. A ``.fn`` park that was
    never armed is some other read whose path happens to end in ``.fn``:
    served verbatim.
    """
    from nnsight.intervention.source import (
        SourceNotAvailable,
        bind,
        instrument,
        make_op,
    )

    if path not in interleaver.sourced:
        return fn  # not an armed recursive-source request: serve verbatim
    if interleaver.sourced[path] is None:
        name = path.rsplit(".", 1)[-1]
        if isinstance(fn, torch.nn.Module):
            return SourceRefused(
                f"{name!r} calls a submodule; call `.source` on that "
                f"submodule directly instead of drilling into the call"
            )
        if fn is bind:
            return SourceRefused(
                f"{name!r} is an assignment, not a call; there is no "
                f"function to drill into"
            )
        try:
            interleaver.sourced[path] = instrument(
                fn,
                make_op(
                    lambda: (interleaver, path)
                    if interleaver.interleaving
                    else (None, None)
                ),
            )
        except SourceNotAvailable as error:  # a builtin/C target
            return SourceRefused(str(error))
    return describe(interleaver.sourced[path][1])


# ------------------------------------------------------------------ an ad-hoc module call


@control(Call)
def call(driver, proxy, request):
    """Run the module at ``request.path`` ad hoc and return its output — the host side
    of the runner's ``IPCEnvoy.__call__`` (an ad-hoc module call, e.g. the
    logit lens, where the module lives here).

    Defers to ``Envoy.__call__`` instead of picking a callable itself,
    because the base call owns the occurrence semantics: ``hook=False`` runs
    the module the ordinary way with the trace stood down for the duration,
    so nothing is served and no occurrence is spent for the module *or
    anything under it* — a hand-rolled ``_module.forward(...)`` with
    ``interleaving`` still on let the controllers count the ad-hoc visit and
    steal the real occurrence, so a later read of the module's real
    ``.output`` raised ``OutOfOrderError`` (#295) — and the module's own
    runtime wrappers still fire (transformers tensor parallelism keeps its
    collectives there; a bare ``forward`` returns one rank's slice).
    ``hook=True`` lets the trace watch the call, for a module attached to
    the tree rather than one the forward already runs. The host's ``Envoy``
    is the real, unpatched class (the IPC patches load only in the runner),
    so this is the trusted path's behavior by construction."""
    return envoy_at(driver.model, request.path)(
        *request.args, hook=request.hook, **request.kwargs
    )


# ------------------------------------------------------------------ tracer.cache()


class ShippingCache(Cache):
    """A ``tracer.cache()`` observed on the host on the runner's behalf.

    Reuses :class:`~nnsight.intervention.cache.Cache`'s filtering and transform (via
    ``observe`` / ``_record``), but ships each kept value to the runner as a
    ``CacheHit`` instead of storing it — the runner's real Cache does the storing.
    Attached to a proxy's ``caches`` so ``Interleaver.handle`` feeds it every
    location the forward reaches, narrowed to that worker's rows.
    """

    def __init__(self, cache_id, link, config, model=None) -> None:
        targets = config["targets"]
        super().__init__(
            # The host's model, not None. `Cache.subscriptions` reads every
            # module's path off it when the cache names no explicit targets —
            # which is what a bare `tracer.cache()` asks for — so without it the
            # run dies in `Interleaver.__enter__` with 'NoneType' object has no
            # attribute 'modules'. The runner cannot supply one: its copy is on
            # meta and the forward runs here.
            model,
            modules=list(targets) if targets is not None else None,
            device=config["device"],
            dtype=config["dtype"],
            detach=config["detach"],
            include_output=config["include_output"],
            include_inputs=config["include_inputs"],
        )
        self._cache_id = cache_id
        self._link = link

    def _record(self, path, key, value):
        self._link.send(CacheHit(self._cache_id, path, key, value))


@control(Observe)
def observe(driver, proxy, request):
    """Attach a `ShippingCache` to the worker's proxy, so that the worker's
    ``tracer.cache()`` in the runner fills from the forward that runs here."""
    proxy.caches.append(
        ShippingCache(request.cache, proxy.link, request.config, driver.model)
    )


# ------------------------------------------------------------------ the write-back of a read


def write_back(served, edited) -> None:
    """Copy a block's in-place edits of a value it read into the host's own value.

    ``served`` is what the host handed over and ``edited`` is the runner's copy of
    it, sent back after the block moved on. Each tensor in ``served`` takes the
    values of its counterpart, in place, if they differ. Nothing is replaced, so
    the forward keeps its own objects, as it does on the trusted path: its other
    references to an edited tensor see the edit, and an object a module updates,
    such as the KV cache in a layer's inputs, stays the one the forward holds.

    A tensor the block left alone is not written, so one that cannot be written in
    place (an expanded attention mask) can still be read.
    """
    if isinstance(served, torch.Tensor):
        if isinstance(edited, torch.Tensor) and served.shape == edited.shape:
            edited = edited.to(served.device)
            if not torch.equal(served, edited):
                served.copy_(edited)
    elif isinstance(served, (list, tuple)) and isinstance(edited, (list, tuple)):
        for mine, theirs in zip(served, edited):
            write_back(mine, theirs)
    elif isinstance(served, dict) and isinstance(edited, dict):
        for key in served.keys() & edited.keys():
            write_back(served[key], edited[key])


@control(Writeback)
def writeback(driver, proxy, request):
    """Write the worker's copy of the value it just read into the original,
    which is what the proxy served (``MediatorProxy.switch``)."""
    write_back(proxy.served, request.value)
    proxy.served = None
