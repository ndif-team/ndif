"""Split autograd across the host and runner processes.

The model's forward runs on the host and the user's block runs in the runner,
so one computation graph spans both processes. Each tensor that crosses the
socket becomes an edge linking the two halves, the same way
``torch.distributed.autograd`` links RPC calls:

* **Host -> runner** (a read, a module call's output, or a served gradient).
  `HostGraph.serve` assigns each outgoing tensor an integer *crossing* id and
  stores the host tensor under it. `RunnerGraph.receive` rebuilds the value and,
  when the host tensor requires grad, creates a leaf that requires grad and
  hands the block a clone of it. A block's ``requires_grad_()`` on a read tensor
  is mirrored onto the host tensor before the forward continues.
* **Runner -> host** (a swap, an in-place edit, or a module call's arguments).
  `RunnerGraph.outgoing` detaches each tensor that requires grad and records the
  crossings its graph reaches. `HostGraph.incoming` wraps it in a `FromRunner`
  node whose inputs are those crossings' host tensors. When the host's backward
  reaches `FromRunner.backward`, it sends the gradient to the runner
  (`GradRequest`); the runner backpropagates it through its own graph and
  replies with the gradients for those crossings (`GradReply`).
* **Backward started in the runner.** The runner backpropagates the loss through
  its own graph to the crossings and parks the worker on a `Backward` control
  request carrying those gradients. The host runs ``torch.autograd.backward``
  from the crossings' host tensors. Any ``with loss.backward():`` backward block
  runs in the runner as another worker, driven by a `MediatorProxy` on a
  host-side backward interleaver; its ``t.grad`` maps ``t`` to its crossing,
  asks the host to hook that tensor (`WatchGrad`), and parks on
  ``grad.{crossing}``.

Leaf gradient totals live only in the runner, where the block reads and resets
``t.grad``. Each pass through the runner's graph (`RunnerGraph.through`) sets
existing totals aside and collects only its own pass's gradients; the host
returns only the gradients its leaves received in that backward and clears them
(`HostGraph.take`), and the runner adds them into its leaves
(`RunnerGraph.restore`).

Limits: ``torch.autograd.grad``, ``create_graph``, ``inputs=``, a backward from
several tensors, and a backward inside a backward block are refused in the
runner. Inside a backward block, ``.grad`` is served only for values read from
the model. A backward whose graph reaches a value read in an earlier trace is
refused because that trace's host graph has been released. Both ends keep their
graphs until the trace ends.
"""

from __future__ import annotations

import torch
from nnsight.util import apply

from ....common.errors import WeightEditError
from .messages import GradReply, GradRequest

# Original torch entry points captured at import time. ``adapters.install``
# replaces ``torch.autograd.backward`` in the runner, and
# ``adapters.run_backward`` replaces ``Tensor.grad`` during a backward block.
autograd_backward = torch.autograd.backward
GRAD = torch.Tensor.grad


# Traversal depth for ``nnsight.util.apply``: containers only on host -> runner,
# and one object level on runner -> host so `Call` arguments are reached. Both
# ends of each direction use the same depth so tensor numbering matches.
SERVE_DEPTH = 0
SEND_DEPTH = 1


# ---------------------------------------------------------------------- runner


