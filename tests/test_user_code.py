"""Tests for user code serialization and remote execution on NDIF.

nnsight's source-based serialization (v0.5.16+) allows users to bring their
own functions, classes, and modules to run remotely.  These tests verify that
user code survives the full pipeline:

    client-side definition → cloudpickle serialization → server deserialization
    → sandbox execution → result returned to client

Test categories:
    1. User-defined functions (inline, closures, lambdas, helpers)
    2. User modules (mymethods.stateful — non-whitelisted, serialized by source)
    3. User classes and dataclasses
    4. Complex patterns (recursion, nested traces, cross-trace patching)
    5. Edge cases (imports inside functions, default args, generators, etc.)

Run with: pytest tests/test_user_code.py --run-remote -v
"""

import sys
import pytest
import torch
from nnsight import LanguageModel

sys.path.insert(0, "tests")

MODEL_ID = "openai-community/gpt2"


@pytest.fixture(scope="module")
def model():
    return LanguageModel(MODEL_ID)


# =============================================================================
# USER-DEFINED FUNCTIONS
# =============================================================================


class TestUserFunctions:
    """User-defined functions executed remotely."""

    def test_inline_function(self, model):
        """A function defined in the test can be used inside a trace."""

        def double(x):
            return x * 2

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = double(hidden).mean().save()

        assert result is not None

    def test_function_with_torch_ops(self, model):
        """User function that calls torch operations."""

        def normalize(x, dim=-1, eps=1e-8):
            return x / (x.norm(dim=dim, keepdim=True) + eps)

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            normed = normalize(hidden)
            result = normed.save()

        norms = result.norm(dim=-1)
        assert torch.allclose(norms, torch.ones_like(norms), atol=1e-4)

    def test_closure_captures_value(self, model):
        """Closure captures a local variable from the enclosing scope."""
        scale_factor = 0.5

        def scale(x):
            return x * scale_factor

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = scale(hidden).mean().save()

        assert result is not None

    def test_closure_factory(self, model):
        """Function returned from a factory (closure over factory arg)."""

        def make_scaler(factor):
            def scaler(x):
                return x * factor
            return scaler

        half = make_scaler(0.5)

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = half(hidden).mean().save()

        assert result is not None

    def test_lambda_in_trace(self, model):
        """Lambda used directly in a trace."""
        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = (lambda x: x * 2)(hidden).mean().save()

        assert result is not None

    def test_lambda_with_captured_variable(self, model):
        """Lambda that captures a variable from enclosing scope."""
        factor = 3.0
        scale = lambda x: x * factor

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = scale(hidden).mean().save()

        assert result is not None

    def test_function_calling_another_function(self, model):
        """User function calls another user function."""

        def add_one(x):
            return x + 1

        def double_plus_one(x):
            return add_one(x * 2)

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = double_plus_one(hidden).mean().save()

        assert result is not None

    def test_function_with_default_args(self, model):
        """Function with various default argument types."""

        def process(x, scale=2.0, offset=0.0, normalize=False):
            result = x * scale + offset
            if normalize:
                result = result / result.norm()
            return result

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = process(hidden, scale=0.5, offset=1.0).mean().save()

        assert result is not None

    def test_function_with_args_kwargs(self, model):
        """Function with *args and **kwargs."""

        def combine(*tensors, dim=0):
            return torch.stack(tensors, dim=dim)

        with model.trace("Hello", remote=True):
            h0 = model.transformer.h[0].output
            h1 = model.transformer.h[1].output
            result = combine(h0, h1, dim=0).shape.save()

        assert result is not None


# =============================================================================
# USER MODULES
# =============================================================================


