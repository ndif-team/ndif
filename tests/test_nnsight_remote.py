"""Remote nnsight functionality against a local NDIF server (``remote=True``).

Each test opens ``with model.trace(..., remote=True):`` (or ``generate``), so the
traced block is serialized, run on the server's real gpt2, and saved values come
back over the wire. Shapes/paths come from ``conftest`` (gpt2).
"""

import asyncio

import nnsight
import torch

from nnsight.modeling.transformers import TransformersModel
from nnsight.schema.response import Status

from conftest import (
    HIDDEN,
    PEFT_ADAPTER,
    PROMPT,
    REPO,
    VOCAB,
    peft_installed,
    requires_server,
)

import pytest

pytestmark = requires_server


class TestRemoteTrace:
    def test_read_hidden_state(self, model):
        with model.trace(PROMPT, remote=True):
            hidden = model.transformer.h[-1].output.save()
        assert hidden.shape[-1] == HIDDEN

    def test_read_logits(self, model):
        with model.trace(PROMPT, remote=True):
            logits = model.output.logits.save()
        assert logits.shape[-1] == VOCAB

    def test_read_inputs(self, model):
        with model.trace(PROMPT, remote=True):
            block_in = model.transformer.h[0].inputs[0][0].save()
        assert block_in.shape[-1] == HIDDEN


class TestRemoteIntervention:
    def test_zeroing_output(self, model):
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].output[:] = 0
            after = model.transformer.h[0].output.save()
        assert (after == 0).all()

    def test_intervention_changes_logits(self, model):
        with model.trace(PROMPT, remote=True):
            baseline = model.output.logits.save()
        with model.trace(PROMPT, remote=True):
            model.transformer.h[5].output[:] = 0
            modified = model.output.logits.save()
        assert not torch.allclose(baseline, modified)

    def test_set_input(self, model):
        with model.trace(PROMPT, remote=True):
            model.transformer.h[1].input[:] = 0
            after = model.transformer.h[1].input.save()
        assert (after == 0).all()

    def test_edit_propagates_downstream(self, model):
        # Zeroing an early block's output changes a later block's output: the edit
        # reaches the real forward, not just the value we read back.
        with model.trace(PROMPT, remote=True):
            baseline = model.transformer.h[1].output[0].save()
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].output[0][:] = 0
            after = model.transformer.h[1].output[0].save()
        assert not torch.allclose(baseline, after)

    def test_edit_partial_slice(self, model):
        # An in-place edit of part of the output leaves the rest untouched.
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].output[0][:, :5] = 0
            out = model.transformer.h[0].output[0].save()
        assert torch.all(out[:, :5] == 0)
        assert torch.any(out[:, 5:] != 0)


class TestRemoteEarlyStop:
    def test_stop(self, model):
        with model.trace(PROMPT, remote=True) as tracer:
            first = model.transformer.h[0].output.save()
            tracer.stop()
        assert first.shape[-1] == HIDDEN


class TestRemoteAdhocModule:
    def test_logit_lens(self, model):
        # Apply the head to an intermediate hidden state (out of execution order).
        with model.trace(PROMPT, remote=True):
            hidden = model.transformer.h[-1].output
            logits = model.lm_head(model.transformer.ln_f(hidden))
            tokens = torch.softmax(logits, dim=-1).argmax(dim=-1).save()
        assert tokens.ndim == 2 and tokens.shape[0] == 1


