"""Whitelist configuration for the NDIF sandbox.

All sandbox policy lives in whitelist.yaml.  This module loads that file and
exposes the result as typed constants that the rest of the sandbox consumes.
Nothing in here enforces anything — it is pure data.

Three kinds of whitelist:
    modules  – which Python modules user code may import (each entry may
               also carry a ``blocked_submodules`` list for exceptions)
    builtins – which builtins are visible (open, exec, compile excluded)
    dunders  – which __dunder__ attributes may be accessed at runtime
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel

_WHITELIST_PATH = Path(__file__).parent / "whitelist.yaml"


def _load_config() -> dict:
    with open(_WHITELIST_PATH, "r") as f:
        return yaml.safe_load(f)


_CONFIG = _load_config()


# ── Module whitelist ──────────────────────────────────────────────────────────


class WhitelistedModule(BaseModel):
    """One entry from the ``modules`` or ``deserialization_modules`` list.

    strict=True  → only the exact module name matches  (e.g. "operator")
    strict=False → the module and every submodule match (e.g. "torch" allows
                   "torch.nn.functional") UNLESS the submodule appears in
                   ``blocked_submodules``.
    """

    name: str
    strict: bool = True
    blocked_submodules: List[str] = []

    def check(self, name: str) -> bool:
        """True if *name* is allowed by this entry (whitelisted and not blocked)."""
        # Check blocked first — a blocked submodule is never allowed.
        for blocked in self.blocked_submodules:
            if name == blocked or name.startswith(blocked + "."):
                return False

        if self.strict:
            return self.name == name
        return self.name == name or name.startswith(self.name + ".")


def _load_modules(key: str) -> List[WhitelistedModule]:
    return [WhitelistedModule(**m) for m in _CONFIG.get(key, [])]


def is_module_whitelisted(name: str, whitelist: List[WhitelistedModule]) -> bool:
    return any(m.check(name) for m in whitelist)


# Modules allowed during user code execution.
WHITELISTED_MODULES = _load_modules("modules")

# Superset used during deserialization — adds pickle/cloudpickle/nnsight
# internals that the unpickler needs but user code must not access.
WHITELISTED_MODULES_DESERIALIZATION = [
    *_load_modules("deserialization_modules"),
    *WHITELISTED_MODULES,
]


# ── Blocked submodules (derived from module entries) ──────────────────────────
# Collected from all module entries for use by SandboxFinder and other checks
# that need a flat set of all blocked names.

BLOCKED_SUBMODULES: frozenset = frozenset(
    blocked
    for m in (*WHITELISTED_MODULES, *_load_modules("deserialization_modules"))
    for blocked in m.blocked_submodules
)


def is_module_blocked(name: str) -> bool:
    """True if *name* matches any blocked submodule (exact or prefix)."""
    for blocked in BLOCKED_SUBMODULES:
        if name == blocked or name.startswith(blocked + "."):
            return True
    return False


def is_module_allowed(name: str, whitelist: List[WhitelistedModule]) -> bool:
    """True if *name* is whitelisted AND not blocked.

    Note: ``WhitelistedModule.check()`` already accounts for blocked submodules
    on its own entry.  This function also checks ``is_module_blocked()`` as
    defense-in-depth (catches blocks defined on other entries).
    """
    return is_module_whitelisted(name, whitelist) and not is_module_blocked(name)


# ── Builtin whitelist ─────────────────────────────────────────────────────────

WHITELISTED_BUILTINS = set(_CONFIG.get("builtins", []))

# Filtered copy of __builtins__ — only entries named in the YAML survive.
# compile() and exec() are absent here; guards.py adds restricted versions.
# getattr/setattr/delattr are present here as the real builtins; guards.py
# replaces them with guarded versions.
SAFE_BUILTINS = {
    k: v for k, v in __builtins__.items() if k in WHITELISTED_BUILTINS
}


# ── Dunder attribute whitelist ────────────────────────────────────────────────

# Dunders that enable sandbox escapes (__class__, __globals__, __code__, …).
BLOCKED_DUNDER_ATTRS = frozenset(_CONFIG.get("blocked_dunder_attrs", []))

# Dunders that are safe and needed for normal operations (__len__, __add__, …).
ALLOWED_DUNDER_ATTRS = frozenset(_CONFIG.get("allowed_dunder_attrs", []))