class RunnerGraph:
    """Runner half of the autograd graph: received crossings and sent tensors."""

    def __init__(self, link) -> None:
        self.link = link
        # crossing -> tensor handed to the block
        self.held: "dict[int, torch.Tensor]" = {}
        # crossing -> leaf autograd accumulates into (the held tensor or the
        # leaf it was cloned from)
        self.leaf: "dict[int, torch.Tensor]" = {}
        # id(held or leaf) -> crossing, for `.grad` inside a backward block
        self.by_id: "dict[int, int]" = {}
        # crossing -> (requires_grad, grad_fn) when handed to the block
        self.handed: "dict[int, tuple]" = {}
        # out id -> (runner tensor, crossings its graph reaches)
        self.sent: "dict[int, tuple]" = {}
        self.next_out = 0
        # True while a backward started by this graph is running on the host.
        self.backward = False

    # -- host -> runner

    def receive(self, value, infos):
        """Prepare ``value`` for the block using host crossing metadata.

        Each tensor whose host tensor requires grad becomes a grad-requiring
        leaf and is handed to the block as a clone, so in-place edits work as
        they do on the host."""
        if not infos:
            return value
        infos = iter(infos)

        def mark(tensor):
            crossing, requires_grad = next(infos)
            held = tensor
            if requires_grad:
                leaf = tensor.requires_grad_(True)
                leaf.ndif_crossing = True
                held = leaf.clone()
                self.leaf[crossing] = leaf
                self.by_id[id(leaf)] = crossing
            self.held[crossing] = held
            self.by_id[id(held)] = crossing
            self.handed[crossing] = (requires_grad, held.grad_fn)
            return held

        return apply(value, mark, torch.Tensor, SERVE_DEPTH)

    def adopt(self) -> list:
        """Register any read tensor the block enabled ``requires_grad_()`` on as
        a leaf of this graph, and return those crossings.

        Called when the worker parks (`settle`) and when a backward starts
        (`adapters.run_backward`), which can happen before the next park."""
        adopted = []
        for crossing, (requires_grad, _) in self.handed.items():
            held = self.held[crossing]
            if not requires_grad and held.requires_grad and held.is_leaf:
                held.ndif_crossing = True
                self.leaf[crossing] = held
                adopted.append(crossing)
        return adopted

    def settle(self) -> "tuple[list, bool]":
        """Finalize crossings handed to the block since the last park.

        Returns ``(mirror, linked)``: crossings where the block enabled
        ``requires_grad_()`` (to mirror on the host), and whether the block
        edited a crossing in place with a grad-requiring value. Drops crossings
        that do not require grad, since no backward can flow into them."""
        mirror = self.adopt()
        linked = any(
            crossing not in mirror and self.held[crossing].grad_fn is not grad_fn
            for crossing, (_, grad_fn) in self.handed.items()
        )
        self.handed = {}
        for crossing in list(self.held):
            if crossing not in self.leaf and not self.held[crossing].requires_grad:
                held = self.held.pop(crossing)
                self.by_id.pop(id(held), None)
        return mirror, linked

    def crossing_of(self, tensor) -> "int | None":
        return self.by_id.get(id(tensor))

    # -- runner -> host

    def reaches(self, tensor) -> list:
        """Return sorted crossings whose leaves ``tensor``'s graph reaches."""
        leaves = {id(leaf): crossing for crossing, leaf in self.leaf.items()}
        if tensor.grad_fn is None:
            return [leaves[id(tensor)]] if id(tensor) in leaves else []
        found, seen, stack = [], set(), [tensor.grad_fn]
        while stack:
            node = stack.pop()
            if node is None or node in seen:
                continue
            seen.add(node)
            variable = getattr(node, "variable", None)  # AccumulateGrad
            if variable is not None and id(variable) in leaves:
                found.append(leaves[id(variable)])
            stack.extend(next_node for next_node, _ in node.next_functions)
        return sorted(set(found))

    def outgoing(self, value):
        """Detach every grad-requiring tensor in ``value`` and record its
        ``(out_id, reached_crossings)`` metadata for the host."""
        infos, any_grad = [], False

        def strip(tensor):
            nonlocal any_grad
            if not tensor.requires_grad:
                infos.append(None)
                return tensor
            any_grad = True
            out = self.next_out
            self.next_out += 1
            reaches = self.reaches(tensor)
            self.sent[out] = (tensor, reaches)
            infos.append((out, reaches))
            return tensor.detach()

        value = apply(value, strip, torch.Tensor, SEND_DEPTH)
        return value, (infos if any_grad else None)

    # -- backward

    def through(self, tensor, gradient) -> dict:
        """Backpropagate ``tensor`` through the runner's graph and return the
        gradients that reached crossing leaves in this pass, keyed by crossing.

        Existing crossing-leaf totals are saved and cleared before the pass and
        restored afterward, so only this pass's gradients are returned; the
        block's own leaves accumulate normally. Uses ``retain_graph=True``
        because a later `GradRequest` can traverse the same nodes (for example,
        a tensor swapped into the model and also used in the loss)."""
        totals = {c: GRAD.__get__(leaf) for c, leaf in self.leaf.items()}
        for leaf in self.leaf.values():
            GRAD.__set__(leaf, None)
        try:
            gradients = None if gradient is None else [gradient]
            autograd_backward([tensor], gradients, retain_graph=True)
            return {
                c: GRAD.__get__(leaf)
                for c, leaf in self.leaf.items()
                if GRAD.__get__(leaf) is not None
            }
        finally:
            for crossing, total in totals.items():
                GRAD.__set__(self.leaf[crossing], total)

    def answer(self, request: GradRequest) -> None:
        """Handle a `GradRequest` by backpropagating through the recorded sent
        tensor and replying with `GradReply`."""
        tensor, reaches = self.sent[request.out]
        reached = self.through(tensor, request.grad)
        self.link.send(GradReply([reached.get(c) for c in reaches]))

    def local(self, tensor, gradient) -> "tuple[list, list]":
        """Run the runner's half of a block-initiated backward.

        Returns ``(crossings, grads)`` for the host to use as backward roots."""
        reached = self.through(tensor, gradient)
        return list(reached), list(reached.values())

    def restore(self, grads: dict) -> None:
        """Accumulate host leaf gradients from a backward into the matching
        runner leaves so ``t.grad`` reflects the total after backward."""
        for crossing, grad in grads.items():
            leaf = self.leaf[crossing]
            total = GRAD.__get__(leaf)
            if total is None:
                GRAD.__set__(leaf, grad)
            else:
                total.add_(grad)



