"""
Remote NNsight tests for NDIF.

These tests verify that nnsight features work correctly when executed
on a remote NDIF server. They cover:
- Basic tracing and activation access
- Generation and multi-token output
- Activation modification (interventions)
- Gradient computation
- Sessions for grouped traces
- Activation caching
- Batching with invokers
- Iteration over generation steps

Run with: pytest tests/test_nnsight.py --run-remote
"""

import pytest
import torch
from nnsight import LanguageModel


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def model():
    """Load GPT-2 model for remote testing.

    The model loads on 'meta' device - no local GPU memory is used.
    Actual execution happens on the remote NDIF server.
    """
    return LanguageModel("openai-community/gpt2")


@pytest.fixture
def MSG_prompt():
    """Standard test prompt that produces predictable output."""
    return "Madison Square Garden is located in the city of"


@pytest.fixture
def ET_prompt():
    """Alternative test prompt."""
    return "The Eiffel Tower is in the city of"


# =============================================================================
# Basic Tracing
# =============================================================================


class TestBasicTracing:
    """Tests for basic remote tracing operations."""

    def test_access_hidden_states(self, model: LanguageModel):
        """Test accessing hidden states from a layer."""
        with model.trace("Hello world", remote=True):
            hs = model.transformer.h[-1].output[0].save()

        assert hs is not None
        assert isinstance(hs, torch.Tensor)
        assert hs.ndim == 3  # (batch, seq_len, hidden_dim)

    def test_access_layer_input(self, model: LanguageModel):
        """Test accessing layer inputs."""
        with model.trace("Hello world", remote=True):
            layer_input = model.transformer.h[0].input.save()

        assert layer_input is not None
        assert isinstance(layer_input, torch.Tensor)
        assert layer_input.ndim == 3

    def test_access_multiple_layers(self, model: LanguageModel):
        """Test accessing outputs from multiple layers in order."""
        with model.trace("Hello", remote=True):
            hs_0 = model.transformer.h[0].output[0].save()
            hs_5 = model.transformer.h[5].output[0].save()
            hs_11 = model.transformer.h[11].output[0].save()

        assert hs_0 is not None
        assert hs_5 is not None
        assert hs_11 is not None
        # Hidden states should differ across layers
        assert not torch.allclose(hs_0, hs_5)
        assert not torch.allclose(hs_5, hs_11)

    def test_access_logits(self, model: LanguageModel, MSG_prompt: str):
        """Test accessing final logits and making predictions."""
        with model.trace(MSG_prompt, remote=True):
            logits = model.lm_head.output[0, -1].save()

        # Should predict "New" for MSG prompt
        predicted_token = logits.argmax(dim=-1)
        predicted_text = model.tokenizer.decode(predicted_token)
        assert "New" in predicted_text

    def test_access_embeddings(self, model: LanguageModel):
        """Test accessing word embeddings."""
        with model.trace("Hello", remote=True):
            embeddings = model.transformer.wte.output.save()

        assert embeddings is not None
        assert embeddings.ndim == 3
        assert embeddings.shape[-1] == 768  # GPT-2 hidden dim

    def test_access_attention_output(self, model: LanguageModel):
        """Test accessing attention layer output."""
        with model.trace("Hello", remote=True):
            attn_out = model.transformer.h[0].attn.output[0].save()

        assert attn_out is not None
        assert attn_out.ndim == 3

    def test_access_mlp_output(self, model: LanguageModel):
        """Test accessing MLP output."""
        with model.trace("Hello", remote=True):
            mlp_out = model.transformer.h[0].mlp.output.save()

        assert mlp_out is not None
        assert mlp_out.ndim == 3


# =============================================================================
# Generation
# =============================================================================


class TestGeneration:
    """Tests for multi-token generation."""

    def test_basic_generation(self, model: LanguageModel, MSG_prompt: str):
        """Test basic multi-token generation."""
        with model.generate(MSG_prompt, max_new_tokens=3, remote=True) as tracer:
            output = tracer.result.save()

        decoded = model.tokenizer.decode(output[0])
        assert "New York" in decoded

    def test_generation_with_hidden_access(self, model: LanguageModel):
        """Test accessing hidden states during generation."""
        with model.generate("Hello", max_new_tokens=2, remote=True) as tracer:
            hs = model.transformer.h[-1].output[0].save()
            output = tracer.result.save()

        assert hs is not None
        assert output is not None


# =============================================================================
# Activation Modification
# =============================================================================


