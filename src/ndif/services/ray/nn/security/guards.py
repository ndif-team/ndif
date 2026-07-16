"""Runtime execution guards for the NDIF sandbox.

Two kinds of guard:

1. **Attribute guards** — ``guarded_getattr``, ``guarded_setattr``, etc.
   Block access to dangerous dunders (__class__, __globals__, …) while
   allowing safe ones (__len__, __add__, …).  Injected in two ways:

     a) Into ``SAFE_BUILTINS`` so ``getattr(obj, '__class__')`` is caught
        even without AST transformation.
     b) Into ``make_restricted_globals()`` as ``_getattr_`` etc. for
        RestrictedPython's AST-transformed code (if used in the future).

2. **Restricted compile / exec** — replacements for the ``compile`` and
   ``exec`` builtins that inject attribute guards into the execution env.

3. **Audit hook** — ``sys.addaudithook`` defense-in-depth that blocks
   dangerous syscall-level operations (subprocess, os.system, socket, …)
   when the sandbox is active.  Permanent per-process; controlled by a
   threading-local flag that the Protector toggles on enter/exit.
"""

from __future__ import annotations

import ast
import sys
import threading
from typing import Any, Dict, Optional

from RestrictedPython.Guards import (
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
)
from RestrictedPython.Eval import (
    default_guarded_getitem,
    default_guarded_getiter,
)

from .whitelist import BLOCKED_DUNDER_ATTRS, ALLOWED_DUNDER_ATTRS, SAFE_BUILTINS

# Keep a reference to the real compile/exec before the Protector can patch
# __builtins__.  restricted_compile and restricted_exec delegate to these.
_original_compile = compile
_original_exec = exec

# Some libraries (e.g. cloudpickle) read ast.compile internally.  Stash the
# real compile on the ast module so it survives the builtins patch.
setattr(ast, "compile", compile)


# ── Dunder check ──────────────────────────────────────────────────────────────
# Factored out so it can be reused by both guarded_getattr and safe_getattr.


def _check_dunder(name: str) -> None:
    """Raise AttributeError if *name* is a blocked private/dunder attribute."""
    if not name.startswith("_"):
        return
    if name in ALLOWED_DUNDER_ATTRS:
        return
    if name in BLOCKED_DUNDER_ATTRS:
        raise AttributeError(
            f"Access to '{name}' is not allowed in restricted code"
        )
    raise AttributeError(
        f"Access to private attribute '{name}' is not allowed in restricted code"
    )


# ── Attribute guards ──────────────────────────────────────────────────────────


def guarded_getattr(obj: Any, name: str, default: Any = None) -> Any:
    """Block access to dangerous dunder attributes.

    Used as ``_getattr_`` in RestrictedPython's AST-transformed code.
    """
    _check_dunder(name)
    if default is None:
        return getattr(obj, name)
    return getattr(obj, name, default)


def guarded_setattr(obj: Any, name: str, value: Any) -> None:
    if name.startswith("_"):
        if name in BLOCKED_DUNDER_ATTRS or name.startswith("__"):
            raise AttributeError(
                f"Setting '{name}' is not allowed in restricted code"
            )
    setattr(obj, name, value)


def guarded_delattr(obj: Any, name: str) -> None:
    if name.startswith("_"):
        if name in BLOCKED_DUNDER_ATTRS or name.startswith("__"):
            raise AttributeError(
                f"Deleting '{name}' is not allowed in restricted code"
            )
    delattr(obj, name)


class GuardedWrite:
    """Wrapper returned by ``_write_(obj)`` that intercepts ``obj.attr = …``."""

    def __init__(self, obj: Any):
        object.__setattr__(self, "_guarded_obj", obj)

    def __setattr__(self, name: str, value: Any) -> None:
        guarded_setattr(object.__getattribute__(self, "_guarded_obj"), name, value)

    def __delattr__(self, name: str) -> None:
        guarded_delattr(object.__getattribute__(self, "_guarded_obj"), name)


def _write_(obj: Any) -> GuardedWrite:
    return GuardedWrite(obj)


