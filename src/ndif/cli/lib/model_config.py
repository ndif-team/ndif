"""Model configuration file utilities.

Supports loading and saving model deployment configurations in YAML format.

File format:
    models:
      - gpt2                                    # Simple: just checkpoint
      - checkpoint: meta-llama/Llama-3.1-8b     # Full: with options
        pinned: true
        revision: main
        actor_class: ray.deployments.modeling.base.ModelActor  # optional
"""

from pathlib import Path
from typing import Optional

import yaml


DEFAULT_CONFIG_PATH = Path.home() / ".ndif" / "models.yaml"

# Template content for models.yaml with comments
MODELS_YAML_TEMPLATE = """\
# NDIF Model Configuration
#
# This file specifies which models to auto-deploy when starting NDIF.
# Models listed here are deployed automatically after `ndif start`.
#
# Format:
#   models:
#     - gpt2                              # Simple: just the checkpoint name (1 replica)
#     - checkpoint: meta-llama/Llama-3.1-8b
#       revision: main                    # Optional: specific revision/branch
#       pinned: true                      # Optional: won't be evicted (default: false)
#       replicas: 2                       # Optional: how many replicas (default: 1)
#       actor_class: ray.deployments.modeling.base.ModelActor  # Optional: custom Ray actor class
#
# Commands:
#   ndif deploy -f models.yaml            # Additive deploy from file
#   ndif deploy -f models.yaml --sync     # Match cluster state to file exactly
#   ndif export -f models.yaml            # Export current deployments to file
#   ndif export --stdout                  # Print current deployments to stdout
#
models: []
"""


def get_default_config_path() -> Path:
    """Get the default model config path (~/.ndif/models.yaml)."""
    return DEFAULT_CONFIG_PATH


def create_config_template(file_path: Path):
    """Create a models.yaml template file with comments.

    Args:
        file_path: Path to write the template file
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(MODELS_YAML_TEMPLATE)


def load_model_config(
    file_path: Path,
    default_revision: Optional[str] = None,
    default_pinned: bool = False,
    default_replicas: int = 1,
    default_model_actor_class: Optional[str] = None,
) -> list[dict]:
    """Load model specifications from a YAML config file.

    Args:
        file_path: Path to the YAML config file
        default_revision: Default revision to use when not specified in file
        default_pinned: Default pinned flag to use when not specified in file
        default_replicas: Default replica count to use when not specified in file
        default_model_actor_class: Default Ray actor class (dotted import path) to use
            when not specified in file

    Returns:
        List of model spec dicts with checkpoint, revision, pinned, replicas,
        actor_class keys.

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format is invalid
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")

    with open(file_path) as f:
        config = yaml.safe_load(f)

    if not config or "models" not in config:
        raise ValueError(f"Config file must contain a 'models' key: {file_path}")

    models = config["models"]
    if not isinstance(models, list):
        raise ValueError(f"'models' must be a list in {file_path}")

    specs = []
    for item in models:
        if isinstance(item, str):
            # Simple form: just the checkpoint string
            specs.append({
                "checkpoint": item,
                "revision": default_revision,
                "pinned": default_pinned,
                "replicas": default_replicas,
                "actor_class": default_model_actor_class,
            })
        elif isinstance(item, dict):
            # Full form: dict with checkpoint and optional revision/pinned/replicas/actor_class
            if "checkpoint" not in item:
                raise ValueError(f"Model entry missing 'checkpoint': {item}")
            specs.append({
                "checkpoint": item["checkpoint"],
                "revision": item.get("revision", default_revision),
                "pinned": item.get("pinned", default_pinned),
                "replicas": int(item.get("replicas", default_replicas)),
                "actor_class": item.get("actor_class", default_model_actor_class),
            })
        else:
            raise ValueError(f"Invalid model entry (must be string or dict): {item}")

    return specs


def save_model_config(file_path: Path, deployments: list[dict]):
    """Save model deployments to a YAML config file.

    Args:
        file_path: Path to write the YAML config file
        deployments: List of deployment dicts with repo_id, revision, pinned keys
    """
    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Build models list - use simple form when possible
    models = []
    for dep in deployments:
        repo_id = dep.get("repo_id") or dep.get("checkpoint")
        revision = dep.get("revision")
        pinned = dep.get("pinned", False)
        replicas = int(dep.get("replicas", 1) or 1)
        actor_class = dep.get("actor_class")

        # Use simple form if no special options.
        if not revision and not pinned and replicas == 1 and not actor_class:
            models.append(repo_id)
        else:
            entry = {"checkpoint": repo_id}
            if revision:
                entry["revision"] = revision
            if pinned:
                entry["pinned"] = pinned
            if replicas != 1:
                entry["replicas"] = replicas
            if actor_class:
                entry["actor_class"] = actor_class
            models.append(entry)

    config = {"models": models}

    with open(file_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def config_exists(file_path: Optional[Path] = None) -> bool:
    """Check if a model config file exists.

    Args:
        file_path: Path to check, or None for default path

    Returns:
        True if the config file exists and has content
    """
    path = file_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return False

    # Check if it has actual model content
    try:
        specs = load_model_config(path)
        return len(specs) > 0
    except (ValueError, FileNotFoundError):
        return False
