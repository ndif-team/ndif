"""Tests for security guards in the protected environment.

These tests verify that:
1. Allowed operations work correctly (whitelisted modules, safe builtins, etc.)
2. Blocked operations are properly rejected (non-whitelisted modules, non-whitelisted builtins)

Security model:
- Import restrictions: Only whitelisted modules can be imported
- Builtin restrictions: Only whitelisted builtins are available

Note: We don't use AST-level attribute guards (like blocking __class__ access)
because RestrictedPython's AST transformation conflicts with nnsight's internal
variable naming. Security is enforced via import/builtin restrictions instead.

The tests use nnsight with remote=True pointing to a local NDIF server.
"""

import pytest
import torch
from nnsight import LanguageModel, CONFIG

# Configure for local testing
CONFIG.API.HOST = "http://localhost:5001"

# Use a small model for testing
MODEL_ID = "openai-community/gpt2"


@pytest.fixture(scope="module")
def model():
    """Load model once for all tests in this module."""
    return LanguageModel(MODEL_ID)


# =============================================================================
# TESTS FOR ALLOWED OPERATIONS
# =============================================================================


class TestAllowedOperations:
    """Tests that verify allowed operations are NOT blocked."""

    def test_basic_trace(self, model):
        """Basic remote tracing should work."""
        with model.trace("Hello world", remote=True):
            output = model.lm_head.output[0][-1].argmax(dim=-1).save()

        assert output is not None
        assert isinstance(output.item(), int)

    def test_tensor_operations(self, model):
        """Standard tensor operations should be allowed."""
        with model.trace("The Eiffel Tower", remote=True):
            hidden = model.transformer.h[0].output[0]

            # Basic operations
            mean = hidden.mean().save()
            std = hidden.std().save()
            shape = hidden.shape

            # Slicing
            sliced = hidden[:, -1, :].save()

            # Arithmetic
            doubled = (hidden * 2).mean().save()

        assert mean is not None
        assert std is not None
        assert sliced is not None
        assert doubled is not None

    def test_torch_functions(self, model):
        """torch.* functions should be allowed."""
        with model.trace("Test input", remote=True):
            hidden = model.transformer.h[0].output[0]

            # Various torch functions
            normed = (
                torch.nn.functional.layer_norm(hidden, hidden.shape[-1:]).mean().save()
            )

            softmax = torch.softmax(hidden, dim=-1).mean().save()

            zeros = torch.zeros(10).sum().save()
            ones = torch.ones(5).sum().save()

        assert normed is not None
        assert softmax is not None
        assert zeros == 0
        assert ones == 5

    def test_lambda_functions(self, model):
        """Lambda functions in user code should work."""
        add_one = lambda x: x + 1
        multiply = lambda x, y: x * y

        with model.trace("Lambda test", remote=True):
            hidden = model.transformer.h[0].output[0]

            mean = hidden.mean()
            result = add_one(mean).save()

        assert result is not None

    def test_list_operations(self, model):
        """List/dict operations should work."""
        with model.trace("List test", remote=True):
            hidden = model.transformer.h[0].output[0]

            # Create and manipulate lists
            values = [hidden[:, i, :].mean() for i in range(min(3, hidden.shape[1]))]
            first = values[0].save()

        assert first is not None

    def test_allowed_dunders(self, model):
        """Safe dunder methods should be accessible."""
        with model.trace("Dunder test", remote=True):
            hidden = model.transformer.h[0].output[0]

            # __getitem__ should work via normal syntax
            item = hidden[0, 0, 0].save()

            # Shape access should work
            shape = hidden.shape

        assert item is not None

    def test_whitelisted_modules(self, model):
        """Whitelisted modules should be importable."""
        with model.trace("Import test", remote=True):
            import math
            import collections

            hidden = model.transformer.h[0].output[0]

            # Use math module
            pi_val = math.pi

            # Use collections
            counter = collections.Counter([1, 2, 2, 3])

            result = hidden.mean().save()

        assert result is not None

    def test_numpy_operations(self, model):
        """NumPy should be allowed."""
        with model.trace("NumPy test", remote=True):
            import numpy as np

            hidden = model.transformer.h[0].output[0]

            # Create numpy arrays
            arr = np.array([1, 2, 3])

            result = hidden.mean().save()

        assert result is not None

    def test_print_statements(self, model):
        """Print statements should work (they show as LOG in remote)."""
        with model.trace("Print test", remote=True):
            hidden = model.transformer.h[0].output[0]
            print(f"Hidden shape: {hidden.shape}")
            print(f"Hidden dtype: {hidden.dtype}")

            result = hidden.mean().save()

        assert result is not None

    def test_conditionals(self, model):
        """Conditional logic should work."""
        with model.trace("Conditional test", remote=True):
            hidden = model.transformer.h[0].output[0]

            mean = hidden.mean()

            # Conditional assignment
            if mean > 0:
                result = (mean * 2).save()
            else:
                result = (mean * -2).save()

        assert result is not None

    def test_intervention_modification(self, model):
        """Modifying activations should work."""
        with model.trace("Intervention test", remote=True):
            # Save original
            original = model.transformer.h[0].output[0].clone().mean().save()

            # Modify
            model.transformer.h[0].output[0][:] = 0

            # Save modified
            modified = model.transformer.h[0].output[0].mean().save()

        assert original is not None
        assert modified is not None
        # Modified should be 0 (we set all to 0)
        assert abs(modified.item()) < 1e-6