class TestRemoteSource:
    def test_source_op_output(self, model):
        with model.trace(PROMPT, remote=True):
            act = model.transformer.h[0].mlp.source.self_act_0.output.save()
        assert act.ndim == 3  # (batch, seq, 4*hidden)

    def test_op_input(self, model):
        # The first argument to an op mid-forward: c_proj's input is the
        # post-activation hidden state, 4*hidden wide.
        with model.trace(PROMPT, remote=True):
            proj_in = model.transformer.h[0].mlp.source.self_c_proj_0.input.save()
        assert proj_in.shape[-1] == HIDDEN * 4

    def test_edit_op_output_changes_logits(self, model):
        # Writing through a source op (zero the activation inside the mlp) lands
        # mid-forward on the server and changes the model's logits.
        with model.trace(PROMPT, remote=True):
            baseline = model.output.logits.save()
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].mlp.source.self_act_0.output[:] = 0
            modified = model.output.logits.save()
        assert not torch.allclose(baseline, modified)

    def test_edit_op_input_changes_logits(self, model):
        # The other write direction: replacing an op's input changes the logits.
        with model.trace(PROMPT, remote=True):
            baseline = model.output.logits.save()
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].mlp.source.self_c_proj_0.input[:] = 0
            modified = model.output.logits.save()
        assert not torch.allclose(baseline, modified)

    def test_multiple_source_ops(self, model):
        # Several ops inside one module's forward, read in execution order.
        with model.trace(PROMPT, remote=True):
            c_fc = model.transformer.h[0].mlp.source.self_c_fc_0.output.save()
            act = model.transformer.h[0].mlp.source.self_act_0.output.save()
        assert c_fc.shape[-1] == HIDDEN * 4 and act.shape[-1] == HIDDEN * 4

    def test_op_skip(self, model):
        # Skipping an op substitutes its output; the op's own body doesn't run. Skip
        # the activation with zeros shaped from its input (c_fc's output).
        with model.trace(PROMPT, remote=True):
            model.transformer.h[0].mlp.source.self_act_0.skip(
                model.transformer.h[0].mlp.source.self_c_fc_0.output * 0
            )
            act = model.transformer.h[0].mlp.source.self_act_0.output.save()
        assert torch.all(act == 0)


class TestRemoteAliasing:
    def test_rename_module_list(self):
        m = TransformersModel(REPO, task="text-generation", rename={"transformer.h": "layers"})
        assert m.layers[0] is m.transformer.h[0]
        with m.trace(PROMPT, remote=True):
            out = m.layers[0].output.save()
        assert out.shape[-1] == HIDDEN

    def test_deep_path_rename(self):
        # A rename of a deeply-nested module mounts on the root; the alias resolves
        # to the same module remotely (the rename travels with the envoy tree).
        m = TransformersModel(
            REPO, task="text-generation", rename={"transformer.h.3.mlp": "my_mlp"}
        )
        assert m.my_mlp is m.transformer.h[3].mlp
        with m.trace(PROMPT, remote=True):
            via_alias = m.my_mlp.output.save()
            via_path = m.transformer.h[3].mlp.output.save()
        assert torch.equal(via_alias, via_path)

    def test_alias_intervention(self):
        # Write through an alias; the intervention lands on the aliased module.
        m = TransformersModel(REPO, task="text-generation", rename={"transformer.h": "layers"})
        with m.trace(PROMPT, remote=True):
            m.layers[0].output[:] = 0
            after = m.layers[0].output.save()
        assert (after == 0).all()


class TestRemoteSkip:
    def test_module_skip_passthrough(self, model):
        # Skip a block: its forward doesn't run and its output becomes the value we
        # pass (its own input here), so the block passes straight through.
        with model.trace(PROMPT, remote=True):
            block_in = model.transformer.h[0].input.save()
            model.transformer.h[0].skip(model.transformer.h[0].input)
            block_out = model.transformer.h[0].output.save()
        assert torch.equal(block_out, block_in)


class TestRemoteSession:
    def test_cross_trace_value_flow(self, model):
        # remote goes on the session, not the inner traces: the whole session
        # (both traces) runs as one remote job, so a value read in the first
        # trace flows into the second without an explicit save.
        with model.session(remote=True):
            with model.trace(PROMPT):
                h0 = model.transformer.h[0].output
            with model.trace(PROMPT):
                diff = (model.transformer.h[0].output - h0).abs().sum().save()
        assert diff.item() == 0.0  # identical input -> identical activations


