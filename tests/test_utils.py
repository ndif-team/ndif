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
from ndif.services.ray.deployments.modeling.util import request_meta


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
        assert meta["max_mem_by_gpu"] == {"0": 2 * self.GB}

    def test_percent_is_against_the_headroom_the_request_had(self):
        # 2 GB of the 10 GB left over once the weights are subtracted.
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert meta["max_mem_pct_by_gpu"] == {"0": 20.0}

    def test_scalar_is_the_worst_pressured_device(self):
        meta = request_meta(
            {0: (30 * self.GB, 31 * self.GB), 1: (30 * self.GB, 34 * self.GB)},
            {0: 40 * self.GB, 1: 40 * self.GB},
            exec_ms=1000,
        )
        assert meta["max_mem_by_gpu"] == {"0": self.GB, "1": 4 * self.GB}
        assert meta["max_memory_usage"] == 4 * self.GB

    def test_freed_memory_does_not_go_negative(self):
        # A peak below the baseline means the block freed weights-adjacent memory;
        # a negative footprint is meaningless, so it floors at zero.
        meta = request_meta({0: (30 * self.GB, 29 * self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta["max_mem_by_gpu"] == {"0": 0}
        assert meta["max_mem_pct_by_gpu"] == {"0": 0.0}

    # -- runtime -----------------------------------------------------------

    def test_runtime_is_seconds_not_the_actors_milliseconds(self):
        assert request_meta({}, {}, exec_ms=1500)["runtime"] == 1.5

    def test_unmeasured_runtime_is_none(self):
        assert request_meta({}, {}, exec_ms=None)["runtime"] is None

    # -- degenerate inputs: none of these may raise ------------------------
    # The report is best-effort. It is never the thing that fails a job that
    # already succeeded.

    def test_no_headroom_reports_zero_percent_rather_than_dividing(self):
        # Weights already fill the assignment — the percentage has no denominator.
        meta = request_meta({0: (40 * self.GB, 41 * self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta["max_mem_pct_by_gpu"] == {"0": 0.0}
        assert meta["max_mem_by_gpu"] == {"0": self.GB}  # the bytes are still real

    def test_unknown_assignment_reports_zero_percent(self):
        # gpu_peaks saw a device the actor has no budget recorded for.
        meta = request_meta({3: (0, self.GB)}, self.ONE_CARD, exec_ms=1)
        assert meta["max_mem_pct_by_gpu"] == {"3": 0.0}

    def test_no_cuda_still_reports(self):
        # gpu_baselines/gpu_peaks return nothing off-GPU. The runtime is still
        # worth having, so the report is emitted with the memory fields empty.
        assert request_meta({}, {}, exec_ms=250) == {
            "runtime": 0.25,
            "max_memory_usage": 0,
            "max_mem_by_gpu": {},
            "max_mem_pct_by_gpu": {},
        }

    # -- string GPU keys ---------------------------------------------------
    # A response reaches the client as JSON *or* as torch.save bytes, and only
    # JSON stringifies dict keys. Emitting strings is what makes the two agree.

    def test_keys_are_strings_not_ints(self):
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1)
        assert list(meta["max_mem_by_gpu"]) == ["0"]
        assert list(meta["max_mem_pct_by_gpu"]) == ["0"]

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

    # -- it actually rides on the response ---------------------------------

    def test_completed_carries_the_report(self):
        request = BackendRequestModel(model_key="m", id="job")
        meta = request_meta(self.ONE_PEAK, self.ONE_CARD, exec_ms=1000)
        assert request.response(Status.COMPLETED, "done", meta_data=meta).meta_data == meta

    def test_other_statuses_carry_none(self):
        # Only the response that ends the job has anything to report.
        request = BackendRequestModel(model_key="m", id="job")
        assert request.response(Status.RUNNING, "started").meta_data is None