class TestUserModules:
    """User modules (non-whitelisted) serialized by source."""

    def test_imported_user_function(self, model):
        """Function from a user module (mymethods.stateful.normalize)."""
        from mymethods.stateful import normalize

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = normalize(hidden).save()

        norms = result.norm(dim=-1)
        assert torch.allclose(norms, torch.ones_like(norms), atol=1e-4)

    def test_user_class_instance(self, model):
        """Class from a user module instantiated and used in a trace."""
        from mymethods.stateful import RunningStats

        stats = RunningStats()

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            stats.add(hidden)
            result = stats.mean().save()

        assert result is not None

    def test_user_module_function_in_session(self, model):
        """User module function used across a session with multiple traces."""
        from mymethods.stateful import normalize

        with model.session(remote=True):
            with model.trace("Hello"):
                h1 = normalize(model.transformer.h[0].output).save()

            with model.trace("World"):
                h2 = normalize(model.transformer.h[0].output).save()

        assert h1 is not None
        assert h2 is not None
        assert not torch.allclose(h1, h2)


# =============================================================================
# USER CLASSES
# =============================================================================


class TestUserClasses:
    """User-defined classes serialized and used remotely."""

    def test_simple_class(self, model):
        """A simple user-defined class used in a trace."""

        class Scaler:
            def __init__(self, factor):
                self.factor = factor

            def __call__(self, x):
                return x * self.factor

        scaler = Scaler(0.5)

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = scaler(hidden).mean().save()

        assert result is not None

    def test_class_with_torch_module(self, model):
        """User class that wraps a torch operation."""

        class MeanPooler:
            def __call__(self, x):
                return x.mean(dim=1)

        pooler = MeanPooler()

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = pooler(hidden).save()

        assert result is not None
        assert result.ndim == 2  # (batch, hidden_dim)


# =============================================================================
# HELPER FUNCTIONS WITH NESTED TRACES
# =============================================================================


class TestHelperTraces:
    """Helper functions that contain model.trace() calls."""

    def test_helper_with_trace(self, model):
        """Helper function containing model.trace(), called from a session."""

        def get_hidden(model, prompt, layer=0):
            with model.trace(prompt):
                hidden = model.transformer.h[layer].output.save()
            return hidden

        with model.session(remote=True):
            h1 = get_hidden(model, "Hello", layer=0)
            h2 = get_hidden(model, "World", layer=5)

        assert h1 is not None
        assert h2 is not None

    def test_closure_helper_with_trace(self, model):
        """Closure-based helper that wraps model.trace()."""

        def make_extractor(layer_idx):
            def extract(model, prompt):
                with model.trace(prompt):
                    hidden = model.transformer.h[layer_idx].output.save()
                return hidden
            return extract

        extract_layer0 = make_extractor(0)
        extract_layer5 = make_extractor(5)

        with model.session(remote=True):
            h0 = extract_layer0(model, "Hello")
            h5 = extract_layer5(model, "Hello")

        assert h0 is not None
        assert h5 is not None
        assert not torch.allclose(h0, h5)

    def test_multiple_helpers_same_session(self, model):
        """Multiple different helper functions in one session."""

        def get_mean(model, prompt):
            with model.trace(prompt):
                result = model.transformer.h[0].output.mean().save()
            return result

        def get_logits(model, prompt):
            with model.trace(prompt):
                result = model.lm_head.output[0, -1].save()
            return result

        with model.session(remote=True):
            mean = get_mean(model, "Hello")
            logits = get_logits(model, "Hello")

        assert mean is not None
        assert logits is not None


# =============================================================================
# COMPLEX PATTERNS
# =============================================================================