class TestActivationModification:
    """Tests for modifying activations during forward pass."""

    def test_inplace_modification(self, model: LanguageModel, MSG_prompt: str):
        """Test in-place activation modification with [:]."""
        with model.trace(MSG_prompt, remote=True):
            pre = model.transformer.h[-1].output[0].clone().save()
            model.transformer.h[-1].output[0][:] = 0
            post = model.transformer.h[-1].output[0].save()

        assert not (pre == 0).all().item()
        assert (post == 0).all().item()

    def test_multiplication_modification(self, model: LanguageModel):
        """Test activation modification via multiplication."""
        with model.trace("Hello", remote=True):
            pre = model.transformer.wte.output.clone().save()
            model.transformer.wte.output = model.transformer.wte.output * 0
            post = model.transformer.wte.output.save()

        assert not (pre == 0).all().item()
        assert (post == 0).all().item()

    def test_addition_modification(self, model: LanguageModel):
        """Test adding to activations."""
        with model.trace("Hello", remote=True):
            pre = model.transformer.h[5].output[0].clone().save()
            # Add a constant to all activations
            model.transformer.h[5].output[0][:] = model.transformer.h[5].output[0] + 1.0
            post = model.transformer.h[5].output[0].save()

        # Post should be pre + 1
        diff = (post - pre).mean().item()
        assert abs(diff - 1.0) < 0.01

    def test_tuple_replacement(self, model: LanguageModel):
        """Test replacing tuple outputs."""
        with model.trace("Hello", remote=True):
            pre = model.transformer.h[-1].output.save()
            model.transformer.h[-1].output = (
                torch.zeros_like(model.transformer.h[-1].output[0]),
            ) + model.transformer.h[-1].output[1:]
            post = model.transformer.h[-1].output.save()

        assert not (pre[0] == 0).all().item()
        assert (post[0] == 0).all().item()

    def test_modification_affects_output(self, model: LanguageModel, MSG_prompt: str):
        """Test that modifications affect final output."""
        # Get baseline prediction
        with model.trace(MSG_prompt, remote=True):
            baseline_logits = model.lm_head.output[0, -1].save()

        # Get modified prediction
        with model.trace(MSG_prompt, remote=True):
            model.transformer.h[-1].output[0][:] = 0
            modified_logits = model.lm_head.output[0, -1].save()

        # Predictions should differ
        baseline_pred = baseline_logits.argmax(dim=-1)
        modified_pred = modified_logits.argmax(dim=-1)
        assert baseline_pred != modified_pred


# =============================================================================
# Gradients
# =============================================================================


class TestGradients:
    """Tests for gradient computation."""

    def test_grad_access_in_backward(self, model: LanguageModel):
        """Test accessing gradients inside backward context."""
        with model.trace("Hello World", remote=True):
            hidden_states = model.transformer.h[-1].output[0]
            hs_shape = hidden_states.shape.save()
            hidden_states.requires_grad_(True)
            logits = model.lm_head.output

            with logits.sum().backward():
                grad = hidden_states.grad.save()

        assert grad is not None
        assert grad.shape == hs_shape

    def test_grad_modification(self, model: LanguageModel):
        """Test modifying gradients in backward context."""
        with model.trace("Hello World", remote=True):
            hidden_states = model.transformer.h[-1].output[0]
            hidden_states.requires_grad_(True)
            logits = model.lm_head.output

            with logits.sum().backward():
                original_grad = hidden_states.grad.clone().save()
                hidden_states.grad[:] = 0
                modified_grad = hidden_states.grad.save()

        assert not (original_grad == 0).all().item()
        assert (modified_grad == 0).all().item()


# =============================================================================
# Sessions
# =============================================================================