class TestRemoteGenerate:
    def test_generate(self, model):
        # generate yields token ids off tracer.result (the prompt plus the new
        # tokens), not the pipeline's decoded records.
        with model.generate(PROMPT, max_new_tokens=3, do_sample=False, remote=True) as tracer:
            result = tracer.result.save()
        assert isinstance(result, torch.Tensor)
        assert result.shape[0] == 1
        assert PROMPT in model.tokenizer.decode(result[0])

    def test_iter_over_generation(self, model):
        # The block runs server-side, so accumulate into a *saved* list (a mutated
        # client-side list wouldn't return); iter[:3] targets the first 3 steps.
        with model.generate(PROMPT, max_new_tokens=3, do_sample=False, remote=True) as tracer:
            captured = nnsight.save([])
            for _ in tracer.iter[:3]:
                captured.append(model.transformer.h[0].output)
        assert len(captured) == 3
        # Step 0 processes the whole prompt; later (cached) steps process one token.
        assert captured[0].shape[1] > 1
        assert captured[1].shape[1] == 1

    def test_generator_output_is_the_result(self, model):
        # Generation output is passed through `model.generator`, so the finished ids
        # read there are the same tensor `tracer.result` returns.
        with model.generate(PROMPT, max_new_tokens=3, do_sample=False, remote=True) as tracer:
            through_generator = model.generator.output.save()
            result = tracer.result.save()
        assert torch.equal(through_generator, result)

    def test_streamer_per_step(self, model):
        # Generated tokens reach the streamer submodule a step at a time; iter[:3]
        # targets the first three. Save the accumulator *inside* the trace so the
        # server-side appends come back.
        with model.generate(PROMPT, max_new_tokens=3, do_sample=False, remote=True) as tracer:
            steps = nnsight.save([])
            for _ in tracer.iter[:3]:
                steps.append(model.generator.streamer.output)
        assert len(steps) == 3
        # Step 0 streams the whole prompt; later steps stream one new token each.
        assert steps[0].shape[-1] > 1
        assert steps[1].shape[-1] == 1


class TestRemoteGradients:
    def test_grad_capture(self, model):
        # The server loads weights with requires_grad_(False) (inference-only), so
        # activations don't track grad by default; opt an activation into the graph
        # with requires_grad_(True), then backward computes its gradient.
        with model.trace(PROMPT, remote=True):
            activation = model.transformer.h[0].output
            activation.requires_grad_(True)
            loss = model.output.logits.sum()
            with loss.backward():
                grad = activation.grad.save()
        assert grad.shape[-1] == HIDDEN
        assert torch.isfinite(grad).all()

    def test_multiple_grads_in_backward_order(self, model):
        # Two activations opted into the graph; their gradients are read in backward
        # order — layer 1's flows before layer 0's.
        with model.trace(PROMPT, remote=True):
            a0 = model.transformer.h[0].output
            a0.requires_grad_(True)
            a1 = model.transformer.h[1].output
            a1.requires_grad_(True)
            loss = model.output.logits.sum()
            with loss.backward():
                g1 = a1.grad.save()
                g0 = a0.grad.save()
        assert g0.shape[-1] == HIDDEN and g1.shape[-1] == HIDDEN
        assert torch.isfinite(g0).all() and torch.isfinite(g1).all()


class TestRemoteCache:
    def test_cache(self, model):
        with model.trace(PROMPT, remote=True) as tracer:
            cache = tracer.cache()
        assert cache["model.transformer.h.0"].output is not None
        # Path-string access works without the model (dropped for serialization).
        assert cache["model.transformer.h.0"].inputs is None

    def test_cache_every_layer(self, model):
        # A default cache captures every module the run reaches — all 12 blocks.
        with model.trace(PROMPT, remote=True) as tracer:
            cache = tracer.cache()
        for layer in range(12):
            assert cache[f"model.transformer.h.{layer}"].output is not None

    def test_cache_targeted(self, model):
        # Passing `modules=` captures only those; others aren't recorded.
        with model.trace(PROMPT, remote=True) as tracer:
            cache = tracer.cache(
                modules=["model.transformer.h.3", "model.transformer.h.7"]
            )
        assert cache["model.transformer.h.3"].output is not None
        assert cache["model.transformer.h.7"].output is not None
        assert "model.transformer.h.0" not in cache


