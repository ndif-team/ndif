"""Protected execution environment for NDIF.

This module provides security restrictions for executing untrusted user code:
- Import restrictions: Only whitelisted modules can be imported
- Builtin restrictions: Only whitelisted builtins are available
- Module immutability: Imported modules cannot be modified

Usage:
    with Protector(WHITELISTED_MODULES):
        # User code runs here with restrictions applied
        exec(user_code)
"""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

import yaml
from pydantic import BaseModel
from RestrictedPython.Guards import (
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
)
from RestrictedPython.Eval import (
    default_guarded_getitem,
    default_guarded_getiter,
)

from nnsight.util import Patch, Patcher
from nnsight.modeling.mixins.remoteable import StreamTracer

# =============================================================================
# WHITELIST CONFIGURATION LOADING
# =============================================================================

WHITELIST_PATH = Path(__file__).parent / "whitelist.yaml"


def _load_whitelist() -> dict:
    """Load the whitelist configuration from YAML file."""
    with open(WHITELIST_PATH, "r") as f:
        return yaml.safe_load(f)


# Load configuration at module import time
_WHITELIST_CONFIG = _load_whitelist()

# Built-in functions and types that are allowed to be used
WHITELISTED_BUILTINS = set(_WHITELIST_CONFIG.get("builtins", []))

SAFE_BUILTINS = {
    key: value for key, value in __builtins__.items() if key in WHITELISTED_BUILTINS
}


class SafeBuiltins(ModuleType):
    """A wrapper around the built-in module that enforces whitelist rules."""

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


class WhitelistedModule(BaseModel):
    """Configuration for a module that is allowed to be imported."""

    name: str
    strict: bool = True

    def check(self, name: str) -> bool:
        if self.strict:
            return self.name == name
        return self.name == name or name.startswith(self.name + ".")


def _load_modules(config_key: str) -> List[WhitelistedModule]:
    """Load module whitelist from config."""
    modules = _WHITELIST_CONFIG.get(config_key, [])
    return [WhitelistedModule(**m) for m in modules]


# Modules that are allowed to be imported
WHITELISTED_MODULES = _load_modules("modules")

# Modules allowed during deserialization (includes regular modules)
WHITELISTED_MODULES_DESERIALIZATION = [
    *_load_modules("deserialization_modules"),
    *WHITELISTED_MODULES,
]


class ProtectedModule(ModuleType):
    """A wrapper around a module that enforces whitelist rules.

    This class:
    1. Blocks access to non-whitelisted submodules (e.g., torch.os)
    2. Prevents modification of module attributes (immutable)
    3. Prevents deletion of module attributes
    """

    # Track if we're in __init__ to allow initial setup
    _initializing = False

    def __init__(self, whitelist_entry: WhitelistedModule):
        object.__setattr__(self, "_initializing", True)
        super().__init__(whitelist_entry.name)
        object.__setattr__(self, "whitelist_entry", whitelist_entry)
        object.__setattr__(self, "_initializing", False)

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)

        if not isinstance(attr, ModuleType):
            return attr

        if self.whitelist_entry.strict:
            if self.__name__ != attr.__name__:
                raise AttributeError(
                    f"Module attribute {attr.__name__} is not whitelisted"
                )
        elif not attr.__name__.startswith(self.__name__ + "."):
            raise AttributeError(f"Module attribute {attr.__name__} is not whitelisted")

        protected = ProtectedModule(self.whitelist_entry)
        protected.__dict__.update(attr.__dict__)
        return protected

    def __setattr__(self, name: str, value: Any) -> None:
        """Prevent modification of module attributes."""
        # Allow setting during initialization and for __dict__ updates
        if object.__getattribute__(self, "_initializing"):
            object.__setattr__(self, name, value)
            return
        raise AttributeError(
            f"Cannot modify protected module: setting '{name}' is not allowed"
        )

    def __delattr__(self, name: str) -> None:
        """Prevent deletion of module attributes."""
        raise AttributeError(
            f"Cannot modify protected module: deleting '{name}' is not allowed"
        )


class Importer:
    """Handles importing modules while enforcing whitelist rules."""

    def __init__(
        self, whitelisted_modules: List[WhitelistedModule], protector: "Protector"
    ):
        self.whitelisted_modules = whitelisted_modules
        self.protector = protector
        self.original_import = __builtins__["__import__"]

    def __call__(
        self,
        name: str,
        globals: Dict[str, Any] = None,
        locals: Dict[str, Any] = None,
        fromlist: List[str] = None,
        level: int = 0,
    ):
        if name in ["builtins", "__builtins__"]:
            return PROTECTED_BUILTINS

        if level > 0:
            self.protector.__exit__(None, None, None)

            try:
                result = self.original_import(name, globals, locals, fromlist, level)

            finally:
                self.protector.__enter__()

            for module in self.whitelisted_modules:
                if module.check(result.__name__):
                    protected = ProtectedModule(module)
                    protected.__dict__.update(result.__dict__)
                    return protected

            raise ImportError(f"Module {result.__name__} is not whitelisted")

        for module in self.whitelisted_modules:
            if module.check(name):
                self.protector.__exit__(None, None, None)

                try:
                    result = self.original_import(
                        name, globals, locals, fromlist, level
                    )
                    protected = ProtectedModule(module)
                    protected.__dict__.update(result.__dict__)
                    return protected
                finally:
                    self.protector.__enter__()

        raise ImportError(f"Module {name} is not whitelisted")