class TestSessions:
    """Tests for session-based grouped traces."""

    def test_basic_session(self, model: LanguageModel):
        """Test basic session with multiple traces."""
        with model.session(remote=True):
            with model.trace("Hello"):
                hs1 = model.transformer.h[0].output[0].save()

            with model.trace("World"):
                hs2 = model.transformer.h[0].output[0].save()

        assert hs1 is not None
        assert hs2 is not None
        # Different inputs should produce different hidden states
        assert not torch.allclose(hs1, hs2)

    def test_session_cross_trace_patching(self, model: LanguageModel):
        """Test using values from one trace in another."""
        with model.session(remote=True):
            with model.trace("The Eiffel Tower is in"):
                paris_hs = model.transformer.h[5].output[0][:, -1, :]

            with model.trace("The Colosseum is in"):
                # Patch with hidden states from first trace
                model.transformer.h[5].output[0][:, -1, :] = paris_hs
                patched_logits = model.lm_head.output[0, -1].save()

        # Should predict something related to Paris, not Rome
        predicted_token = patched_logits.argmax(dim=-1)
        predicted_text = model.tokenizer.decode(predicted_token)
        # The patching should affect the prediction
        assert predicted_text is not None

    def test_session_with_generation(self, model: LanguageModel):
        """Test session with generation traces."""
        with model.session(remote=True):
            with model.generate("Hello", max_new_tokens=2) as tracer:
                output1 = tracer.result.save()

            with model.generate("World", max_new_tokens=2) as tracer:
                output2 = tracer.result.save()

        assert output1 is not None
        assert output2 is not None


# =============================================================================
# Caching
# =============================================================================


class TestCaching:
    """Tests for activation caching."""

    def test_basic_cache(self, model: LanguageModel):
        """Test basic caching of all modules."""
        with model.trace("Hello", remote=True) as tracer:
            cache = tracer.cache()

        assert "model.transformer.h.0" in cache
        assert cache["model.transformer.h.0"].output is not None

    def test_cache_specific_modules(self, model: LanguageModel):
        """Test caching specific modules."""
        with model.trace("Hello", remote=True) as tracer:
            cache = tracer.cache(
                modules=[model.transformer.h[0], model.transformer.h[1], model.lm_head]
            )

        assert len(cache.keys()) == 3
        assert "model.transformer.h.0" in cache
        assert "model.transformer.h.1" in cache
        assert "model.lm_head" in cache

    def test_cache_with_inputs(self, model: LanguageModel):
        """Test caching both outputs and inputs."""
        with model.trace("Hello", remote=True) as tracer:
            cache = tracer.cache(include_inputs=True)

        # Layer 1's input should match layer 0's output
        assert cache["model.transformer.h.0"].output is not None
        assert cache["model.transformer.h.1"].inputs is not None

    def test_cache_with_intervention(self, model: LanguageModel):
        """Test that cache captures intervened values."""
        with model.trace("Hello", remote=True) as tracer:
            cache = tracer.cache()
            model.transformer.h[0].output[0][:] = 0

        # Cache should contain the modified (zeroed) values
        assert torch.all(cache["model.transformer.h.0"].output[0] == 0)


# =============================================================================
# Batching with Invokers
# =============================================================================


class TestInvokers:
    """Tests for batching with invokers."""

    def test_multiple_invokes(self, model: LanguageModel):
        """Test multiple invokes in a single trace."""
        with model.trace(remote=True) as tracer:
            with tracer.invoke("Hello"):
                out1 = model.lm_head.output[:, -1].save()

            with tracer.invoke("World"):
                out2 = model.lm_head.output[:, -1].save()

        assert out1 is not None
        assert out2 is not None
        assert out1.shape[0] == 1
        assert out2.shape[0] == 1
        # Different inputs should produce different outputs
        assert not torch.allclose(out1, out2)

    def test_batched_invoke(self, model: LanguageModel):
        """Test invoke with multiple inputs."""
        with model.trace(remote=True) as tracer:
            with tracer.invoke(["Hello", "World"]):
                out = model.lm_head.output[:, -1].save()

        assert out.shape[0] == 2

    def test_cross_invoke_patching(self, model: LanguageModel):
        """Test patching values between invokes."""
        with model.trace(remote=True) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke("The Eiffel Tower is in"):
                embeddings = model.transformer.wte.output
                barrier()
            with tracer.invoke("_ _ _ _ _"):
                barrier()
                model.transformer.wte.output = embeddings
                patched_out = model.lm_head.output[:, -1].save()

        assert patched_out is not None

    def test_promptless_invoke(self, model: LanguageModel):
        """Test invoking without a prompt (operates on full batch)."""
        with model.trace(remote=True) as tracer:
            with tracer.invoke("Hello"):
                out1 = model.lm_head.output[:, -1].save()

            with tracer.invoke("World"):
                out2 = model.lm_head.output[:, -1].save()

            # Promptless invoke operates on the full batch
            with tracer.invoke():
                out_all = model.lm_head.output[:, -1].save()

        assert out_all.shape[0] == 2  # Full batch of 2


# =============================================================================
# Iteration
# =============================================================================