def earlier(tensor, current=frozenset()) -> bool:
    """Return True if ``tensor``'s graph reaches a crossing leaf not in
    ``current`` (a value read in an earlier trace whose host graph has been
    released)."""

    def stale(variable) -> bool:
        return getattr(variable, "ndif_crossing", False) and id(variable) not in current

    if tensor.grad_fn is None:
        return stale(tensor)
    seen, stack = set(), [tensor.grad_fn]
    while stack:
        node = stack.pop()
        if node is None or node in seen:
            continue
        seen.add(node)
        if stale(getattr(node, "variable", None)):  # AccumulateGrad
            return True
        stack.extend(next_node for next_node, _ in node.next_functions)
    return False


# ---------------------------------------------------------------------- host


class FromRunner(torch.autograd.Function):
    """Host-side autograd node for a tensor sent from the runner.

    Its inputs are the host tensors for the crossings reached by that runner
    tensor; `backward` sends the incoming gradient back over the socket."""

    @staticmethod
    def forward(ctx, value, graph, out, *inputs):
        ctx.graph = graph
        ctx.out = out
        ctx.count = len(inputs)
        return value.view_as(value)

    @staticmethod
    def backward(ctx, grad):
        grads = ctx.graph.request(ctx.out, grad)
        if ctx.count > len(grads):  # the anchor
            grads = [None] + list(grads)
        return (None, None, None, *grads)


class HostGraph:
    """Host half of the autograd graph: tensors served under each crossing."""

    def __init__(self) -> None:
        self.link = None
        self.served: "dict[int, torch.Tensor]" = {}
        self.next_crossing = 0
        # Grad-requiring dummy input attached when a runner tensor reaches no
        # crossing (for example, one built only from the block's own
        # parameters), so `FromRunner` stays in the host autograd graph.
        self.anchor = None
        # (interleaver, hooked crossings, hook handles) while `Backward` runs.
        self.watching = None

    def serve(self, value) -> "list | None":
        infos = []

        def number(tensor):
            crossing = self.next_crossing
            self.next_crossing += 1
            self.served[crossing] = tensor
            infos.append((crossing, tensor.requires_grad))

        apply(value, number, torch.Tensor, SERVE_DEPTH)
        return infos or None

    def mirror(self, crossings, kept=frozenset()) -> None:
        """Apply ``requires_grad_()`` to host tensors whose runner copies had it
        enabled.

        Raises `WeightEditError` if a tensor's storage belongs to the model
        (``controls.kept_by``): the flag would stay on a weight for every later
        request."""
        for crossing in crossings or ():
            tensor = self.served[crossing]
            if tensor.untyped_storage().data_ptr() in kept:
                raise WeightEditError()
            tensor.requires_grad_(True)

    def prune(self) -> None:
        self.served = {
            crossing: tensor
            for crossing, tensor in self.served.items()
            if tensor.requires_grad
        }

    def incoming(self, value, infos):
        if not infos:
            return value
        infos = iter(infos)

        def link(tensor):
            info = next(infos)
            if info is None:
                return tensor
            out, reaches = info
            inputs = [self.served[crossing] for crossing in reaches]
            if not any(tensor.requires_grad for tensor in inputs):
                if self.anchor is None:
                    self.anchor = torch.zeros((), requires_grad=True)
                inputs = [self.anchor, *inputs]
            return FromRunner.apply(tensor, self, out, *inputs)

        return apply(value, link, torch.Tensor, SEND_DEPTH)

    def request(self, out, grad) -> list:
        reply = self.link.request(GradRequest(out, grad), GradReply)
        return reply.grads

    def take(self) -> dict:
        """Return and clear gradients accumulated on served host leaves during
        this backward, keyed by crossing. Totals are accumulated in the runner
        (`RunnerGraph.restore`)."""
        grads = {}
        for crossing, tensor in self.served.items():
            if tensor.is_leaf and tensor.grad is not None:
                grads[crossing] = tensor.grad
                tensor.grad = None
        return grads

    def release(self) -> None:
        """Clear references held for this trace.

        A `FromRunner` context holds a reference back to this graph, forming a
        cycle through autograd's C++ nodes that Python's GC cannot collect."""
        self.served = {}
        self.link = None


def watch(interleaver, tensor, crossing, hooks: list) -> None:
    """Register a gradient hook on ``tensor`` that routes through
    ``interleaver`` at ``grad.{crossing}``.

    Mirrors nnsight's ``backward._grad_property`` ``wrap``, keyed by crossing
    id. A batched read is a view outside the loss graph, so the hook attaches to
    its base tensor and slices out this view's elements."""
    location = f"grad.{crossing}"
    if getattr(tensor, "_nnsight_batch", False) and tensor._base is not None:
        base = tensor._base
        geometry = (tensor.shape, tensor.stride(), tensor.storage_offset())

        def hook(grad):
            try:
                sliced = grad.as_strided(*geometry)
                served = interleaver.handle(location, sliced)
                if served is sliced:
                    return grad
                updated = grad.clone()
                updated.as_strided(*geometry).copy_(served)
                return updated
            finally:
                handle.remove()

        handle = base.register_hook(hook)
    else:

        def hook(grad):
            try:
                return interleaver.handle(location, grad)
            finally:
                handle.remove()

        handle = tensor.register_hook(hook)
    hooks.append(handle)