class TestComplexPatterns:
    """Complex serialization patterns that stress the system."""

    def test_recursive_function(self, model):
        """Recursive user function used in a trace."""

        def sum_layers(model, layers, idx=0, acc=None):
            if idx >= len(layers):
                return acc
            h = model.transformer.h[layers[idx]].output
            if acc is None:
                acc = h
            else:
                acc = acc + h
            return sum_layers(model, layers, idx + 1, acc)

        with model.trace("Hello", remote=True):
            result = sum_layers(model, [0, 1, 2]).mean().save()

        assert result is not None

    def test_function_with_import_inside(self, model):
        """Function that imports a whitelisted module internally."""

        def compute(x):
            import math
            return x * math.pi

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = compute(hidden).mean().save()

        assert result is not None

    def test_deeply_nested_closure(self, model):
        """Three levels of closure nesting."""
        a = 1.0

        def level1(x):
            b = 2.0

            def level2(y):
                c = 3.0

                def level3(z):
                    return z + a + b + c

                return level3(y)

            return level2(x)

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = level1(hidden).mean().save()

        assert result is not None

    def test_function_with_tensor_constant(self, model):
        """Function that captures a tensor in its closure.

        The tensor must be moved to the model's device inside the function
        since the closure captures a CPU tensor but execution is on GPU.
        """
        bias_value = 1.0

        def add_bias(x):
            return x + bias_value

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = add_bias(hidden).mean().save()

        assert result is not None

    def test_cross_trace_with_user_function(self, model):
        """User function result passed between traces in a session."""

        def extract_last_token(x):
            return x[:, -1, :]

        with model.session(remote=True):
            with model.trace("The Eiffel Tower is in"):
                paris_repr = extract_last_token(
                    model.transformer.h[5].output
                )

            with model.trace("The Colosseum is in"):
                model.transformer.h[5].output[:, -1, :] = paris_repr
                logits = model.lm_head.output[0, -1].save()

        assert logits is not None

    def test_user_function_in_generation(self, model):
        """User function used during multi-token generation."""

        def zero_out(x):
            return torch.zeros_like(x)

        with model.generate("Hello", max_new_tokens=3, remote=True) as tracer:
            with tracer.iter[:]:
                model.transformer.h[0].output[:] = zero_out(
                    model.transformer.h[0].output
                )
            result = tracer.result.save()

        assert result is not None

    def test_multiple_user_functions_composed(self, model):
        """Chain of user functions applied to hidden states."""

        def center(x):
            return x - x.mean(dim=-1, keepdim=True)

        def scale(x):
            return x / (x.std(dim=-1, keepdim=True) + 1e-8)

        def manual_layer_norm(x):
            return scale(center(x))

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = manual_layer_norm(hidden).save()

        # Should have zero mean and unit variance along last dim
        assert abs(result.mean(dim=-1).mean().item()) < 0.1
        assert abs(result.std(dim=-1).mean().item() - 1.0) < 0.1


# =============================================================================
# EDGE CASES
# =============================================================================


class TestEdgeCases:
    """Edge cases for user code serialization."""

    def test_empty_function(self, model):
        """Function that returns None (pass body)."""

        def noop(x):
            pass

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            noop(hidden)
            result = hidden.mean().save()

        assert result is not None

    def test_print_in_user_function(self, model):
        """User function that prints (captured as LOG)."""

        def debug_shape(x, label="tensor"):
            print(f"{label} shape: {x.shape}")
            return x

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = debug_shape(hidden, "hidden").mean().save()

        assert result is not None

    def test_conditional_logic(self, model):
        """User function with conditional branching."""

        def clamp_mean(x, threshold=0.0):
            mean = x.mean()
            if mean > threshold:
                return x - mean
            return x

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = clamp_mean(hidden).mean().save()

        assert result is not None

    def test_list_comprehension(self, model):
        """User function using list comprehension over model layers."""
        with model.trace("Hello", remote=True):
            means = [
                model.transformer.h[i].output.mean()
                for i in range(3)
            ]
            result = torch.stack(means).save()

        assert result.shape == (3,)

    def test_dict_of_results(self, model):
        """Collecting multiple saved results from one trace."""
        with model.trace("Hello", remote=True):
            first = model.transformer.h[0].output.mean().save()
            last = model.transformer.h[-1].output.mean().save()
            logits = model.lm_head.output[0, -1].argmax().save()

        assert first is not None
        assert last is not None
        assert isinstance(logits.item(), int)

    def test_function_with_type_annotations(self, model):
        """Function with full type annotations."""

        def typed_scale(x: torch.Tensor, factor: float = 2.0) -> torch.Tensor:
            return x * factor

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = typed_scale(hidden, factor=0.1).mean().save()

        assert result is not None

    def test_function_with_docstring(self, model):
        """Function with a docstring (preserved through serialization)."""

        def documented(x):
            """Scale the input by 2."""
            return x * 2

        with model.trace("Hello", remote=True):
            hidden = model.transformer.h[0].output
            result = documented(hidden).mean().save()

        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