# =============================================================================
# TESTS FOR BLOCKED OPERATIONS
# =============================================================================


class TestBlockedOperations:
    """Tests that verify dangerous operations ARE blocked."""

    def test_blocked_module_os(self, model):
        """os module should be blocked."""
        with pytest.raises(Exception):  # Could be ImportError or wrapped error
            with model.trace("Block os", remote=True):
                import os

                os.getcwd()
                model.lm_head.output.save()

    def test_blocked_module_subprocess(self, model):
        """subprocess module should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block subprocess", remote=True):
                import subprocess

                subprocess.run(["ls"])
                model.lm_head.output.save()

    def test_blocked_module_sys(self, model):
        """sys module should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block sys", remote=True):
                import sys

                sys.exit(1)
                model.lm_head.output.save()

    def test_blocked_module_socket(self, model):
        """socket module should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block socket", remote=True):
                import socket

                s = socket.socket()
                model.lm_head.output.save()

    # Note: Dunder access blocking (like __class__, __globals__, etc.) is NOT
    # currently enforced because we don't use RestrictedPython's AST transformation.
    # Security relies on import/builtin restrictions instead.

    def test_blocked_cross_module_access(self, model):
        """Accessing non-whitelisted module via whitelisted module should fail.

        e.g., torch.os should fail even though torch is whitelisted.
        """
        with pytest.raises(Exception):
            with model.trace("Block cross-module", remote=True):
                import torch

                # Try to access os through torch (if it exists)
                # This should be blocked by ProtectedModule
                _ = torch.os
                model.lm_head.output.save()

    def test_blocked_importlib(self, model):
        """importlib.import_module should be blocked for non-whitelisted modules."""
        with pytest.raises(Exception):
            with model.trace("Block importlib", remote=True):
                import importlib

                # Try to use importlib to bypass import restrictions
                os_mod = importlib.import_module("os")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_dynamic_import(self, model):
        """Dynamic __import__ should be blocked for non-whitelisted modules."""
        with pytest.raises(Exception):
            with model.trace("Block dynamic import", remote=True):
                # Try dynamic import
                os_mod = __import__("os")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_eval_import(self, model):
        """eval() trying to import should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block eval import", remote=True):
                # Try to use eval to bypass
                os_mod = eval("__import__('os')")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_open(self, model):
        """open() builtin should not be available."""
        with pytest.raises(Exception):
            with model.trace("Block open", remote=True):
                # Try to open a file
                f = open("/etc/passwd", "r")
                model.lm_head.output.save()

    def test_blocked_exec_import(self, model):
        """exec() trying to import should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block exec import", remote=True):
                # Try to use exec to bypass
                exec("import os; os.getcwd()")
                model.lm_head.output.save()

    def test_blocked_module_modification(self, model):
        """Modifying whitelisted modules should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block module modification", remote=True):
                import torch

                # Try to replace a function in torch
                torch.save = lambda *args: None
                model.lm_head.output.save()

    def test_blocked_module_deletion(self, model):
        """Deleting from whitelisted modules should be blocked."""
        with pytest.raises(Exception):
            with model.trace("Block module deletion", remote=True):
                import torch

                # Try to delete something from torch
                del torch.save
                model.lm_head.output.save()


