"""Load and save model deployment configs in YAML (for ``deploy -f`` / ``export``).

File format::

    models:
      - gpt2                                 # simple: just the checkpoint
      - checkpoint: meta-llama/Llama-3.1-8B  # full: with options
        revision: null
        pinned: true
        replicas: 2
        trusted: false
        dtype: bfloat16
        padding_factor: 0.15
        # Placement overrides -- any of these replaces a step the controller
        # would otherwise derive; anything left out is still worked out for you.
        size_bytes: 6425499648   # measured weights; skips the Hub estimate
        padding_bias: 2000000000 # per-model flat headroom
        gpus: 4                  # place on exactly this many cards
        max_tp: 8                # cap/supply the sharding degree (0 = never TP)
        execution_timeout_seconds: 3600
        envoy_class: ndif.services.ray.deployments.modeling.base.ModelActor
        actor_class: ndif.services.ray.deployments.modeling.base.ModelActor
        model_key: null
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


def load_model_config(
    file_path: Path,
    default_revision: Optional[str] = None,
    default_pinned: bool = False,
    default_replicas: int = 1,
    default_model_actor_class: Optional[str] = None,
    default_trusted: bool = False,
    default_dtype: Optional[str] = None,
    default_padding_factor: Optional[float] = None,
    default_padding_bias: Optional[int] = None,
    default_size_bytes: Optional[int] = None,
    default_gpus: Optional[int] = None,
    default_max_tp: Optional[int] = None,
    default_execution_timeout_seconds: Optional[float] = None,
    default_envoy_class: Optional[str] = None,
) -> list[dict]:
    """Load model specs from a YAML config file.

    Every field the deploy path understands is passed through: ``checkpoint``,
    ``revision``, ``pinned``, ``replicas``, ``actor_class``, ``trusted``,
    ``dtype``, ``padding_factor``, ``execution_timeout_seconds``,
    ``envoy_class``, ``model_key``. A per-model value in the file overrides the
    corresponding ``default_*`` (which the CLI flags feed).

    Raises FileNotFoundError if the file is missing, ValueError on bad format.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")

    with open(file_path) as f:
        data = yaml.safe_load(f)

    if not data or "models" not in data:
        raise ValueError(f"Config file must contain a 'models' key: {file_path}")

    models = data["models"]
    if not isinstance(models, list):
        raise ValueError(f"'models' must be a list in {file_path}")

    specs: list[dict] = []
    for item in models:
        if isinstance(item, str):
            specs.append({
                "checkpoint": item,
                "revision": default_revision,
                "pinned": default_pinned,
                "replicas": default_replicas,
                "actor_class": default_model_actor_class,
                "trusted": default_trusted,
                "dtype": default_dtype,
                "padding_factor": default_padding_factor,
                "size_bytes": default_size_bytes,
                "padding_bias": default_padding_bias,
                "gpus": default_gpus,
                "max_tp": default_max_tp,
                "execution_timeout_seconds": default_execution_timeout_seconds,
                "envoy_class": default_envoy_class,
                "model_key": None,
            })
        elif isinstance(item, dict):
            if "checkpoint" not in item:
                raise ValueError(f"Model entry missing 'checkpoint': {item}")
            specs.append({
                "checkpoint": item["checkpoint"],
                "revision": item.get("revision", default_revision),
                "pinned": item.get("pinned", default_pinned),
                "replicas": int(item.get("replicas", default_replicas)),
                "actor_class": item.get("actor_class", default_model_actor_class),
                "trusted": item.get("trusted", default_trusted),
                "dtype": item.get("dtype", default_dtype),
                "padding_factor": item.get("padding_factor", default_padding_factor),
                "size_bytes": item.get("size_bytes", default_size_bytes),
                "padding_bias": item.get("padding_bias", default_padding_bias),
                "gpus": item.get("gpus", default_gpus),
                "max_tp": item.get("max_tp", default_max_tp),
                "execution_timeout_seconds": item.get(
                    "execution_timeout_seconds", default_execution_timeout_seconds
                ),
                "envoy_class": item.get("envoy_class", default_envoy_class),
                "model_key": item.get("model_key"),
            })
        else:
            raise ValueError(f"Invalid model entry (must be string or dict): {item}")

    return specs


def build_models_list(deployments: list[dict]) -> list:
    """Build the ``models:`` list for a config file (simple form when possible).

    The single serializer behind both config outputs — ``save_model_config``
    writes it to a file, ``ndif export --stdout`` prints it — so the two emit the
    same shape for the same deployments.
    """
    models: list = []
    for dep in deployments:
        repo_id = dep.get("repo_id") or dep.get("checkpoint")
        revision = dep.get("revision")
        pinned = dep.get("pinned", False)
        replicas = int(dep.get("replicas", 1) or 1)
        actor_class = dep.get("actor_class")
        trusted = bool(dep.get("trusted", False))
        dtype = dep.get("dtype")
        padding_factor = dep.get("padding_factor")
        execution_timeout_seconds = dep.get("execution_timeout_seconds")
        envoy_class = dep.get("envoy_class")
        model_key = dep.get("model_key")

        extras = (
            revision or pinned or replicas != 1 or actor_class or trusted or dtype
            or padding_factor is not None or execution_timeout_seconds is not None
            or envoy_class or model_key
        )
        if not extras:
            models.append(repo_id)
        else:
            entry = {"checkpoint": repo_id}
            if revision:
                entry["revision"] = revision
            if pinned:
                entry["pinned"] = pinned
            if replicas != 1:
                entry["replicas"] = replicas
            if trusted:
                entry["trusted"] = trusted
            if dtype:
                entry["dtype"] = dtype
            if padding_factor is not None:
                entry["padding_factor"] = padding_factor
            if execution_timeout_seconds is not None:
                entry["execution_timeout_seconds"] = execution_timeout_seconds
            if envoy_class:
                entry["envoy_class"] = envoy_class
            if actor_class:
                entry["actor_class"] = actor_class
            if model_key:
                entry["model_key"] = model_key
            models.append(entry)

    return models


def save_model_config(file_path: Path, deployments: list[dict]) -> None:
    """Write model deployments to a YAML config file (simple form when possible)."""
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w") as f:
        yaml.dump(
            {"models": build_models_list(deployments)},
            f,
            default_flow_style=False,
            sort_keys=False,
        )