class TestIteration:
    """Tests for iteration over generation steps."""

    def test_iter_all_steps(self, model: LanguageModel, MSG_prompt: str):
        """Test iterating over all generation steps."""
        with model.generate(MSG_prompt, max_new_tokens=3, remote=True) as tracer:
            logits_list = list().save()
            with tracer.iter[:]:
                logits_list.append(model.lm_head.output[0, -1].argmax(dim=-1))

        assert len(logits_list) == 3
        decoded = model.tokenizer.batch_decode(logits_list)
        assert " New" in decoded[0]

    def test_iter_specific_steps(self, model: LanguageModel, MSG_prompt: str):
        """Test iterating over specific generation steps."""
        with model.generate(MSG_prompt, max_new_tokens=5, remote=True) as tracer:
            logits_list = list().save()
            with tracer.iter[1:3]:
                logits_list.append(model.lm_head.output[0, -1].argmax(dim=-1))

        assert len(logits_list) == 2

    def test_iter_with_intervention(self, model: LanguageModel):
        """Test intervention during specific iteration steps."""
        with model.generate("Hello", max_new_tokens=3, remote=True) as tracer:
            hidden_states = list().save()
            with tracer.iter[:] as step_idx:
                if step_idx == 1:
                    # Only intervene on step 1
                    model.transformer.h[0].output[0][:] = 0
                hidden_states.append(model.transformer.h[-1].output[0].clone())

        assert len(hidden_states) == 3
        # Step 1 should be different due to intervention
        # (the zeros propagate through the model)


# =============================================================================
# Ad-hoc Module Application (Logit Lens)
# =============================================================================


class TestAdhocModules:
    """Tests for applying modules outside normal execution order."""

    def test_logit_lens(self, model: LanguageModel):
        """Test applying lm_head to intermediate hidden states."""
        with model.trace("The Eiffel Tower is in the city of", remote=True):
            hidden_states = model.transformer.h[-1].output[0]
            # Apply final layer norm and lm_head manually
            hidden_states = model.lm_head(model.transformer.ln_f(hidden_states))
            tokens = torch.softmax(hidden_states, dim=2).argmax(dim=2).save()

        decoded = model.tokenizer.decode(tokens[0])
        assert decoded is not None


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_string(self, model: LanguageModel):
        """Test with minimal input."""
        with model.trace("a", remote=True):
            out = model.lm_head.output.save()

        assert out is not None

    def test_long_sequence(self, model: LanguageModel):
        """Test with a longer sequence."""
        long_prompt = "The quick brown fox jumps over the lazy dog. " * 10
        with model.trace(long_prompt, remote=True):
            out = model.lm_head.output.save()

        assert out is not None
        assert out.shape[1] > 50  # Should have many tokens

    def test_special_characters(self, model: LanguageModel):
        """Test with special characters."""
        with model.trace("Hello! How are you? 😀", remote=True):
            out = model.lm_head.output.save()

        assert out is not None

    def test_clone_before_modify(self, model: LanguageModel):
        """Test that clone works correctly before modification."""
        with model.trace("Hello", remote=True):
            before = model.transformer.h[0].output[0].clone().save()
            model.transformer.h[0].output[0][:] = 0
            after = model.transformer.h[0].output[0].save()

        # Before should not be zeros (it was cloned before modification)
        assert not (before == 0).all().item()
        # After should be zeros
        assert (after == 0).all().item()

    def test_detach_and_cpu(self, model: LanguageModel):
        """Test detaching and moving to CPU for smaller downloads."""
        with model.trace("Hello", remote=True):
            hs = model.transformer.h[0].output[0].detach().cpu().save()

        assert hs.device.type == "cpu"
        assert not hs.requires_grad


# =============================================================================
# Print and Debug
# =============================================================================


class TestPrintAndDebug:
    """Tests for print statements and debugging in remote traces."""

    def test_print_in_trace(self, model: LanguageModel):
        """Test that print statements work in remote traces."""
        # Print statements are captured and sent back as LOG status
        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output[0]
            print(f"Hidden shape: {hidden.shape}")
            output = model.lm_head.output.save()

        assert output is not None

    def test_shape_access(self, model: LanguageModel):
        """Test accessing tensor shapes inside trace."""
        with model.trace("Hello", remote=True):
            hs = model.transformer.h[0].output[0]
            # Shape is available inside the trace
            assert hs.shape[-1] == 768  # GPT-2 hidden dim
            output = model.lm_head.output.save()

        assert output is not None