class Protector(Patcher):
    """Enforces security restrictions on Python's built-ins and imports."""

    def __init__(
        self,
        whitelisted_modules: List[WhitelistedModule],
        builtins: bool = False,
        restrict_compile: bool = True,
    ):
        super().__init__()
        self.importer = Importer(whitelisted_modules, self)

        # Patch __import__ to use our custom importer
        self.add(
            Patch(
                __builtins__,
                replacement=self.importer.__call__,
                key="__import__",
                as_dict=True,
            )
        )

        self.add(
            Patch(
                SAFE_BUILTINS,
                replacement=self.importer.__call__,
                key="__import__",
                as_dict=True,
            )
        )

        self.add(
            Patch(
                StreamTracer,
                replacement=self.escape(StreamTracer.execute),
                key="execute",
            )
        )

        # Patch compile and exec to use restricted versions
        if restrict_compile:
            self.add(
                Patch(
                    __builtins__,
                    replacement=restricted_compile,
                    key="compile",
                    as_dict=True,
                )
            )
            self.add(
                Patch(
                    __builtins__,
                    replacement=restricted_exec,
                    key="exec",
                    as_dict=True,
                )
            )
            # Add restricted versions directly to SAFE_BUILTINS
            # (they don't exist there originally, so we can't use Patch)
            SAFE_BUILTINS["compile"] = restricted_compile
            SAFE_BUILTINS["exec"] = restricted_exec

        # Remove non-whitelisted built-ins
        if builtins:
            for key in __builtins__.keys():
                if key not in WHITELISTED_BUILTINS:
                    self.add(Patch(__builtins__, key=key, as_dict=True))

    def escape(self, fn: Callable):
        @wraps(fn)
        def inner(*args, **kwargs):
            self.__exit__(None, None, None)
            try:
                return fn(*args, **kwargs)
            finally:
                self.__enter__()

        return inner


# =============================================================================
# RESTRICTED COMPILE / EXEC
# =============================================================================

# Dunder attributes loaded from whitelist.yaml
BLOCKED_DUNDER_ATTRS = frozenset(_WHITELIST_CONFIG.get("blocked_dunder_attrs", []))
ALLOWED_DUNDER_ATTRS = frozenset(_WHITELIST_CONFIG.get("allowed_dunder_attrs", []))


def guarded_getattr(obj: Any, name: str, default: Any = None) -> Any:
    """Guard for attribute access that blocks dangerous dunder attributes.

    This is injected as `_getattr_` into the execution globals when running
    restricted code. RestrictedPython transforms `obj.attr` into
    `_getattr_(obj, 'attr')` calls.

    Args:
        obj: The object to get the attribute from.
        name: The attribute name.
        default: Default value if attribute doesn't exist.

    Returns:
        The attribute value.

    Raises:
        AttributeError: If the attribute is blocked.
    """
    if name.startswith("_"):
        # Allow explicitly safe dunder methods
        if name in ALLOWED_DUNDER_ATTRS:
            pass
        # Block explicitly dangerous ones
        elif name in BLOCKED_DUNDER_ATTRS:
            raise AttributeError(
                f"Access to '{name}' is not allowed in restricted code"
            )
        # Block all other underscore-prefixed attributes (private/dunder)
        else:
            raise AttributeError(
                f"Access to private attribute '{name}' is not allowed in restricted code"
            )

    if default is None:
        return getattr(obj, name)
    return getattr(obj, name, default)


def guarded_setattr(obj: Any, name: str, value: Any) -> None:
    """Guard for attribute setting that blocks dangerous dunder attributes.

    Injected as `_write_` guard. RestrictedPython transforms `obj.attr = val`
    into guarded calls.
    """
    if name.startswith("_"):
        if name in BLOCKED_DUNDER_ATTRS or name.startswith("__"):
            raise AttributeError(f"Setting '{name}' is not allowed in restricted code")
    setattr(obj, name, value)


def guarded_delattr(obj: Any, name: str) -> None:
    """Guard for attribute deletion that blocks dangerous dunder attributes."""
    if name.startswith("_"):
        if name in BLOCKED_DUNDER_ATTRS or name.startswith("__"):
            raise AttributeError(f"Deleting '{name}' is not allowed in restricted code")
    delattr(obj, name)


