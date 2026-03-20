"""Import interception for the NDIF sandbox.

Two layers of defense:

1. **Importer** — replaces ``__builtins__["__import__"]``.  Every ``import``
   statement in user code (and in pickle during deserialization) goes through
   here.  Returns UnauthorizedModule for blocked imports, ProtectedModule for
   allowed ones.

2. **SandboxFinder** — inserted into ``sys.meta_path``.  Catches imports that
   bypass ``__import__`` entirely (e.g. ``importlib._bootstrap._find_and_load``
   called from C code).  Defense-in-depth: if something slips past the
   Importer, the finder blocks it.

Both layers check the whitelist AND the blocked-submodule list.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import Any, Dict, List, TYPE_CHECKING

from .whitelist import WhitelistedModule, SAFE_BUILTINS, is_module_allowed

if TYPE_CHECKING:
    from .protector import Protector


# ── SafeBuiltins ──────────────────────────────────────────────────────────────
# Returned when user code does ``import builtins``.  Attribute access is
# redirected to SAFE_BUILTINS so only whitelisted builtins are visible.


class SafeBuiltins(ModuleType):

    def __init__(self):
        super().__init__("safe_builtins")

    def __getattribute__(self, name: str):
        try:
            return SAFE_BUILTINS[name]
        except KeyError:
            raise AttributeError(f"module 'builtins' has no attribute '{name}'")

    def __getitem__(self, name: str):
        return SAFE_BUILTINS[name]


PROTECTED_BUILTINS = SafeBuiltins()


# ── UnauthorizedModule ────────────────────────────────────────────────────────
# Returned for non-whitelisted imports.  We don't raise immediately because
# some code paths import a module speculatively and catch ImportError.  By
# deferring the error to first *use*, we match that pattern while still
# blocking any actual access.


class UnauthorizedModule:

    def __init__(self, name: str):
        object.__setattr__(self, "_module_name", name)

    def _raise_error(self) -> None:
        name = object.__getattribute__(self, "_module_name")
        raise ImportError(f"Module {name} is not whitelisted")

    def __getattribute__(self, name: str) -> Any:
        if name in ("_module_name", "_raise_error"):
            return object.__getattribute__(self, name)
        object.__getattribute__(self, "_raise_error")()

    # Every other interaction triggers the same error.
    def __setattr__(self, n: str, v: Any) -> None:  self._raise_error()
    def __delattr__(self, n: str) -> None:           self._raise_error()
    def __call__(self, *a: Any, **kw: Any) -> Any:   self._raise_error()
    def __iter__(self) -> Any:                        self._raise_error()
    def __next__(self) -> Any:                        self._raise_error()
    def __len__(self) -> int:                         self._raise_error()
    def __getitem__(self, k: Any) -> Any:             self._raise_error()
    def __setitem__(self, k: Any, v: Any) -> None:    self._raise_error()
    def __delitem__(self, k: Any) -> None:            self._raise_error()
    def __contains__(self, i: Any) -> bool:           self._raise_error()
    def __bool__(self) -> bool:                       self._raise_error()

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_module_name")
        return f"<UnauthorizedModule '{name}'>"


# ── ProtectedModule ──────────────────────────────────────────────────────────
# Wraps whitelisted modules.  Uses lazy __getattr__ so every attribute access
# is checked — cross-module access (e.g. torch.os) is blocked, and the module
# is immutable (no setattr/delattr).


class ProtectedModule(ModuleType):

    def __init__(self, whitelist_entry: WhitelistedModule, real_module: ModuleType):
        # Use object.__setattr__ to bypass our own __setattr__ guard.
        object.__setattr__(self, "_whitelist_entry", whitelist_entry)
        object.__setattr__(self, "_real_module", real_module)
        # ModuleType needs __name__ for repr, etc.
        super().__init__(real_module.__name__)

    def __getattr__(self, name: str):
        """Lazy attribute lookup — delegates to the real module, wrapping
        submodule results in a new ProtectedModule."""
        real = object.__getattribute__(self, "_real_module")
        entry = object.__getattribute__(self, "_whitelist_entry")

        attr = getattr(real, name)

        # Non-module attributes (functions, classes, constants) pass through.
        if not isinstance(attr, ModuleType):
            return attr

        # Module attributes must be legitimate submodules, not unrelated
        # modules that happen to be reachable (e.g. torch.os).
        if entry.strict:
            if real.__name__ != attr.__name__:
                raise AttributeError(
                    f"Module attribute {attr.__name__} is not whitelisted"
                )
        elif not attr.__name__.startswith(real.__name__ + "."):
            raise AttributeError(
                f"Module attribute {attr.__name__} is not whitelisted"
            )

        # Wrap the submodule so the same rules apply recursively.
        return ProtectedModule(entry, attr)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"Cannot modify protected module: setting '{name}' is not allowed"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            f"Cannot modify protected module: deleting '{name}' is not allowed"
        )


# ── SandboxFinder (sys.meta_path) ────────────────────────────────────────────
# Defense-in-depth: blocks imports that bypass __import__ (e.g. importlib
# internals called from C).  Installed in sys.meta_path by the Protector.


class _BlockedLoader:
    """Loader that raises ImportError during exec_module."""

    def __init__(self, name: str):
        self.name = name

    def create_module(self, spec):
        return None  # Let the import system create a default module.

    def exec_module(self, module):
        # Remove the half-initialized module and raise.
        sys.modules.pop(self.name, None)
        raise ImportError(f"Module {self.name} is not whitelisted")


class SandboxFinder:
    """MetaPathFinder that blocks non-whitelisted / blocked-submodule imports.

    Inserted at position 0 in sys.meta_path so it is checked first.
    Returns None for allowed modules (letting normal finders handle them)
    and a blocking ModuleSpec for everything else.
    """

    def __init__(self, whitelist: List[WhitelistedModule]):
        self.whitelist = whitelist

    def find_module(self, fullname, path=None):
        # Legacy protocol — defer to find_spec.
        return None

    def find_spec(self, fullname, path, target=None):
        if is_module_allowed(fullname, self.whitelist):
            return None  # Allowed — let normal import machinery handle it.

        # Blocked — return a spec whose loader will raise ImportError.
        return importlib.util.spec_from_loader(
            fullname, _BlockedLoader(fullname)
        )


# ── Importer ──────────────────────────────────────────────────────────────────


class Importer:
    """Replaces ``__import__`` while the Protector is active.

    For whitelisted modules the Importer must temporarily *exit* the Protector
    so the real import machinery runs without interference (it may trigger
    nested imports of its own).  After the import succeeds the Protector is
    re-entered and the result is wrapped in a ProtectedModule.
    """

    def __init__(
        self,
        whitelisted_modules: List[WhitelistedModule],
        protector: Protector,
    ):
        self.whitelisted_modules = whitelisted_modules
        self.protector = protector
        self.original_import = __builtins__["__import__"]

    def _do_import(self, name, globals, locals, fromlist, level):
        """Run the real import with the Protector temporarily disabled."""
        self.protector.__exit__(None, None, None)
        try:
            return self.original_import(name, globals, locals, fromlist, level)
        finally:
            self.protector.__enter__()

    def _find_entry(self, name: str):
        """If *name* is allowed, return the WhitelistedModule entry for it."""
        for module in self.whitelisted_modules:
            if module.check(name):
                return module
        return None

    def __call__(
        self,
        name: str,
        globals: Dict[str, Any] = None,
        locals: Dict[str, Any] = None,
        fromlist: List[str] = None,
        level: int = 0,
    ):
        if name in ("builtins", "__builtins__"):
            return PROTECTED_BUILTINS

        # ── Relative imports (level > 0) ──────────────────────────────────
        # We must let the real import resolve the relative path first, then
        # decide whether the resolved module name is allowed.
        if level > 0:
            result = self._do_import(name, globals, locals, fromlist, level)
            if is_module_allowed(result.__name__, self.whitelisted_modules):
                entry = self._find_entry(result.__name__)
                return ProtectedModule(entry, result)
            return UnauthorizedModule(result.__name__)

        # ── Absolute imports ──────────────────────────────────────────────
        if is_module_allowed(name, self.whitelisted_modules):
            entry = self._find_entry(name)
            result = self._do_import(name, globals, locals, fromlist, level)
            return ProtectedModule(entry, result)

        return UnauthorizedModule(name)