# =============================================================================
# TESTS FOR EDGE CASES
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and potential bypasses."""

    def test_nested_attribute_access(self, model):
        """Deeply nested attribute access should work for allowed attrs."""
        with model.trace("Nested attrs", remote=True):
            # This should work - just regular attribute access
            output = model.transformer.h[0].output[0][:, -1, :].mean().save()

        assert output is not None

    def test_getattr_builtin(self, model):
        """getattr() with allowed attributes should work."""
        with model.trace("getattr allowed", remote=True):
            hidden = model.transformer.h[0].output[0]

            # getattr with allowed attribute
            mean_method = getattr(hidden, "mean")
            result = mean_method().save()

        assert result is not None

    # Note: getattr blocking for dunders is NOT currently enforced
    # because we don't use RestrictedPython's AST transformation.

    def test_hasattr_allowed(self, model):
        """hasattr() should work for checking allowed attributes."""
        with model.trace("hasattr test", remote=True):
            hidden = model.transformer.h[0].output[0]

            # Check for allowed attribute
            has_shape = hasattr(hidden, "shape")

            result = hidden.mean().save()

        assert result is not None

    def test_complex_intervention(self, model):
        """Complex interventions with multiple operations should work."""
        with model.trace("Complex intervention", remote=True):
            # Get hidden states from multiple layers
            h0 = model.transformer.h[0].output[0]
            h1 = model.transformer.h[1].output[0]

            # Combine them
            combined = (h0 + h1) / 2

            # Apply to later layer
            model.transformer.h[5].output[0][:] = combined

            # Get final output
            result = model.lm_head.output[0][-1].argmax(dim=-1).save()

        assert result is not None

    def test_iteration(self, model):
        """Iteration over tensors should work."""
        with model.trace("Iteration test", remote=True):
            hidden = model.transformer.h[0].output[0]

            # Iterate with enumerate
            for i, layer in enumerate(model.transformer.h[:3]):
                _ = layer.output[0]

            result = hidden.mean().save()

        assert result is not None


# =============================================================================
# STANDALONE TESTS (No remote, just testing guards directly)
# =============================================================================


class TestGuardsDirect:
    """Direct tests of the guard functions without remote execution."""

    def test_guarded_getattr_allowed(self):
        """guarded_getattr should allow safe attributes."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            guarded_getattr,
        )

        class Obj:
            def __init__(self):
                self.value = 42
                self.name = "test"

        obj = Obj()

        # Regular attributes should work
        assert guarded_getattr(obj, "value") == 42
        assert guarded_getattr(obj, "name") == "test"

        # Allowed dunders should work
        assert (
            guarded_getattr(obj, "__doc__") is not None
            or guarded_getattr(obj, "__doc__", None) is None
        )

    def test_guarded_getattr_blocked(self):
        """guarded_getattr should block dangerous dunders."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            guarded_getattr,
        )

        class Obj:
            pass

        obj = Obj()

        # Blocked dunders should raise
        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__class__")

        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__dict__")

        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__globals__")

    def test_restricted_compile_valid(self):
        """restricted_compile should compile valid code."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            restricted_compile,
        )

        code = restricted_compile("x = 1 + 2", mode="exec")
        assert code is not None

    def test_restricted_exec(self):
        """restricted_exec should execute code."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            restricted_exec,
        )

        # Simple code should work - use locals to capture result
        result = {}
        restricted_exec("x = 1 + 2", None, result)
        assert result.get("x") == 3

    def test_whitelisted_module_check(self):
        """WhitelistedModule.check should work correctly."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            WhitelistedModule,
        )

        # Strict mode - exact match only
        strict = WhitelistedModule(name="torch", strict=True)
        assert strict.check("torch") is True
        assert strict.check("torch.nn") is False
        assert strict.check("torchvision") is False

        # Non-strict mode - allows submodules
        non_strict = WhitelistedModule(name="torch", strict=False)
        assert non_strict.check("torch") is True
        assert non_strict.check("torch.nn") is True
        assert non_strict.check("torch.nn.functional") is True
        assert non_strict.check("torchvision") is False  # Different module

    def test_safe_builtins_has_allowed(self):
        """SAFE_BUILTINS should contain whitelisted builtins."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            SAFE_BUILTINS,
        )

        # Should have basic functions
        assert "print" in SAFE_BUILTINS
        assert "len" in SAFE_BUILTINS
        assert "range" in SAFE_BUILTINS
        assert "list" in SAFE_BUILTINS
        assert "dict" in SAFE_BUILTINS

    def test_safe_builtins_missing_dangerous(self):
        """SAFE_BUILTINS should NOT contain dangerous builtins."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            SAFE_BUILTINS,
        )

        # Should NOT have dangerous functions
        assert "open" not in SAFE_BUILTINS
        assert "exec" not in SAFE_BUILTINS  # We patch this separately
        assert "compile" not in SAFE_BUILTINS  # We patch this separately

    def test_protected_module_blocks_cross_module(self):
        """ProtectedModule should block access to non-whitelisted modules."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            ProtectedModule,
            WhitelistedModule,
        )
        from types import ModuleType

        # Create a fake module that has 'os' as an attribute
        class FakeModule(ModuleType):
            pass

        fake_torch = FakeModule("torch")

        # Create a fake 'os' module inside it
        fake_os = ModuleType("os")
        fake_os.getcwd = lambda: "/tmp"
        fake_torch.os = fake_os

        # Wrap it in ProtectedModule
        whitelist_entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(whitelist_entry)
        protected.__dict__.update(fake_torch.__dict__)

        # Accessing non-module attributes should work
        # But accessing 'os' module should fail
        with pytest.raises(AttributeError, match="not whitelisted"):
            _ = protected.os

    def test_protected_module_allows_submodules(self):
        """ProtectedModule should allow legitimate submodules."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            ProtectedModule,
            WhitelistedModule,
        )
        from types import ModuleType

        # Create fake torch and torch.nn modules
        fake_torch = ModuleType("torch")
        fake_nn = ModuleType("torch.nn")
        fake_nn.Linear = lambda: None  # fake class
        fake_torch.nn = fake_nn

        # Wrap in ProtectedModule
        whitelist_entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(whitelist_entry)
        protected.__dict__.update(fake_torch.__dict__)

        # Accessing torch.nn should work (it's a submodule)
        nn = protected.nn
        assert nn is not None

    def test_protected_module_immutable(self):
        """ProtectedModule should prevent attribute modification."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            ProtectedModule,
            WhitelistedModule,
        )
        from types import ModuleType

        # Create a fake module
        fake_torch = ModuleType("torch")
        fake_torch.save = lambda *args: "original"

        # Wrap in ProtectedModule
        whitelist_entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(whitelist_entry)
        protected.__dict__.update(fake_torch.__dict__)

        # Try to modify - should fail
        with pytest.raises(AttributeError, match="Cannot modify protected module"):
            protected.save = lambda *args: "malicious"

        # Try to delete - should fail
        with pytest.raises(AttributeError, match="Cannot modify protected module"):
            del protected.save

    def test_importlib_not_whitelisted(self):
        """importlib should not be in the whitelist."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            WHITELISTED_MODULES,
        )

        importlib_whitelisted = any(m.check("importlib") for m in WHITELISTED_MODULES)
        assert not importlib_whitelisted, "importlib should NOT be whitelisted"

    def test_os_not_whitelisted(self):
        """os should not be in the whitelist."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            WHITELISTED_MODULES,
        )

        os_whitelisted = any(m.check("os") for m in WHITELISTED_MODULES)
        assert not os_whitelisted, "os should NOT be whitelisted"

    def test_subprocess_not_whitelisted(self):
        """subprocess should not be in the whitelist."""
        from src.services.ray.src.ray.nn.security.protected_environment import (
            WHITELISTED_MODULES,
        )

        subprocess_whitelisted = any(m.check("subprocess") for m in WHITELISTED_MODULES)
        assert not subprocess_whitelisted, "subprocess should NOT be whitelisted"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