def _inplacevar_(op: str, x: Any, y: Any) -> Any:
    """Simulate in-place operations (``x += y`` → ``x = _inplacevar_('+=', x, y)``)."""
    ops = {
        "+=": lambda a, b: a + b,  "-=": lambda a, b: a - b,
        "*=": lambda a, b: a * b,  "/=": lambda a, b: a / b,
        "//=": lambda a, b: a // b, "%=": lambda a, b: a % b,
        "**=": lambda a, b: a**b,  "&=": lambda a, b: a & b,
        "|=": lambda a, b: a | b,  "^=": lambda a, b: a ^ b,
        "<<=": lambda a, b: a << b, ">>=": lambda a, b: a >> b,
        "@=": lambda a, b: a @ b,
    }
    if op not in ops:
        raise ValueError(f"Unknown in-place operator: {op}")
    return ops[op](x, y)


# ── Safe builtin wrappers ────────────────────────────────────────────────────
# Replace getattr/setattr/delattr/hasattr in SAFE_BUILTINS so that dunder
# restrictions are enforced even without RestrictedPython AST transformation.

_MISSING = object()  # Sentinel to distinguish "no default" from "default is None".


def safe_getattr(obj: Any, name: str, default: Any = _MISSING) -> Any:
    """getattr() replacement that enforces dunder restrictions."""
    _check_dunder(name)
    if default is _MISSING:
        return getattr(obj, name)
    return getattr(obj, name, default)


def safe_setattr(obj: Any, name: str, value: Any) -> None:
    """setattr() replacement that enforces dunder restrictions."""
    guarded_setattr(obj, name, value)


def safe_delattr(obj: Any, name: str) -> None:
    """delattr() replacement that enforces dunder restrictions."""
    guarded_delattr(obj, name)


def safe_hasattr(obj: Any, name: str) -> bool:
    """hasattr() replacement — uses safe_getattr so blocked dunders return False."""
    try:
        safe_getattr(obj, name)
        return True
    except AttributeError:
        return False


# ── Restricted compile / exec ─────────────────────────────────────────────────


def restricted_compile(
    source: str,
    filename: str = "<restricted>",
    mode: str = "exec",
    flags: int = 0,
    dont_inherit: bool = False,
    optimize: int = -1,
) -> Any:
    """Safe compile() — delegates to the real compile.  Security comes from
    the Protector's import/builtin restrictions plus the attribute guards
    injected by ``make_restricted_globals``."""
    return _original_compile(source, filename, mode, flags, dont_inherit, optimize)