@pytest.mark.skipif(
    not peft_installed, reason="client needs peft to graft the adapter architecture"
)
class TestRemotePeft:
    """Per-request PEFT: the client asks for an adapter via ``peft=``, the id rides
    on the request env, and the server swaps it in with ``_remoteable_set_env``
    before running (covering both the in-process and sandbox paths)."""

    def test_env_reports_adapter(self):
        m = TransformersModel(REPO, task="text-generation", peft=PEFT_ADAPTER)
        assert m._remoteable_get_env() == {"peft": PEFT_ADAPTER}
        # The adapter is grafted onto the (undispatched) client model, so its
        # paths match the adapted model the server exposes.
        assert any("lora" in name.lower() for name, _ in m._module.named_modules())

    def test_adapter_applied_remotely(self):
        adapted = TransformersModel(REPO, task="text-generation", peft=PEFT_ADAPTER)
        # The `base_model.model.*` path exists only once the model is wrapped with
        # the adapter, so resolving it against the server's model proves the server
        # applied the adapter per-request (via _remoteable_set_env) before running.
        with adapted.trace(PROMPT, remote=True):
            hidden = adapted.base_model.model.transformer.h[0].output.save()
            logits = adapted.output.logits.save()
        assert hidden.shape[-1] == HIDDEN
        assert logits.shape[-1] == VOCAB
        assert torch.isfinite(logits).all()


class TestNdif:
    def test_status_lists_deployed_model(self, model):
        from nnsight.ndif import NdifStatus

        # Ensure gpt2 is deployed, then it should show up as running.
        with model.trace(PROMPT, remote=True):
            model.output.logits.save()
        s = nnsight.status()
        assert "openai-community/gpt2" in s
        assert s.status is NdifStatus.Status.UP

    def test_is_model_running(self, model):
        with model.trace(PROMPT, remote=True):
            model.output.logits.save()
        assert nnsight.is_model_running("openai-community/gpt2", "main") is True

    def test_remote_env(self):
        from nnsight import ndif

        env = ndif.get_remote_env(force_refresh=True)
        assert "python_version" in env
        assert isinstance(env["packages"], dict) and env["packages"]

    def test_compare_returns_inspectable_object(self):
        from nnsight.ndif import EnvComparison

        result = nnsight.compare()
        assert isinstance(result, EnvComparison)
        assert isinstance(result.packages, dict) and result.packages
        assert isinstance(result.mismatches, dict)
        assert "Python Version:" in str(result)  # printable

    
    def test_persistent_objects(self, model):
        tokens = model.tokenizer("Hello World!", return_tensors="pt")["input_ids"]

        with model.trace("Hi", remote=True):
            tokens_remote = model.tokenizer("Hello World!", return_tensors="pt")["input_ids"].save()

        assert torch.equal(tokens, tokens_remote)



def _inline_negate(x):
    """A function defined in this (local) test module — used by test_inline_function
    to check code defined in the caller's own module ships to the server."""
    return -x


