"""Tests for the NDIF sandbox security system.

Two kinds of tests:

    1. Remote tests (TestAllowedOperations, TestBlockedOperations, TestEdgeCases)
       — require a running NDIF server at localhost:5001 with GPT-2 loaded.
       Exercise the full pipeline: client serialization → deserialization →
       sandbox execution → result.

    2. Direct tests (TestGuardsDirect, TestWhitelist, TestDeserialization,
       TestSandboxFinder, TestAuditHook, TestProtectedObjects)
       — run in-process with no server.  Test individual sandbox components
       in isolation.
"""

import io
import os
import pickle
import subprocess
import socket
import sys
from types import ModuleType

import cloudpickle
import numpy as np
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
# REMOTE TESTS — ALLOWED OPERATIONS
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

            mean = hidden.mean().save()
            std = hidden.std().save()
            shape = hidden.shape
            sliced = hidden[:, -1, :].save()
            doubled = (hidden * 2).mean().save()

        assert mean is not None
        assert std is not None
        assert sliced is not None
        assert doubled is not None

    def test_torch_functions(self, model):
        """torch.* functions should be allowed."""
        with model.trace("Test input", remote=True):
            hidden = model.transformer.h[0].output[0]

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

        with model.trace("Lambda test", remote=True):
            hidden = model.transformer.h[0].output[0]
            result = add_one(hidden.mean()).save()

        assert result is not None

    def test_list_operations(self, model):
        """List/dict operations should work."""
        with model.trace("List test", remote=True):
            hidden = model.transformer.h[0].output[0]
            values = [hidden[:, i, :].mean() for i in range(min(3, hidden.shape[1]))]
            first = values[0].save()

        assert first is not None

    def test_allowed_dunders(self, model):
        """Safe dunder methods should be accessible."""
        with model.trace("Dunder test", remote=True):
            hidden = model.transformer.h[0].output[0]
            item = hidden[0, 0, 0].save()
            shape = hidden.shape

        assert item is not None

    def test_whitelisted_modules(self, model):
        """Whitelisted modules should be importable."""
        with model.trace("Import test", remote=True):
            import math
            import collections

            hidden = model.transformer.h[0].output[0]
            pi_val = math.pi
            counter = collections.Counter([1, 2, 2, 3])
            result = hidden.mean().save()

        assert result is not None

    def test_numpy_operations(self, model):
        """NumPy should be allowed."""
        with model.trace("NumPy test", remote=True):
            import numpy as np

            arr = np.array([1, 2, 3])
            hidden = model.transformer.h[0].output[0]
            result = hidden.mean().save()

        assert result is not None

    def test_print_statements(self, model):
        """Print statements should work (they show as LOG in remote)."""
        with model.trace("Print test", remote=True):
            hidden = model.transformer.h[0].output[0]
            print(f"Hidden shape: {hidden.shape}")
            result = hidden.mean().save()

        assert result is not None

    def test_conditionals(self, model):
        """Conditional logic should work."""
        with model.trace("Conditional test", remote=True):
            hidden = model.transformer.h[0].output[0]
            mean = hidden.mean()
            if mean > 0:
                result = (mean * 2).save()
            else:
                result = (mean * -2).save()

        assert result is not None

    def test_intervention_modification(self, model):
        """Modifying activations should work."""
        with model.trace("Intervention test", remote=True):
            original = model.transformer.h[0].output[0].clone().mean().save()
            model.transformer.h[0].output[0][:] = 0
            modified = model.transformer.h[0].output[0].mean().save()

        assert original is not None
        assert modified is not None
        assert abs(modified.item()) < 1e-6


# =============================================================================
# REMOTE TESTS — BLOCKED OPERATIONS
# =============================================================================


