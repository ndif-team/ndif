"""Cache of values the user has previously deployed successfully.

Backs the autocomplete dropdowns in the deploy + schedule modals. Three
keyed lists — ``repo_id``, ``actor_class``, ``envoy_class`` — kept in
most-recently-used order and capped so the file doesn't grow unbounded.

Only successfully-deployed values land here; the deploy/reconcile paths
call :func:`add_many` after the controller has confirmed each spec went
``READY``/``DEPLOYED``. That keeps the dropdown free of typos and dead
checkpoints.

File layout (``<data_dir>/cache/values.json``)::

    {
      "repo_id":     ["meta-llama/Llama-3.1-8B", "openai-community/gpt2", ...],
      "actor_class": ["ndif.services.ray.deployments.modeling.base.ModelActor", ...],
      "envoy_class": ["nnsight.modeling.transformers.TransformersModel", ...]
    }
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

# Per-field cap. The file should stay tiny in practice, but we don't want
# adversarial input to make it unbounded — and the dropdown gets unusable
# past a few hundred entries anyway.
MAX_ENTRIES_PER_FIELD = 200

# The set of fields we track. Anything outside this is silently ignored
# so callers can pass arbitrary spec dicts without sanitizing.
FIELDS = ("repo_id", "actor_class", "envoy_class")


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {f: [] for f in FIELDS}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {f: [] for f in FIELDS}
    return {f: list(raw.get(f) or []) for f in FIELDS}


def _write(path: Path, data: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _bump(values: list[str], new: str) -> list[str]:
    """MRU bump: move ``new`` to the front; cap at ``MAX_ENTRIES_PER_FIELD``."""
    out = [v for v in values if v != new]
    out.insert(0, new)
    return out[:MAX_ENTRIES_PER_FIELD]


def read(path: Path) -> dict[str, list[str]]:
    """Return the current cache (may be empty lists)."""
    with _locked(path):
        return _read(path)


def add_many(path: Path, entries: Iterable[dict]) -> None:
    """Bump every (field, value) pair from ``entries`` in MRU order.

    ``entries`` is a list of spec-shaped dicts; only the keys in
    :data:`FIELDS` are read, others are ignored. Empty/falsy values are
    skipped so a deploy that didn't specify ``actor_class`` doesn't
    add ``""`` or ``None`` to the cache.
    """
    entries = list(entries)
    if not entries:
        return

    with _locked(path):
        data = _read(path)
        for entry in entries:
            for field in FIELDS:
                value = entry.get(field)
                if not value or not isinstance(value, str):
                    continue
                data[field] = _bump(data[field], value)
        _write(path, data)


def add_from_deploy_result(
    path: Path,
    specs: Iterable[dict],
    deployments_result: Iterable[dict],
) -> None:
    """Bump cache entries for specs whose deploy succeeded.

    Pairs ``specs`` to the per-model entries in ``deployments_result`` by
    ``checkpoint`` (the as-typed user string the deploy lib echoes back).
    The ``repo_id`` written to the cache is the canonical form parsed out
    of the ``model_key`` so case variants (``…-8b`` vs ``…-8B``) collapse
    to the form HF actually serves.

    ``error is None`` is the success criterion — i.e. ``status == "READY"``
    with every replica initialized. Anything with an error attached
    (``"PARTIAL"`` ready-but-not-all, ``"ERROR"`` for placement failure,
    timeouts during ``wait_for_replica_ready``, …) is silently skipped.
    """
    # Lazy import — keeps the cache module free of cli dependency.
    from ....cli.lib.models import extract_repo_id_from_model_key

    specs_by_checkpoint = {s.get("checkpoint"): s for s in specs}
    entries: list[dict] = []
    for d in deployments_result:
        if d.get("error") is not None:
            continue
        spec = specs_by_checkpoint.get(d.get("checkpoint")) or {}
        repo_id = extract_repo_id_from_model_key(d.get("model_key") or "")
        entries.append({
            "repo_id": repo_id or d.get("checkpoint"),
            "actor_class": spec.get("actor_class"),
            "envoy_class": spec.get("envoy_class"),
        })
    add_many(path, entries)