class TestRemoteLocalCode:
    """Local (non-installed) code used inside a remote trace ships to the server
    automatically — the backend's pull_env registers local modules for
    serialize-by-value, so no manual nnsight.register() is needed. Each test uses
    a distinct module name (sys.modules caches by name) and resets _PULLED_ENV so
    pull_env re-scans and picks up the just-written module."""

    @staticmethod
    def _add_local_module(tmp_path, monkeypatch, name, body):
        (tmp_path / f"{name}.py").write_text(body)
        monkeypatch.syspath_prepend(str(tmp_path))
        import nnsight.ndif as ndif

        monkeypatch.setattr(ndif, "_PULLED_ENV", False)

    def test_import_module_function(self, model, tmp_path, monkeypatch):
        self._add_local_module(
            tmp_path, monkeypatch, "ship_a", "def triple(x):\n    return x * 3\n"
        )
        import ship_a

        with model.trace(PROMPT, remote=True):
            out = ship_a.triple(model.transformer.h[0].output.sum()).save()
        assert torch.isfinite(out).all()

    def test_from_import_function(self, model, tmp_path, monkeypatch):
        self._add_local_module(
            tmp_path, monkeypatch, "ship_b", "def quad(x):\n    return x * 4\n"
        )
        from ship_b import quad

        with model.trace(PROMPT, remote=True):
            out = quad(model.transformer.h[0].output.sum()).save()
        assert torch.isfinite(out).all()

    def test_local_class(self, model, tmp_path, monkeypatch):
        self._add_local_module(
            tmp_path,
            monkeypatch,
            "ship_c",
            "class Scaler:\n"
            "    def __init__(self, factor):\n"
            "        self.factor = factor\n"
            "    def scale(self, x):\n"
            "        return x * self.factor\n",
        )
        from ship_c import Scaler

        with model.trace(PROMPT, remote=True):
            out = Scaler(5).scale(model.transformer.h[0].output.sum()).save()
        assert torch.isfinite(out).all()

    def test_inline_function(self, model, monkeypatch):
        # _inline_negate is defined in this test module (a local module); pull_env
        # registers it so it ships by value.
        import nnsight.ndif as ndif

        monkeypatch.setattr(ndif, "_PULLED_ENV", False)
        with model.trace(PROMPT, remote=True):
            out = _inline_negate(model.transformer.h[0].output.sum()).save()
        assert torch.isfinite(out).all()


class TestRemoteNonBlocking:
    """Non-blocking remote: submit without a websocket, then poll the backend for
    the latest status (GET /response/{id}); on COMPLETED it returns the saved
    values. Requires the server's /response endpoint + object-store response
    persistence."""

    @staticmethod
    def _poll_until_done(backend, tries=40, delay=0.5):
        import time

        for _ in range(tries):
            result = backend()  # None while running, dict on COMPLETED
            if result is not None:
                return result
            time.sleep(delay)
        return None

    def test_submit_and_poll(self, model):
        with model.trace(PROMPT, remote=True, blocking=False) as tracer:
            logits = model.output.logits.save()
        backend = tracer.backend
        assert backend.job_id is not None  # submitted, no result yet

        result = self._poll_until_done(backend)
        assert result is not None, "job did not complete"
        assert "logits" in result
        assert result["logits"].shape[-1] == VOCAB

    def test_manual_backend_poll(self, model):
        # Submit, then poll via a freshly-constructed RemoteBackend for the job id.
        from nnsight.intervention.backends.remote import RemoteBackend

        with model.trace(PROMPT, remote=True, blocking=False) as tracer:
            hidden = model.transformer.h[0].output.save()
        job_id = tracer.backend.job_id

        poller = RemoteBackend(model.to_model_key(), blocking=False, job_id=job_id)
        result = self._poll_until_done(poller)
        assert result is not None, "job did not complete"
        assert result["hidden"].shape[-1] == HIDDEN


class TestRemoteEdit:
    """Edits ride to the server in envoy._edits (serialized by value) and apply on
    the remote run."""

    def test_edit_applies_remotely(self, model):
        # non-inplace edit yields (tracer, copy); `model` stays clean.
        with model.edit() as (tracer, edited):
            edited.transformer.h[0].output[0][:] = 0
        with edited.trace(PROMPT, remote=True):
            out = edited.transformer.h[0].output[0].save()
        assert torch.all(out == 0)

    def test_original_unaffected(self, model):
        with model.edit():  # a copy is edited; the base keeps no edits
            pass
        with model.trace(PROMPT, remote=True):
            out = model.transformer.h[0].output[0].save()
        assert torch.any(out != 0)