class TestBlockedOperations:
    """Tests that verify dangerous operations ARE blocked."""

    def test_blocked_module_os(self, model):
        with pytest.raises(Exception):
            with model.trace("Block os", remote=True):
                import os
                os.getcwd()
                model.lm_head.output.save()

    def test_blocked_module_subprocess(self, model):
        with pytest.raises(Exception):
            with model.trace("Block subprocess", remote=True):
                import subprocess
                subprocess.run(["ls"])
                model.lm_head.output.save()

    def test_blocked_module_sys(self, model):
        with pytest.raises(Exception):
            with model.trace("Block sys", remote=True):
                import sys
                sys.exit(1)
                model.lm_head.output.save()

    def test_blocked_module_socket(self, model):
        with pytest.raises(Exception):
            with model.trace("Block socket", remote=True):
                import socket
                s = socket.socket()
                model.lm_head.output.save()

    def test_blocked_cross_module_access(self, model):
        """torch.os should be blocked even though torch is whitelisted."""
        with pytest.raises(Exception):
            with model.trace("Block cross-module", remote=True):
                import torch
                _ = torch.os
                model.lm_head.output.save()

    def test_blocked_importlib(self, model):
        with pytest.raises(Exception):
            with model.trace("Block importlib", remote=True):
                import importlib
                os_mod = importlib.import_module("os")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_dynamic_import(self, model):
        with pytest.raises(Exception):
            with model.trace("Block dynamic import", remote=True):
                os_mod = __import__("os")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_eval_import(self, model):
        with pytest.raises(Exception):
            with model.trace("Block eval import", remote=True):
                os_mod = eval("__import__('os')")
                os_mod.getcwd()
                model.lm_head.output.save()

    def test_blocked_open(self, model):
        with pytest.raises(Exception):
            with model.trace("Block open", remote=True):
                f = open("/etc/passwd", "r")
                model.lm_head.output.save()

    def test_blocked_exec_import(self, model):
        with pytest.raises(Exception):
            with model.trace("Block exec import", remote=True):
                exec("import os; os.getcwd()")
                model.lm_head.output.save()

    def test_blocked_module_modification(self, model):
        with pytest.raises(Exception):
            with model.trace("Block module modification", remote=True):
                import torch
                torch.save = lambda *args: None
                model.lm_head.output.save()

    def test_blocked_module_deletion(self, model):
        with pytest.raises(Exception):
            with model.trace("Block module deletion", remote=True):
                import torch
                del torch.save
                model.lm_head.output.save()


