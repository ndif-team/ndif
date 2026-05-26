"""Protector — the top-level sandbox context manager.

    with Protector(WHITELISTED_MODULES):
        exec(user_code)   # imports, builtins, compile, exec, deserialization,
                          # and audit hooks are all restricted here

The Protector works by stacking monkey-patches (via nnsight's Patcher) that
are applied on ``__enter__`` and reversed on ``__exit__``.

Security layers:

    1. Import interception  (Importer — patches __import__)
    2. Meta-path finder     (SandboxFinder — defense-in-depth in sys.meta_path)
    3. Deserialization       (subimport + dynamic_subimport + find_class patches)
    4. Builtin restriction   (optional — strips non-whitelisted builtins)
    5. Compile / exec        (restricted versions that inject guards)
    6. Audit hook            (blocks subprocess, os.system, socket, … at C level)
"""

from __future__ import annotations

import pickle
import sys
from functools import wraps
from typing import Callable, List

import cloudpickle.cloudpickle as _cloudpickle_module
from nnsight.util import Patch, Patcher
from nnsight.modeling.mixins.remoteable import StreamTracer
from nnsight.intervention.serialization import CustomCloudUnpickler

from .whitelist import (
    WhitelistedModule,
    WHITELISTED_BUILTINS,
    SAFE_BUILTINS,
    is_module_allowed,
)
from .importer import Importer, SandboxFinder
from .guards import (
    restricted_compile,
    restricted_exec,
    sandbox_active,
    install_audit_hook,
)


class Protector(Patcher):

    def __init__(
        self,
        whitelisted_modules: List[WhitelistedModule],
        builtins: bool = False,
        restrict_compile: bool = True,
    ):
        super().__init__()
        self._whitelisted_modules = whitelisted_modules
        self.importer = Importer(whitelisted_modules, self)
        self._finder = SandboxFinder(whitelisted_modules)

        # Layer 1 — redirect __import__ to our Importer (in both the real
        # builtins dict and the SAFE_BUILTINS dict used during exec).
        self.add(Patch(__builtins__, replacement=self.importer.__call__,
                       key="__import__", as_dict=True))
        self.add(Patch(SAFE_BUILTINS, replacement=self.importer.__call__,
                       key="__import__", as_dict=True))

        # Layer 3a — patch cloudpickle.subimport so it can't bypass the
        # Importer via sys.modules.
        _original_subimport = _cloudpickle_module.subimport

        def _secure_subimport(name):
            if not is_module_allowed(name, whitelisted_modules):
                raise ImportError(f"Module {name} is not whitelisted")
            return _original_subimport(name)

        self.add(Patch(_cloudpickle_module, replacement=_secure_subimport,
                       key="subimport"))

        # Layer 3b — patch cloudpickle.dynamic_subimport (creates modules
        # from a vars dict — same bypass vector as subimport).
        _original_dynamic_subimport = _cloudpickle_module.dynamic_subimport

        def _secure_dynamic_subimport(name, vars):
            if not is_module_allowed(name, whitelisted_modules):
                raise ImportError(f"Module {name} is not whitelisted")
            return _original_dynamic_subimport(name, vars)

        self.add(Patch(_cloudpickle_module, replacement=_secure_dynamic_subimport,
                       key="dynamic_subimport"))

        # nnsight's StreamTracer.execute must run *outside* the sandbox so
        # nnsight internals can use unrestricted imports.
        self.add(Patch(StreamTracer, replacement=self.escape(StreamTracer.execute),
                       key="execute"))

        # Layer 5 — replace compile/exec in __builtins__ with restricted versions.
        if restrict_compile:
            self.add(Patch(__builtins__, replacement=restricted_compile,
                           key="compile", as_dict=True))
            self.add(Patch(__builtins__, replacement=restricted_exec,
                           key="exec", as_dict=True))

        # Layer 4 — strip non-whitelisted builtins entirely.
        if builtins:
            for key in __builtins__.keys():
                if key not in WHITELISTED_BUILTINS:
                    self.add(Patch(__builtins__, key=key, as_dict=True))

        # Layer 6 — install the audit hook (once per process).
        install_audit_hook()

    # Layer 2 — sys.meta_path finder + Layer 3c — find_class patch.
    #
    # These can't be Patch objects:
    #   - meta_path is a list, not a dict/attribute
    #   - find_class must be set/deleted on a Python subclass of a C type
    #
    # So we manage them manually in __enter__/__exit__.

    def __enter__(self):
        result = super().__enter__()

        # Layer 2 — insert the SandboxFinder at the front of sys.meta_path.
        sys.meta_path.insert(0, self._finder)

        # Layer 3c — patch find_class on CustomCloudUnpickler.
        # pickle.Unpickler.find_class() calls __import__ then reads
        # sys.modules[module], ignoring the Importer's return value.
        whitelisted = self._whitelisted_modules
        original_import = self.importer.original_import

        def _secure_find_class(unpickler_self, module, name):
            if not is_module_allowed(module, whitelisted):
                raise ImportError(f"Module {module} is not whitelisted")
            original_import(module, level=0)
            # _getattribute handles dotted names like "Tracer.Info".
            return pickle._getattribute(sys.modules[module], name)[0]

        CustomCloudUnpickler.find_class = _secure_find_class

        # Layer 6 — enable the audit hook for this thread.
        sandbox_active.enabled = True

        return result

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Layer 6 — disable the audit hook for this thread.
        sandbox_active.enabled = False

        # Layer 3c — remove find_class override.
        if "find_class" in CustomCloudUnpickler.__dict__:
            delattr(CustomCloudUnpickler, "find_class")

        # Layer 2 — remove the SandboxFinder from sys.meta_path.
        try:
            sys.meta_path.remove(self._finder)
        except ValueError:
            pass

        return super().__exit__(exc_type, exc_val, exc_tb)

    def escape(self, fn: Callable):
        """Wrap *fn* so it runs with all sandbox patches temporarily removed."""

        @wraps(fn)
        def inner(*args, **kwargs):
            self.__exit__(None, None, None)
            try:
                return fn(*args, **kwargs)
            finally:
                self.__enter__()

        return inner