class TestRemoteBatching:
    """Several `tracer.invoke(...)` blocks combined into one remote forward, each
    scoped to its own rows."""

    P1 = "the cat sat"
    P2 = "a much longer prompt appears here"

    def test_batched_matches_solo(self, model):
        # Each invoke's last-token prediction must equal the prompt run alone —
        # proving the server's left-pad batching + position_ids are right over the
        # wire. (argmax, not allclose: batched vs unbatched CUDA matmul differs in
        # the last fp bits; the exact-match check lives in the offline suite.)
        with model.trace(self.P1, remote=True):
            solo1 = model.output.logits[0, -1].save()
        with model.trace(self.P2, remote=True):
            solo2 = model.output.logits[0, -1].save()
        with model.trace(remote=True) as tracer:
            with tracer.invoke(self.P1):
                b1 = model.output.logits[0, -1].save()
            with tracer.invoke(self.P2):
                b2 = model.output.logits[0, -1].save()
        assert b1.argmax() == solo1.argmax()
        assert b2.argmax() == solo2.argmax()

    def test_invoke_edit_isolation(self, model):
        with model.trace(remote=True) as tracer:
            with tracer.invoke(self.P1):
                model.transformer.h[0].output[0][:] = 0
                edited = model.transformer.h[0].output[0].save()
            with tracer.invoke(self.P1):
                other = model.transformer.h[0].output[0].save()
        assert torch.all(edited == 0)
        assert torch.any(other != 0)

    def test_batched_generate(self, model):
        # Two invokes run as one batched generate; each invoke reads its own row of
        # the generated ids off tracer.result (read inside the invoke).
        with model.generate(max_new_tokens=3, remote=True) as tracer:
            with tracer.invoke("The Eiffel Tower is in"):
                a = model.transformer.h[0].output[0].save()
                first = tracer.result.save()
            with tracer.invoke("The capital of France is"):
                b = model.transformer.h[0].output[0].save()
                second = tracer.result.save()
        assert first.shape[0] == 1 and second.shape[0] == 1
        assert a.shape[-1] == HIDDEN and b.shape[-1] == HIDDEN

    def test_list_prompts_stack(self, model):
        # A list of prompts to a single generate stacks into one batch: the returned
        # ids have one row per prompt.
        with model.generate(
            [self.P1, self.P2], max_new_tokens=3, do_sample=False, remote=True
        ) as tracer:
            out = tracer.result.save()
        assert out.shape[0] == 2


class TestRemoteSaving:
    """Save mechanics over the wire: both the function form (``nnsight.save(v)``)
    and a value computed inside the block come back."""

    def test_function_form_save(self, model):
        with model.trace(PROMPT, remote=True):
            hidden = nnsight.save(model.transformer.h[-1].output)
        assert hidden.shape[-1] == HIDDEN

    def test_save_computed_value(self, model):
        # A value derived in the block (not a raw activation) returns too.
        with model.trace(PROMPT, remote=True):
            mean = model.transformer.h[-1].output.mean().save()
        assert mean.ndim == 0 and torch.isfinite(mean)