# =============================================================================
# REMOTE TESTS — EDGE CASES
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and potential bypasses."""

    def test_nested_attribute_access(self, model):
        with model.trace("Nested attrs", remote=True):
            output = model.transformer.h[0].output[0][:, -1, :].mean().save()
        assert output is not None

    def test_getattr_builtin(self, model):
        with model.trace("getattr allowed", remote=True):
            hidden = model.transformer.h[0].output[0]
            mean_method = getattr(hidden, "mean")
            result = mean_method().save()
        assert result is not None

    def test_hasattr_allowed(self, model):
        with model.trace("hasattr test", remote=True):
            hidden = model.transformer.h[0].output[0]
            has_shape = hasattr(hidden, "shape")
            result = hidden.mean().save()
        assert result is not None

    def test_complex_intervention(self, model):
        with model.trace("Complex intervention", remote=True):
            h0 = model.transformer.h[0].output[0]
            h1 = model.transformer.h[1].output[0]
            combined = (h0 + h1) / 2
            model.transformer.h[5].output[0][:] = combined
            result = model.lm_head.output[0][-1].argmax(dim=-1).save()
        assert result is not None

    def test_iteration(self, model):
        with model.trace("Iteration test", remote=True):
            hidden = model.transformer.h[0].output[0]
            for i, layer in enumerate(model.transformer.h[:3]):
                _ = layer.output[0]
            result = hidden.mean().save()
        assert result is not None


# =============================================================================
# DIRECT TESTS — GUARDS
# =============================================================================


class TestGuardsDirect:
    """Direct tests of guard functions without remote execution."""

    def test_guarded_getattr_allowed(self):
        from ndif.services.ray.nn.security.guards import guarded_getattr

        class Obj:
            value = 42
            name = "test"

        obj = Obj()
        assert guarded_getattr(obj, "value") == 42
        assert guarded_getattr(obj, "name") == "test"
        # Allowed dunders should work (Obj has no docstring so __doc__ is None)
        assert guarded_getattr(obj, "__doc__") is None
        assert guarded_getattr(obj, "__name__", "fallback") is not None

    def test_guarded_getattr_blocked(self):
        from ndif.services.ray.nn.security.guards import guarded_getattr

        obj = object()
        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__class__")
        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__dict__")
        with pytest.raises(AttributeError):
            guarded_getattr(obj, "__globals__")

    def test_safe_getattr_in_builtins(self):
        """SAFE_BUILTINS[getattr] should be the guarded version."""
        from ndif.services.ray.nn.security.whitelist import SAFE_BUILTINS
        from ndif.services.ray.nn.security.guards import safe_getattr
        assert SAFE_BUILTINS["getattr"] is safe_getattr

    def test_safe_getattr_blocks_dunders(self):
        from ndif.services.ray.nn.security.guards import safe_getattr

        obj = object()
        with pytest.raises(AttributeError):
            safe_getattr(obj, "__class__")
        with pytest.raises(AttributeError):
            safe_getattr(obj, "__globals__")
        with pytest.raises(AttributeError):
            safe_getattr(obj, "__code__")

    def test_safe_getattr_allows_normal(self):
        from ndif.services.ray.nn.security.guards import safe_getattr

        class Obj:
            x = 42

        assert safe_getattr(Obj(), "x") == 42

    def test_safe_getattr_default_none(self):
        """safe_getattr(obj, 'missing', None) should return None, not raise."""
        from ndif.services.ray.nn.security.guards import safe_getattr

        assert safe_getattr(object(), "nonexistent", None) is None

    def test_safe_getattr_no_default_raises(self):
        """safe_getattr(obj, 'missing') should raise AttributeError."""
        from ndif.services.ray.nn.security.guards import safe_getattr

        with pytest.raises(AttributeError):
            safe_getattr(object(), "nonexistent")

    def test_safe_hasattr_blocks_dunders(self):
        from ndif.services.ray.nn.security.guards import safe_hasattr

        assert safe_hasattr(object(), "__globals__") is False
        assert safe_hasattr(object(), "__code__") is False

    def test_safe_hasattr_allows_normal(self):
        from ndif.services.ray.nn.security.guards import safe_hasattr

        class Obj:
            x = 1

        assert safe_hasattr(Obj(), "x") is True
        assert safe_hasattr(Obj(), "nonexistent") is False

    def test_safe_setattr_blocks_dunders(self):
        from ndif.services.ray.nn.security.guards import safe_setattr

        with pytest.raises(AttributeError):
            safe_setattr(object(), "__class__", int)
        with pytest.raises(AttributeError):
            safe_setattr(object(), "__dict__", {})

    def test_safe_delattr_blocks_dunders(self):
        from ndif.services.ray.nn.security.guards import safe_delattr

        class Obj:
            __x__ = 1

        with pytest.raises(AttributeError):
            safe_delattr(Obj(), "__x__")

    def test_restricted_compile_valid(self):
        from ndif.services.ray.nn.security.guards import restricted_compile

        code = restricted_compile("x = 1 + 2", mode="exec")
        assert code is not None

    def test_restricted_exec(self):
        from ndif.services.ray.nn.security.guards import restricted_exec

        result = {}
        restricted_exec("x = 1 + 2", None, result)
        assert result.get("x") == 3

    def test_safe_builtins_has_allowed(self):
        from ndif.services.ray.nn.security.whitelist import SAFE_BUILTINS

        for name in ("print", "len", "range", "list", "dict", "int", "str"):
            assert name in SAFE_BUILTINS, f"{name} missing from SAFE_BUILTINS"

    def test_safe_builtins_missing_dangerous(self):
        from ndif.services.ray.nn.security.whitelist import SAFE_BUILTINS
        from ndif.services.ray.nn.security.guards import (
            restricted_compile,
            restricted_exec,
            safe_getattr,
            safe_setattr,
            safe_delattr,
            safe_hasattr,
        )

        assert "open" not in SAFE_BUILTINS

        # These are present but must be the *restricted/guarded* versions.
        assert SAFE_BUILTINS["compile"] is restricted_compile
        assert SAFE_BUILTINS["exec"] is restricted_exec
        assert SAFE_BUILTINS["getattr"] is safe_getattr
        assert SAFE_BUILTINS["setattr"] is safe_setattr
        assert SAFE_BUILTINS["delattr"] is safe_delattr
        assert SAFE_BUILTINS["hasattr"] is safe_hasattr


# =============================================================================
# DIRECT TESTS — WHITELIST
# =============================================================================


class TestWhitelist:
    """Tests for whitelist configuration and module checking."""

    def test_whitelisted_module_strict(self):
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        strict = WhitelistedModule(name="torch", strict=True)
        assert strict.check("torch") is True
        assert strict.check("torch.nn") is False
        assert strict.check("torchvision") is False

    def test_whitelisted_module_non_strict(self):
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        non_strict = WhitelistedModule(name="torch", strict=False)
        assert non_strict.check("torch") is True
        assert non_strict.check("torch.nn") is True
        assert non_strict.check("torch.nn.functional") is True
        assert non_strict.check("torchvision") is False

    def test_blocked_submodules_loaded(self):
        from ndif.services.ray.nn.security.whitelist import BLOCKED_SUBMODULES

        assert "torch.multiprocessing" in BLOCKED_SUBMODULES
        assert "torch.hub" in BLOCKED_SUBMODULES
        assert "numpy.ctypeslib" in BLOCKED_SUBMODULES

    def test_is_module_blocked(self):
        from ndif.services.ray.nn.security.whitelist import is_module_blocked

        # Exact match
        assert is_module_blocked("torch.multiprocessing") is True
        # Child of blocked
        assert is_module_blocked("torch.multiprocessing.spawn") is True
        assert is_module_blocked("torch.hub.download_url_to_file") is True
        # Not blocked
        assert is_module_blocked("torch.nn") is False
        assert is_module_blocked("torch") is False
        assert is_module_blocked("torch.distributed") is False
        assert is_module_blocked("os") is False

    def test_is_module_allowed(self):
        from ndif.services.ray.nn.security.whitelist import (
            is_module_allowed,
            WHITELISTED_MODULES,
        )

        # Whitelisted and not blocked → allowed
        assert is_module_allowed("torch.nn", WHITELISTED_MODULES) is True
        assert is_module_allowed("numpy", WHITELISTED_MODULES) is True
        assert is_module_allowed("collections", WHITELISTED_MODULES) is True

        # Whitelisted but blocked → NOT allowed
        assert is_module_allowed("torch.multiprocessing", WHITELISTED_MODULES) is False
        assert is_module_allowed("torch.hub", WHITELISTED_MODULES) is False
        assert is_module_allowed("numpy.ctypeslib", WHITELISTED_MODULES) is False

        # Not whitelisted at all → NOT allowed
        assert is_module_allowed("os", WHITELISTED_MODULES) is False
        assert is_module_allowed("subprocess", WHITELISTED_MODULES) is False

    def test_dangerous_modules_not_whitelisted(self):
        from ndif.services.ray.nn.security.whitelist import (
            WHITELISTED_MODULES,
            is_module_whitelisted,
        )

        for name in ("os", "subprocess", "socket", "sys", "importlib", "shutil"):
            assert not is_module_whitelisted(name, WHITELISTED_MODULES), (
                f"{name} should NOT be whitelisted"
            )


# =============================================================================
# DIRECT TESTS — PROTECTED MODULE
# =============================================================================


class TestProtectedModule:
    """Tests for ProtectedModule (lazy getattr, immutability, cross-module blocking)."""

    def test_blocks_cross_module_access(self):
        from ndif.services.ray.nn.security.importer import ProtectedModule
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        fake_torch = ModuleType("torch")
        fake_os = ModuleType("os")
        fake_os.getcwd = lambda: "/tmp"
        fake_torch.os = fake_os
        fake_torch.pi = 3.14

        entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(entry, fake_torch)

        # Non-module attributes pass through
        assert protected.pi == 3.14

        # Cross-module access blocked
        with pytest.raises(AttributeError, match="not whitelisted"):
            _ = protected.os

    def test_allows_submodules(self):
        from ndif.services.ray.nn.security.importer import ProtectedModule
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        fake_torch = ModuleType("torch")
        fake_nn = ModuleType("torch.nn")
        fake_nn.Linear = lambda: None
        fake_torch.nn = fake_nn

        entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(entry, fake_torch)

        nn = protected.nn
        assert nn is not None
        assert isinstance(nn, ProtectedModule)

    def test_immutable(self):
        from ndif.services.ray.nn.security.importer import ProtectedModule
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        fake_torch = ModuleType("torch")
        fake_torch.save = lambda *args: "original"

        entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(entry, fake_torch)

        with pytest.raises(AttributeError, match="Cannot modify protected module"):
            protected.save = lambda *args: "malicious"

        with pytest.raises(AttributeError, match="Cannot modify protected module"):
            del protected.save

    def test_recursive_wrapping(self):
        """Submodules of submodules should also be wrapped."""
        from ndif.services.ray.nn.security.importer import ProtectedModule
        from ndif.services.ray.nn.security.whitelist import WhitelistedModule

        fake_torch = ModuleType("torch")
        fake_nn = ModuleType("torch.nn")
        fake_functional = ModuleType("torch.nn.functional")
        fake_functional.relu = lambda x: x
        fake_nn.functional = fake_functional
        fake_torch.nn = fake_nn

        entry = WhitelistedModule(name="torch", strict=False)
        protected = ProtectedModule(entry, fake_torch)

        # torch.nn → ProtectedModule
        nn = protected.nn
        assert isinstance(nn, ProtectedModule)

        # torch.nn.functional → also ProtectedModule
        func = nn.functional
        assert isinstance(func, ProtectedModule)

        # torch.nn.functional.relu → passes through (not a module)
        assert callable(func.relu)


# =============================================================================
# DIRECT TESTS — DESERIALIZATION
# =============================================================================


class TestDeserialization:
    """Tests for pickle deserialization hardening (subimport, find_class).

    Uses a minimal CustomCloudUnpickler subclass that skips the Envoy/frame
    setup but inherits the Protector's find_class patch.
    """

    def _unpickle(self, data):
        from nnsight.intervention.serialization import CustomCloudUnpickler
        from ndif.services.ray.nn.security import (
            Protector,
            WHITELISTED_MODULES_DESERIALIZATION,
        )

        class TestUnpickler(CustomCloudUnpickler):
            def __init__(self, file):
                pickle.Unpickler.__init__(self, file)

            def load(self):
                return pickle.Unpickler.load(self)

        with Protector(WHITELISTED_MODULES_DESERIALIZATION):
            return TestUnpickler(io.BytesIO(data)).load()

    def test_blocks_os_module(self):
        with pytest.raises(ImportError):
            self._unpickle(cloudpickle.dumps(os))

    def test_blocks_os_listdir(self):
        with pytest.raises(ImportError):
            self._unpickle(cloudpickle.dumps(os.listdir))

    def test_blocks_os_system(self):
        with pytest.raises(ImportError):
            self._unpickle(cloudpickle.dumps(os.system))

    def test_blocks_subprocess_run(self):
        with pytest.raises(ImportError):
            self._unpickle(cloudpickle.dumps(subprocess.run))

    def test_blocks_socket_module(self):
        with pytest.raises(ImportError):
            self._unpickle(cloudpickle.dumps(socket))

    def test_allows_torch(self):
        result = self._unpickle(cloudpickle.dumps(torch.float32))
        assert result is torch.float32

    def test_allows_collections(self):
        import collections

        result = self._unpickle(cloudpickle.dumps(collections.OrderedDict))
        assert result is collections.OrderedDict

    def test_allows_numpy(self):
        result = self._unpickle(cloudpickle.dumps(np.array))
        assert result is np.array

    def test_find_class_cleaned_up(self):
        from nnsight.intervention.serialization import CustomCloudUnpickler
        from ndif.services.ray.nn.security import (
            Protector,
            WHITELISTED_MODULES_DESERIALIZATION,
        )

        with Protector(WHITELISTED_MODULES_DESERIALIZATION):
            assert "find_class" in CustomCloudUnpickler.__dict__

        assert "find_class" not in CustomCloudUnpickler.__dict__


# =============================================================================
# DIRECT TESTS — SANDBOX FINDER (sys.meta_path)
# =============================================================================


class TestSandboxFinder:
    """Tests for the SandboxFinder MetaPathFinder."""

    def test_finder_installed_during_protector(self):
        from ndif.services.ray.nn.security import Protector, WHITELISTED_MODULES
        from ndif.services.ray.nn.security.importer import SandboxFinder

        with Protector(WHITELISTED_MODULES):
            assert any(isinstance(f, SandboxFinder) for f in sys.meta_path)

    def test_finder_removed_after_protector(self):
        from ndif.services.ray.nn.security import Protector, WHITELISTED_MODULES
        from ndif.services.ray.nn.security.importer import SandboxFinder

        with Protector(WHITELISTED_MODULES):
            pass
        assert not any(isinstance(f, SandboxFinder) for f in sys.meta_path)

    def test_finder_returns_none_for_allowed(self):
        from ndif.services.ray.nn.security import WHITELISTED_MODULES
        from ndif.services.ray.nn.security.importer import SandboxFinder

        finder = SandboxFinder(WHITELISTED_MODULES)
        assert finder.find_spec("torch", None) is None
        assert finder.find_spec("torch.nn", None) is None
        assert finder.find_spec("numpy", None) is None

    def test_finder_blocks_non_whitelisted(self):
        from ndif.services.ray.nn.security import WHITELISTED_MODULES
        from ndif.services.ray.nn.security.importer import SandboxFinder

        finder = SandboxFinder(WHITELISTED_MODULES)
        spec = finder.find_spec("os", None)
        assert spec is not None

        spec = finder.find_spec("subprocess", None)
        assert spec is not None

    def test_finder_blocks_blocked_submodules(self):
        from ndif.services.ray.nn.security import WHITELISTED_MODULES
        from ndif.services.ray.nn.security.importer import SandboxFinder

        finder = SandboxFinder(WHITELISTED_MODULES)
        spec = finder.find_spec("torch.multiprocessing", None)
        assert spec is not None

        spec = finder.find_spec("torch.hub", None)
        assert spec is not None


# =============================================================================
# DIRECT TESTS — AUDIT HOOK
# =============================================================================


class TestAuditHook:
    """Tests for the audit hook defense-in-depth layer."""

    def test_flag_disabled_outside_sandbox(self):
        from ndif.services.ray.nn.security.guards import sandbox_active

        assert not getattr(sandbox_active, "enabled", False)

    def test_flag_enabled_inside_sandbox(self):
        from ndif.services.ray.nn.security import (
            Protector,
            WHITELISTED_MODULES,
        )
        from ndif.services.ray.nn.security.guards import sandbox_active

        with Protector(WHITELISTED_MODULES):
            assert getattr(sandbox_active, "enabled", False) is True

    def test_flag_disabled_after_sandbox(self):
        from ndif.services.ray.nn.security import (
            Protector,
            WHITELISTED_MODULES,
        )
        from ndif.services.ray.nn.security.guards import sandbox_active

        with Protector(WHITELISTED_MODULES):
            pass
        assert getattr(sandbox_active, "enabled", False) is False

    def test_blocked_events_defined(self):
        from ndif.services.ray.nn.security.guards import _BLOCKED_AUDIT_EVENTS

        assert "subprocess.Popen" in _BLOCKED_AUDIT_EVENTS
        assert "os.system" in _BLOCKED_AUDIT_EVENTS
        assert "os.fork" in _BLOCKED_AUDIT_EVENTS


# =============================================================================
# DIRECT TESTS — PROTECTED OBJECTS
# =============================================================================


class TestProtectedObjects:
    """Tests for model/tokenizer protection wrappers."""

    def test_blocks_to(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.to("cpu")

    def test_blocks_cuda(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.cuda()

    def test_blocks_cpu(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.cpu()

    def test_blocks_half(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.half()

    def test_blocks_float(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.float()

    def test_blocks_bfloat16(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.bfloat16()

    def test_blocks_requires_grad_(self):
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        with pytest.raises(ValueError, match="cannot be called"):
            wrapped.requires_grad_()

    def test_isinstance_preserved(self):
        """Wrapped object should still be instanceof the original class."""
        from ndif.services.ray.nn.security.protected_objects import protect

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        assert isinstance(wrapped, torch.nn.Module)
        assert isinstance(wrapped, torch.nn.Linear)

    @pytest.mark.xfail(
        reason="Pre-existing bug: clear_set_attrs looks up by original obj id "
               "but SET_ATTRS is keyed by wrapper id",
        strict=True,
    )
    def test_clear_set_attrs(self):
        from ndif.services.ray.nn.security.protected_objects import (
            protect,
            clear_set_attrs,
        )

        module = torch.nn.Linear(10, 10)
        wrapped = protect(module)

        original_training = module.training
        wrapped.training = not original_training
        assert module.training != original_training

        clear_set_attrs()
        assert module.training == original_training


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
