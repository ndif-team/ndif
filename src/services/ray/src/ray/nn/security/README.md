# NDIF Sandbox Security

This directory implements the security sandbox that protects the NDIF server from untrusted user code. Users submit nnsight intervention code that runs on shared GPU workers — the sandbox ensures that code can only perform model inference operations, not escape to the host system.

## Threat Model

Users submit serialized Python (cloudpickle) that the server deserializes and executes against a loaded model. An attacker could:

- Import dangerous modules (`os`, `subprocess`, `socket`) to access the host
- Access dunder attributes (`__class__`, `__globals__`) to escape the sandbox
- Craft pickle payloads that reconstruct dangerous objects during deserialization
- Use whitelisted modules to reach dangerous submodules (`torch.multiprocessing`)
- Move models between GPUs or mutate shared model state

## Security Layers

The sandbox uses six layers of defense-in-depth. No single layer is sufficient on its own — they overlap so that bypassing one still leaves others in place.

### Layer 1: Import Interception (`importer.py`)

The **Importer** class replaces `__builtins__["__import__"]` while the sandbox is active. Every `import` statement goes through it.

- **Whitelisted modules** (torch, numpy, collections, etc.) are imported normally but wrapped in a **ProtectedModule** that makes them immutable and blocks cross-module access (e.g. `torch.os` is blocked even though `torch` is allowed).
- **Non-whitelisted modules** (os, subprocess, socket, sys, etc.) return an **UnauthorizedModule** — a lazy placeholder that raises `ImportError` on any interaction. The error is deferred (not raised on import) so that speculative imports that catch `ImportError` work normally.
- **Blocked submodules** of whitelisted packages (torch.multiprocessing, torch.distributed, torch.hub, numpy.ctypeslib, etc.) are treated as non-whitelisted even though their parent is allowed.

The whitelist and blocklist are defined in `whitelist.yaml`.

### Layer 2: Meta-Path Finder (`importer.py` → `SandboxFinder`)

A **SandboxFinder** is inserted at position 0 in `sys.meta_path` while the sandbox is active. This is defense-in-depth against imports that bypass `__import__` — for example, `importlib._bootstrap._find_and_load` called from C code does not go through `__import__` but does consult `sys.meta_path`.

The finder returns `None` for allowed modules (letting normal finders handle them) and a blocking `ModuleSpec` for everything else that raises `ImportError` during module execution.

### Layer 3: Deserialization Hardening (`protector.py`)

User requests arrive as cloudpickle payloads. During unpickling, two functions bypass the `__import__` hook by calling it but then ignoring the return value and reading `sys.modules` directly:

- **`cloudpickle.subimport(name)`** — reconstructs module objects. Patched to check the whitelist before returning.
- **`cloudpickle.dynamic_subimport(name, vars)`** — creates modules from a vars dict. Patched with the same check.
- **`pickle.Unpickler.find_class(module, name)`** — reconstructs class/function references. Overridden on `CustomCloudUnpickler` (nnsight's unpickler) to check the whitelist. Uses `pickle._getattribute` to handle dotted names like `Tracer.Info`.

A separate deserialization whitelist (`deserialization_modules` in the YAML) temporarily allows pickle/cloudpickle/nnsight internals that the unpickler needs but user code must not access.

### Layer 4: Builtin Restriction (`whitelist.py`, `guards.py`)

`SAFE_BUILTINS` is a filtered copy of `__builtins__` containing only entries named in the YAML whitelist. Dangerous builtins are excluded:

- `open` — file I/O
- `__import__` — replaced with the Importer
- `compile`, `exec` — replaced with restricted versions (see Layer 5)
- `getattr`, `setattr`, `delattr`, `hasattr` — replaced with guarded versions that enforce dunder restrictions (see below)

When `Protector(builtins=True)` is used (the execution phase), non-whitelisted builtins are also removed from the real `__builtins__` dict.

### Layer 5: Attribute Guards (`guards.py`)

Dangerous dunder attributes (`__class__`, `__globals__`, `__code__`, `__dict__`, etc.) are blocked through two mechanisms:

- **Guarded builtins**: `getattr`, `setattr`, `delattr`, and `hasattr` in `SAFE_BUILTINS` are replaced with versions that check attribute names against the dunder blocklist before delegating to the real builtins. This catches `getattr(obj, '__class__')` even without AST transformation.
- **Guard functions**: `_getattr_`, `_write_`, `_inplacevar_`, etc. are injected into `make_restricted_globals()` for use with RestrictedPython's AST-transformed code (if/when enabled).

`restricted_compile` and `restricted_exec` replace the `compile` and `exec` builtins so user code that calls them gets the restricted versions.

The allowed/blocked dunder lists are defined in `whitelist.yaml`.

### Layer 6: Audit Hook (`guards.py`)

A `sys.addaudithook` callback blocks dangerous syscall-level operations when the sandbox is active:

- `subprocess.Popen`, `os.system`, `os.exec`, `os.fork`, `os.spawn`, `os.kill`
- `webbrowser.open`, `shutil.rmtree`

The hook is **permanent per-process** (Python limitation — cannot be removed once installed). A `threading.local` flag is toggled by the Protector's `__enter__`/`__exit__` so the hook only blocks operations while the sandbox is active. When the Protector temporarily exits for whitelisted imports, the flag is disabled so internal operations proceed normally.

The hook does NOT block `open` or `import` because whitelisted modules (torch, numpy) need them during normal execution.

## Protected Objects (`protected_objects.py`)

Separate from the sandbox, the **ProtectedObject** wrapper protects loaded models from user mutation:

- **Device movement blocked**: `.to()`, `.cuda()`, `.cpu()`, `.half()`, `.float()`, `.bfloat16()`, `.double()`, `.to_empty()` — prevents users from moving models off their assigned GPUs.
- **Tensor deepcopy**: Reads of Tensor, list, and dict attributes return deep copies so users can't silently mutate model internals.
- **Write tracking**: Attribute writes are recorded and reverted by `clear_set_attrs()` after each request so modifications don't leak between users.

## File Structure

```
security/
  __init__.py            Public API: Protector, WHITELISTED_MODULES, WHITELISTED_MODULES_DESERIALIZATION
  whitelist.yaml         All policy: allowed builtins, modules, blocked submodules, dunder lists
  whitelist.py           Loads whitelist.yaml into typed constants
  importer.py            Import interception: Importer, SandboxFinder, module wrappers
  guards.py              Attribute guards, restricted compile/exec, guarded builtins, audit hook
  protector.py           Protector context manager — orchestrates all layers
  protected_objects.py   Model/tokenizer wrapping (independent of the sandbox)
```

Dependency graph (no cycles):
```
whitelist.py
    ↓
importer.py   guards.py
    ↘         ↙
   protector.py
```

## Request Lifecycle

```
  Client                           Server (ModelActor)
  ──────                           ───────────────────
  import os                        ┌─────────────────────────────────┐
  with model.session(remote=True): │ 1. DESERIALIZATION              │
    os.listdir(".")                │    Protector(DESERIALIZATION)    │
    model.generate(...)            │    ├─ __import__ → Importer     │
         │                         │    ├─ subimport → whitelist     │
    cloudpickle.dumps(session) ──► │    ├─ find_class → whitelist    │
                                   │    └─ meta_path → SandboxFinder │
                                   │    request.deserialize()        │
                                   │    ✗ os ref → ImportError       │
                                   │                                 │
                                   │ 2. EXECUTION                    │
                                   │    Protector(EXECUTION,         │
                                   │             builtins=True)      │
                                   │    ├─ __import__ → Importer     │
                                   │    ├─ builtins → SAFE_BUILTINS  │
                                   │    ├─ getattr → safe_getattr    │
                                   │    ├─ audit hook → enabled      │
                                   │    └─ meta_path → SandboxFinder │
                                   │    tracer.execute(model)        │
                                   │                                 │
                                   │ 3. CLEANUP                      │
                                   │    clear_set_attrs()            │
                                   └─────────────────────────────────┘
```

## Adding to the Whitelist

To allow a new module, add an entry to `whitelist.yaml` under `modules`:

```yaml
- name: scipy
  strict: false   # allows scipy.linalg, scipy.stats, etc.
```

To block a dangerous submodule of an allowed package:

```yaml
blocked_submodules:
  - scipy.io   # file I/O
```

## Known Limitations

- **No AST transformation**: We use standard `compile()` rather than RestrictedPython's AST transformation because it conflicts with nnsight's internal variable naming (`__nnsight_tracer_*`). This means `obj.__class__` in syntax form is not caught — only `getattr(obj, '__class__')` is blocked via the guarded builtins. Full dunder blocking requires AST transformation.
- **Python-level sandbox**: The sandbox operates at the Python level. C extensions loaded by whitelisted modules (torch, numpy) can perform arbitrary operations. OS-level isolation (seccomp, gVisor) would be needed to contain C-level escapes.
- **Audit hook is permanent**: Once installed, the audit hook cannot be removed for the lifetime of the process. The threading-local flag minimizes overhead when the sandbox is inactive.
- **`sys.modules` caching**: If a blocked module was already imported before the sandbox activated, it remains in `sys.modules`. The `__import__` patch catches direct imports, but code that reaches `sys.modules` through other means (e.g. a whitelisted module's internals) could access the cached module.