class GuardedWrite:
    """Guard class that wraps objects for guarded attribute writes.

    RestrictedPython calls `_write_(obj)` before attribute assignment.
    This returns a wrapper that intercepts `__setattr__` and `__delattr__`.
    """

    def __init__(self, obj: Any):
        object.__setattr__(self, "_guarded_obj", obj)

    def __setattr__(self, name: str, value: Any) -> None:
        obj = object.__getattribute__(self, "_guarded_obj")
        guarded_setattr(obj, name, value)

    def __delattr__(self, name: str) -> None:
        obj = object.__getattribute__(self, "_guarded_obj")
        guarded_delattr(obj, name)


def _write_(obj: Any) -> GuardedWrite:
    """Return a guarded wrapper for write operations."""
    return GuardedWrite(obj)


def _inplacevar_(op: str, x: Any, y: Any) -> Any:
    """Handle in-place operations like +=, -=, etc.

    RestrictedPython transforms `x += y` into `x = _inplacevar_('+=', x, y)`.
    """
    op_map = {
        "+=": lambda a, b: a + b,
        "-=": lambda a, b: a - b,
        "*=": lambda a, b: a * b,
        "/=": lambda a, b: a / b,
        "//=": lambda a, b: a // b,
        "%=": lambda a, b: a % b,
        "**=": lambda a, b: a**b,
        "&=": lambda a, b: a & b,
        "|=": lambda a, b: a | b,
        "^=": lambda a, b: a ^ b,
        "<<=": lambda a, b: a << b,
        ">>=": lambda a, b: a >> b,
        "@=": lambda a, b: a @ b,
    }
    if op not in op_map:
        raise ValueError(f"Unknown in-place operator: {op}")
    return op_map[op](x, y)


# Store reference to original compile before we patch it
# This prevents infinite recursion when restricted_compile calls compile
_original_compile = compile


def restricted_compile(
    source: str,
    filename: str = "<restricted>",
    mode: str = "exec",
    flags: int = 0,
    dont_inherit: bool = False,
    optimize: int = -1,
) -> Any:
    """Compile source code for execution in a restricted environment.

    This uses the original Python compile(). Security is enforced through:
    1. The Protector context which restricts imports to whitelisted modules
    2. The Protector context which restricts builtins to safe ones
    3. The guarded execution globals (guarded_getattr blocks dangerous dunders)

    Note: We use standard compile instead of RestrictedPython's AST transformation
    because RestrictedPython blocks nnsight's internal variable names
    (like __nnsight_tracer_*). Security is still enforced at runtime.

    Args:
        source: The source code to compile.
        filename: Filename for error messages.
        mode: Compile mode - 'exec', 'eval', or 'single'.
        flags: Compiler flags (passed through).
        dont_inherit: Don't inherit flags from calling code.
        optimize: Optimization level (-1 for default).

    Returns:
        A compiled code object.

    Raises:
        SyntaxError: If there are syntax errors in the source.
    """
    return _original_compile(source, filename, mode, flags, dont_inherit, optimize)


def make_restricted_globals(
    base_globals: Optional[Dict[str, Any]] = None,
    builtins: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a globals dict with all required guards for restricted execution.

    When executing code compiled with `restricted_compile`, the execution globals
    must contain the guard functions that RestrictedPython's AST transformations
    expect. This function creates a properly configured globals dict.

    Args:
        base_globals: Optional base globals to include (e.g., user-provided values).
        builtins: Optional custom builtins dict. Defaults to SAFE_BUILTINS.

    Returns:
        A globals dict ready for use with exec() on restricted-compiled code.

    Example:
        >>> code = restricted_compile("result = obj.value + 1", mode="exec")
        >>> globs = make_restricted_globals({"obj": my_obj})
        >>> exec(code, globs)
        >>> print(globs["result"])
    """
    globs = dict(base_globals) if base_globals else {}

    # Set builtins
    globs["__builtins__"] = builtins if builtins is not None else SAFE_BUILTINS

    # Guard functions required by RestrictedPython's AST transformations
    globs["_getattr_"] = guarded_getattr
    globs["_getitem_"] = default_guarded_getitem
    globs["_getiter_"] = default_guarded_getiter
    globs["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    globs["_unpack_sequence_"] = guarded_unpack_sequence
    globs["_write_"] = _write_
    globs["_inplacevar_"] = _inplacevar_

    return globs


def restricted_exec(
    code: Any,
    globals: Optional[Dict[str, Any]] = None,
    locals: Optional[Dict[str, Any]] = None,
) -> None:
    """Execute code in a restricted environment.

    If the code is a string, it will be compiled with restricted_compile first.
    The globals are automatically augmented with the required guard functions.

    Args:
        code: Either a code object (from restricted_compile) or source string.
        globals: Optional globals dict. Will be augmented with guards.
        locals: Optional locals dict.
    """
    if isinstance(code, str):
        code = restricted_compile(code, mode="exec")

    exec_globals = make_restricted_globals(globals)
    exec(code, exec_globals, locals)
