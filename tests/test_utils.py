"""Unit tests for `services/ray/deployments/modeling/util.py`.

Pure functions over synthetic numbers — no GPU, no Ray, no server — unlike the
rest of the suite. One class per helper; add a class alongside as helpers grow.
"""

from __future__ import annotations

import pytest

# The helpers are leaves, but the wire models they feed subclass nnsight's, and
# a bare ImportError here would abort collection for the whole directory. Skip
# instead, as test_placement.py does for the server's own dependencies.
pytest.importorskip("nnsight", reason="the wire models subclass nnsight's")

from ndif.common.schema.request import BackendRequestModel
from ndif.common.schema.response import Status
from ndif.services.ray.deployments.modeling.util import (
    alloc_shortfall,
    is_oom,
    request_meta,
)


def message_error(message: str) -> BaseException:
    """A torch OOM carrying ``message`` -- what the actor is handed to report on."""
    import torch

    return torch.cuda.OutOfMemoryError(message)


class TestRequestMeta:
    """What a finished job reports back about its own cost.

    `request_meta` turns the actor's raw measurements — `gpu_peaks` output and a
    wall clock in ms — into the dict that rides on the COMPLETED response as
    nnsight's `ResponseModel.meta_data`.

    Worth testing because the figures are unverifiable at runtime: a wrong
    denominator or a stringified key doesn't fail anything, it just quietly tells
    every user something untrue about what their block cost.
    """

    GB = 1024**3

    # One assigned card, 40 GB of it, holding 30 GB of weights: 10 GB of headroom.
    ONE_CARD = {0: 40 * GB}
    # The request peaked 2 GB above the resident weights — a fifth of its headroom.
    ONE_PEAK = {0: (30 * GB, 32 * GB)}

    # -- the per-device figures --------------------------------------------

    def test_bytes_are_measured_above_the_resident_weights(self):
        # Not the device's total usage: the 30 GB of weights are the actor's.
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert meta.max_mem_by_gpu == {"0": 2 * self.GB}

    def test_percent_is_against_the_headroom_the_request_had(self):
        # 2 GB of the 10 GB left over once the weights are subtracted.
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert meta.max_mem_pct_by_gpu == {"0": 20.0}

    def test_scalar_is_the_worst_pressured_device(self):
        meta = request_meta(
            {0: (30 * self.GB, 31 * self.GB), 1: (30 * self.GB, 34 * self.GB)},
            {0: 40 * self.GB, 1: 40 * self.GB},
            exec_ms=1000,
        )
        assert meta.max_mem_by_gpu == {"0": self.GB, "1": 4 * self.GB}
        assert meta.max_memory_usage == 4 * self.GB

    def test_freed_memory_does_not_go_negative(self):
        # A peak below the baseline means the block freed weights-adjacent memory;
        # a negative footprint is meaningless, so it floors at zero.
        meta = request_meta({0: (30 * self.GB, 29 * self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta.max_mem_by_gpu == {"0": 0}
        assert meta.max_mem_pct_by_gpu == {"0": 0.0}

    # -- runtime -----------------------------------------------------------

    def test_runtime_is_seconds_not_the_actors_milliseconds(self):
        assert request_meta({}, {}, exec_ms=1500).runtime == 1.5

    def test_unmeasured_runtime_is_none(self):
        assert request_meta({}, {}, exec_ms=None).runtime is None

    # -- degenerate inputs: none of these may raise ------------------------
    # The report is best-effort. It is never the thing that fails a job that
    # already succeeded.

    def test_no_headroom_reports_zero_percent_rather_than_dividing(self):
        # Weights already fill the assignment — the percentage has no denominator.
        meta = request_meta({0: (40 * self.GB, 41 * self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta.max_mem_pct_by_gpu == {"0": 0.0}
        assert meta.max_mem_by_gpu == {"0": self.GB}  # the bytes are still real

    def test_unknown_assignment_reports_zero_percent(self):
        # gpu_peaks saw a device the actor has no budget recorded for.
        meta = request_meta({3: (0, self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta.max_mem_pct_by_gpu == {"3": 0.0}

    def test_no_cuda_still_reports(self):
        # gpu_baselines/gpu_peaks return nothing off-GPU. The runtime is still
        # worth having, so the report is emitted with the memory fields empty.
        assert request_meta({}, {}, exec_ms=250).model_dump() == {
            "runtime": 0.25,
            "max_memory_usage": 0,
            "max_mem_by_gpu": {},
            "max_mem_pct_by_gpu": {},
            "alloc_shortfall_by_gpu": None,
        }

    # -- string GPU keys ---------------------------------------------------
    # A response reaches the client as JSON *or* as torch.save bytes, and only
    # JSON stringifies dict keys. Emitting strings is what makes the two agree.

    def test_keys_are_strings_not_ints(self):
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1)
        assert list(meta.max_mem_by_gpu) == ["0"]
        assert list(meta.max_mem_pct_by_gpu) == ["0"]

    def test_both_wire_routes_produce_the_same_report(self):
        from nnsight.schema.response import ResponseModel

        request = BackendRequestModel(model_key="m", id="job")
        response = request.response(
            Status.COMPLETED, data=None, meta_data=request_meta(self.ONE_PEAK, self.ONE_CARD, 1)
        )
        # JSON frame (a url result, or a non-blocking job's stored status) vs the
        # pickled frame (the result blob riding along on the response).
        as_json = ResponseModel.model_validate_json(response.model_dump_json())
        as_pickle = ResponseModel.unpickle(response.pickle())
        assert as_json.meta_data == as_pickle.meta_data == response.meta_data

    # -- the optional out-of-memory key ------------------------------------

    def test_no_shortfall_without_an_exception(self):
        # A COMPLETED job. None is the answer to "did this run out of memory".
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert meta.alloc_shortfall_by_gpu is None

    def test_an_ordinary_failure_reports_no_shortfall(self):
        meta = request_meta(
            self.ONE_PEAK, self.ONE_CARD, 1000, ValueError("shape mismatch")
        )
        assert meta.alloc_shortfall_by_gpu is None

    def test_an_oom_adds_the_shortfall(self, monkeypatch):
        import torch

        monkeypatch.setattr(torch.cuda, "max_memory_reserved", lambda d: 30 * self.GB,
                            raising=False)
        meta = request_meta(
            self.ONE_PEAK,
            self.ONE_CARD,
            1000,
            message_error("Tried to allocate 20.00 GiB. GPU 0 has ..."),
        )
        # 40 GiB assigned, 30 reserved -> 10 free; a 20 GiB ask is 10 GiB short.
        assert meta.alloc_shortfall_by_gpu == {"0": 10 * self.GB}
        # ...and the rest of the report is unchanged by it.
        assert meta.max_mem_by_gpu == {"0": 2 * self.GB}

    # -- it actually rides on the response ---------------------------------

    def test_completed_carries_the_report(self):
        request = BackendRequestModel(model_key="m", id="job")
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert request.response(Status.COMPLETED, "done", meta_data=meta).meta_data == meta

    def test_other_statuses_carry_none(self):
        # Only the response that ends the job has anything to report.
        request = BackendRequestModel(model_key="m", id="job")
        assert request.response(Status.RUNNING, "started").meta_data is None


class TestAllocShortfall:
    """How far past its allowance a refused allocation reached, per device.

    The size of the refused allocation on its own doesn't say what to do about
    it: asking for 2 GB with 1.9 GB free and asking for it with nothing free are
    the same number and completely different problems. This is the difference.

    Only the requested size is read from the exception -- the budget is the
    actor's own and the reserved figure is torch's, which is stubbed here so the
    arithmetic is checkable without a GPU.
    """

    GB = 1024**3

    # A real message, captured from a live gpt2 actor refusing an 8 GiB block.
    # Verbatim on purpose: if a torch upgrade rewords this, these tests are what
    # says so, rather than the field quietly going None in production.
    REAL = (
        "CUDA out of memory. Tried to allocate 8.00 GiB. GPU 0 has a total "
        "capacity of 47.37 GiB of which 46.57 GiB is free. Including "
        "non-PyTorch memory, this process has 772.00 MiB memory in use. "
        "800.55 MiB allowed; Of the allocated memory 245.48 MiB is allocated by "
        "PyTorch, and 16.52 MiB is reserved by PyTorch but unallocated."
    )

    @pytest.fixture
    def reserved(self, monkeypatch):
        """Pin what torch reports as reserved on every device."""
        import torch

        def pin(value):
            monkeypatch.setattr(
                torch.cuda, "max_memory_reserved", lambda d: value, raising=False
            )

        return pin

    def test_reads_a_real_refusal(self, reserved):
        # 8 GiB asked for against 800.55 MiB allowed with 262 MiB reserved:
        # ~538.55 MiB was free, so the block was ~7.47 GiB short.
        reserved(262 * 1024**2)
        short = alloc_shortfall(
            RuntimeError(self.REAL), {0: int(800.55 * 1024**2)}
        )
        assert round(short["0"] / self.GB, 2) == 7.47

    def test_takes_the_card_from_the_message(self, reserved):
        reserved(0)
        message = "CUDA out of memory. Tried to allocate 2.00 GiB. GPU 3 has ..."
        short = alloc_shortfall(message_error(message), {0: self.GB, 3: self.GB})
        assert list(short) == ["3"]

    def test_falls_back_to_the_only_card_when_none_is_named(self, reserved):
        reserved(0)
        short = alloc_shortfall(
            message_error("CUDA out of memory. Tried to allocate 2.00 GiB."),
            {2: self.GB},
        )
        assert list(short) == ["2"]

    def test_gives_up_when_the_card_is_ambiguous(self, reserved):
        # No GPU named and several assigned -- guessing would be worse than
        # saying nothing.
        reserved(0)
        assert alloc_shortfall(
            message_error("CUDA out of memory. Tried to allocate 2.00 GiB."),
            {0: self.GB, 1: self.GB},
        ) is None

    def test_understands_the_smaller_units(self, reserved):
        # torch scales the unit to the size, so MiB shows up on near misses.
        reserved(0)
        short = alloc_shortfall(
            message_error("Tried to allocate 512.00 MiB. GPU 0 has ..."),
            {0: 256 * 1024**2},
        )
        assert short == {"0": 256 * 1024**2}

    def test_is_the_part_of_the_request_that_did_not_fit(self, reserved):
        # 2 GiB budget with 1.72 reserved leaves 0.28 free; a 1.5 GiB ask is
        # therefore 1.22 GiB short.
        reserved(int(1.72 * self.GB))
        short = alloc_shortfall(
            message_error("Tried to allocate 1.50 GiB. GPU 0 has ..."),
            {0: 2 * self.GB},
        )
        assert round(short["0"] / self.GB, 2) == 1.22

    def test_measured_against_reserved_not_allocated(self, reserved):
        # The cap is enforced on what the allocator reserved from the driver, so
        # the same ask against the same budget is shorter by more when more is
        # already reserved.
        error = message_error("Tried to allocate 2.00 GiB. GPU 0 has ...")
        budget = {0: 2 * self.GB}
        reserved(3 * self.GB // 2)
        tight = alloc_shortfall(error, budget)      # 0.5 free -> 1.5 short
        reserved(self.GB // 2)
        loose = alloc_shortfall(error, budget)      # 1.5 free -> 0.5 short
        assert tight["0"] == 3 * self.GB // 2
        assert loose["0"] == self.GB // 2

    def test_floors_at_zero(self, reserved):
        # The request would have fit in what was free -- nothing to report
        # rather than a negative shortfall.
        reserved(0)
        short = alloc_shortfall(
            message_error("Tried to allocate 1.00 GiB. GPU 0 has ..."),
            {0: 8 * self.GB},
        )
        assert short == {"0": 0}

    def test_keys_are_strings_like_the_other_per_gpu_maps(self, reserved):
        # Same reason as request_meta's: the JSON and torch.save encodings of a
        # response must agree on the key type.
        reserved(0)
        short = alloc_shortfall(
            message_error("Tried to allocate 2.00 GiB. GPU 0 has ..."), {0: self.GB}
        )
        assert list(short) == ["0"]

    def test_none_when_the_failure_is_not_a_refusal(self, reserved):
        # The overwhelming case: a job that failed for some other reason. No
        # stale record to inherit, because there is no record.
        reserved(0)
        assert alloc_shortfall(ValueError("shape mismatch"), {0: self.GB}) is None

    def test_none_when_the_card_has_no_recorded_budget(self, reserved):
        reserved(0)
        assert alloc_shortfall(
            message_error("Tried to allocate 2.00 GiB. GPU 7 has ..."),
            {0: self.GB},
        ) is None


class TestIsOom:
    """Which failures get the memory report attached."""

    def test_recognizes_the_torch_error_by_type(self):
        import torch

        assert is_oom(torch.cuda.OutOfMemoryError("CUDA out of memory."))

    def test_recognizes_a_wrapped_one_by_message(self):
        # A block can re-raise its own way out of a nested call and still be the
        # same failure.
        assert is_oom(RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"))

    def test_an_ordinary_failure_is_not_an_oom(self):
        assert not is_oom(ValueError("shape mismatch"))