class TestRemoteMetaData:
    """What the server reports back about a finished job's cost.

    The one place the whole path is visible at once: the actor measures a real
    run on a real GPU, ``request_meta`` shapes it, it crosses the wire on the
    COMPLETED response, and the client parks it on the backend the tracer holds.
    ``test_request_meta.py`` covers the arithmetic; this covers the plumbing,
    which is all that can go wrong silently.
    """

    def test_tracer_exposes_the_job_cost(self, model):
        with model.trace(PROMPT, remote=True) as tracer:
            logits = model.output.logits.save()

        # The backend runs in __exit__, so the report exists only out here.
        meta = tracer.backend.meta_data
        assert meta is not None, "server sent no meta_data on COMPLETED"
        assert set(meta) >= {
            "runtime",
            "max_memory_usage",
            "max_mem_by_gpu",
            "max_mem_pct_by_gpu",
        }
        assert logits.shape[-1] == VOCAB  # the job really did run

    def test_runtime_is_plausible_seconds(self, model):
        with model.trace(PROMPT, remote=True) as tracer:
            model.output.logits.save()

        runtime = tracer.backend.meta_data["runtime"]
        # Seconds, not the actor's milliseconds: a gpt2 forward is well under a
        # minute, so a value in the hundreds would mean the units are wrong.
        assert 0 < runtime < 60, runtime

    def test_gpu_figures_are_reported_per_device(self, model):
        with model.trace(PROMPT, remote=True) as tracer:
            model.output.logits.save()

        meta = tracer.backend.meta_data
        by_gpu = meta["max_mem_by_gpu"]
        if not by_gpu:
            pytest.skip("server has no CUDA device to report")

        # Keys are strings whichever way the response came back (JSON frame or
        # pickled frame) — that's the point of stringifying them server-side.
        assert all(isinstance(k, str) for k in by_gpu)
        assert set(meta["max_mem_pct_by_gpu"]) == set(by_gpu)
        # A real forward pass allocates activations on top of the weights.
        assert meta["max_memory_usage"] == max(by_gpu.values()) > 0
        assert all(0 <= p <= 100 for p in meta["max_mem_pct_by_gpu"].values())


class TestRemoteAsync:
    """The async backend against a live job (``await`` and ``async for``).

    The rest of this file drives the blocking path; ``AsyncRemoteBackend`` is
    otherwise only exercised in nnsight's unit tests against a fake connection
    with a stubbed download. This is the one place the real socket, the real
    COMPLETED frame and the real result decode are on the async path.

    ``asyncio.run`` rather than pytest-asyncio: the suite has no asyncio config
    and needs none for this, and it matches how nnsight tests the same class.
    """

    def _backend(self, model):
        from nnsight.intervention.backends.remote import AsyncRemoteBackend

        # Host comes from CONFIG.API.HOST, pointed at the local server in conftest.
        return AsyncRemoteBackend(model.to_model_key())

    def test_await_returns_the_saves(self, model):
        backend = self._backend(model)
        with model.trace(PROMPT, backend=backend):
            logits = model.output.logits.save()

        # The trace has exited, so nothing is pushed back into this frame — the
        # saves come out of the await, keyed by the name they were bound to.
        result = asyncio.run(backend.resolve())
        assert result["logits"].shape[-1] == VOCAB

    def test_await_records_the_job_cost(self, model):
        backend = self._backend(model)
        with model.trace(PROMPT, backend=backend):
            model.output.logits.save()

        asyncio.run(backend.resolve())
        # resolve() goes through note(), the same shared handling the blocking
        # path uses.
        assert backend.meta_data is not None
        assert backend.meta_data["runtime"] > 0

    def test_aiter_yields_statuses_then_the_saves(self, model):
        backend = self._backend(model)
        with model.trace(PROMPT, backend=backend):
            logits = model.output.logits.save()

        async def collect():
            return [item async for item in backend]

        items = asyncio.run(collect())
        # Every item but the last is a raw status update; the saves dict is last.
        assert items[-1]["logits"].shape[-1] == VOCAB
        statuses = [item.status for item in items[:-1]]
        assert statuses[-1] == Status.COMPLETED
        assert Status.RUNNING in statuses

    def test_stream_records_the_cost_off_the_raw_frame(self, model):
        # stream() bypasses note(), so it records meta_data on its own path —
        # and the frame it yields carries the report itself. The fake-connection
        # unit test can't vouch for the server actually putting it there.
        backend = self._backend(model)
        with model.trace(PROMPT, backend=backend):
            model.output.logits.save()

        async def collect():
            return [item async for item in backend]

        items = asyncio.run(collect())
        completed = [
            item
            for item in items[:-1]
            if item.status == Status.COMPLETED
        ]
        assert completed and completed[0].meta_data is not None
        assert backend.meta_data == completed[0].meta_data