def make_restricted_globals(
    base_globals: Optional[Dict[str, Any]] = None,
    builtins: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a globals dict that contains all the guard hooks RestrictedPython
    expects (``_getattr_``, ``_write_``, etc.)."""
    globs = dict(base_globals) if base_globals else {}
    globs["__builtins__"] = builtins if builtins is not None else SAFE_BUILTINS
    globs["_getattr_"] = guarded_getattr
    globs["_getitem_"] = default_guarded_getitem
    globs["_getiter_"] = default_guarded_getiter
    globs["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    globs["_unpack_sequence_"] = guarded_unpack_sequence
    globs["_write_"] = _write_
    globs["_inplacevar_"] = _inplacevar_
    return globs


_GUARD_KEYS = frozenset(make_restricted_globals())


def restricted_exec(
    code: Any,
    globals: Optional[Dict[str, Any]] = None,
    locals: Optional[Dict[str, Any]] = None,
) -> None:
    """Safe exec() — injects attribute guards then delegates to the real exec.
    New variables defined by the executed code are synced back to *globals*
    (minus the guard keys) so the caller can read them."""
    if isinstance(code, str):
        code = restricted_compile(code, mode="exec")

    # If Python's import machinery is calling exec() to run a module's body
    # (e.g. einops/__init__.py while resolving an import), pass the module's
    # __dict__ through unchanged. Otherwise module-level definitions land in
    # a throwaway copy and only get synced back AFTER the exec completes —
    # which breaks any module that references its own attributes during
    # initialization (e.g. einops, whose submodules do `from . import
    # EinopsError` from within the package init).
    # `__spec__` is set on every module dict by the loader before exec_module
    # runs, so it's a reliable signal that `globals` is a real module dict.
    if isinstance(globals, dict) and "__spec__" in globals:
        _original_exec(code, globals, locals)
        return

    exec_globals = make_restricted_globals(globals)
    _original_exec(code, exec_globals, locals)

    if globals is not None:
        for key, value in exec_globals.items():
            if key not in _GUARD_KEYS:
                globals[key] = value


# ── Register safe builtins ────────────────────────────────────────────────────
# Replace dangerous builtins with guarded versions in SAFE_BUILTINS.

SAFE_BUILTINS["compile"] = restricted_compile
SAFE_BUILTINS["exec"] = restricted_exec
SAFE_BUILTINS["getattr"] = safe_getattr
SAFE_BUILTINS["setattr"] = safe_setattr
SAFE_BUILTINS["delattr"] = safe_delattr
SAFE_BUILTINS["hasattr"] = safe_hasattr


# ── Audit hook (defense-in-depth) ────────────────────────────────────────────
# sys.addaudithook is permanent per-process — once installed, it cannot be
# removed.  We use a threading-local flag that the Protector toggles so the
# hook only blocks operations while the sandbox is active.
#
# We do NOT block 'open' or 'import' because whitelisted modules (torch, etc.)
# need them during normal execution.  The hook focuses on operations that
# should never happen during model inference: process spawning, network access,
# native code loading, and destructive filesystem operations.

sandbox_active = threading.local()

_BLOCKED_AUDIT_EVENTS = frozenset({
    "os.system",
    "os.exec",
    "os.fork",
    "os.spawn",
    "os.kill",
    "webbrowser.open",
    # shutil.rmtree intentionally NOT blocked (branch): triton's tempfile cleanup
    # rmtree's its temp compile dirs. See OPEN SANDBOX note above.
})

# OPEN SANDBOX (branch): the Nemotron-H custom (trust_remote_code) path JIT-compiles
# mamba/triton kernels during the forward, which shells out to these CUDA/host
# compiler tools. Allow subprocess.Popen ONLY for this fixed set of executables so
# triton can compile; every other subprocess is still blocked, so user intervention
# code still cannot spawn arbitrary processes.
_COMPILER_EXECUTABLES = frozenset({
    "ptxas", "cuobjdump", "nvdisasm", "nvlink", "fatbinary", "cicc",
    "nvcc", "gcc", "cc", "g++", "c++", "clang", "clang++", "ld", "ld.lld", "as",
    # triton/mamba also shell out to these for library/driver discovery during
    # the first kernel launch (e.g. mamba_chunk_scan_combined -> ldconfig -p).
    "ldconfig", "which", "ldd",
})


def _basename(x) -> str:
    if isinstance(x, bytes):
        x = x.decode("utf-8", "ignore")
    if not isinstance(x, str):
        return ""
    return x.replace("\\", "/").rpartition("/")[2]


_HOOK_INSTALLED = False


def _audit_hook(event: str, args):
    if not getattr(sandbox_active, "enabled", False):
        return
    if event == "subprocess.Popen":
        # OPEN SANDBOX (branch): allow all subprocess. The triton/mamba kernel path
        # shells out during the forward to a moving set of tools — ptxas + gcc for
        # compilation, plus ldconfig/file/uname for driver+library discovery on the
        # first launch — and enumerating them is whack-a-mole. (The load-time warmup
        # that used to front-load these outside the sandbox has been removed.)
        return
    if event in _BLOCKED_AUDIT_EVENTS:
        raise RuntimeError(
            f"Operation '{event}' is blocked inside the sandbox"
        )


def install_audit_hook():
    """Install the audit hook (once per process, idempotent)."""
    global _HOOK_INSTALLED
    if not _HOOK_INSTALLED:
        sys.addaudithook(_audit_hook)
        _HOOK_INSTALLED = True
