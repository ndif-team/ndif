"""Utility functions for NDIF CLI"""

import time
from pathlib import Path


# ASCII art for NDIF logo
NDIF_LOGO = [
    "                              ",
    " ███╗   ██╗██████╗ ██╗███████╗",
    " ████╗  ██║██╔══██╗██║██╔════╝",
    " ██╔██╗ ██║██║  ██║██║█████╗  ",
    " ██║╚██╗██║██║  ██║██║██╔══╝  ",
    " ██║ ╚████║██████╔╝██║██║     ",
    " ╚═╝  ╚═══╝╚═════╝ ╚═╝╚═╝     ",
]


def print_logo():
    """Print the NDIF logo with a purple to light blue gradient."""
    start_color = (148, 0, 211)  # Purple
    end_color = (135, 206, 250)  # Light blue

    width = len(NDIF_LOGO[0])

    for line in NDIF_LOGO:
        colored_line = ""
        for i, char in enumerate(line):
            factor = i / (width - 1) if width > 1 else 0
            r = int(start_color[0] + (end_color[0] - start_color[0]) * factor)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * factor)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * factor)
            colored_line += f"\033[38;2;{r};{g};{b}m{char}\033[0m"
        print(colored_line)
    print()


def get_ndif_root() -> Path:
    """Get the root of the installed ndif package directory.

    Works identically in development (editable install) and installed modes —
    resolves to wherever the `ndif` package was imported from, e.g.
    ``<repo>/src/ndif`` or ``site-packages/ndif``.
    """
    import ndif

    return Path(ndif.__file__).resolve().parent


def get_service_dir(service_name: str) -> Path:
    """Get the filesystem directory for a given service (api, ray, monitor)."""
    return get_ndif_root() / "services" / service_name


# =============================================================================
# Ray utilities — see ``ndif.common.providers.ray`` for the actor handle
# helpers (``get_controller_actor_handle``, ``get_model_actor_handle``).
# =============================================================================


DEFAULT_ENVOY_CLASS = "nnsight.modeling.language.LanguageModel"


def get_model_key(
    checkpoint: str,
    revision: str = None,
    envoy_class: str = None,
) -> str:
    """Get the model key for a checkpoint.

    Args:
        checkpoint: Model checkpoint/repo ID
        revision: Model revision (default: None, uses model's default)
        envoy_class: Dotted import path of the nnsight envoy class
            (e.g. ``nnsight.modeling.language.LanguageModel``,
            ``nnsight.modeling.vlm.VisionLanguageModel``). Defaults to
            ``LanguageModel`` for back-compat.

    Returns:
        Model key string
    """
    # TODO: This is a temporary workaround to get the model key.
    # There should be a more lightweight way to do this.
    from nnsight.util import from_import_path

    cls = from_import_path(envoy_class or DEFAULT_ENVOY_CLASS)
    model = cls(
        checkpoint, revision=revision, dispatch=False, trust_remote_code=True
    )
    return model.to_model_key()


def extract_repo_id_from_model_key(model_key: str) -> str:
    """Extract repo_id from model_key string.

    Args:
        model_key: Full model key string

    Returns:
        The repo_id if found, otherwise the original model_key
    """
    # model_key format: 'nnsight.modeling.language.LanguageModel:{"repo_id": "...", ...}'
    try:
        if '"repo_id":' in model_key:
            start = model_key.index('"repo_id":') + len('"repo_id":')
            remainder = model_key[start:].strip()
            if remainder.startswith('"'):
                end = remainder.index('"', 1)
                return remainder[1:end]
    except (ValueError, IndexError):
        pass
    return model_key


def canonicalize_checkpoint(
    checkpoint: str,
    revision: str = None,
    envoy_class: str = None,
) -> tuple[str, str | None, str]:
    """Resolve a user-typed checkpoint and return ``(canonical_checkpoint, revision, model_key)``.

    HuggingFace repo IDs are case-insensitive — the API serves the same model
    regardless of casing — but the canonical name has a specific capitalization
    (e.g. ``meta-llama/Llama-3.1-8B``, not ``…-8b``). nnsight's
    ``<envoy_class>(...).to_model_key()`` does the HF resolution; we surface
    the canonical repo_id AND the model_key from a single lookup so callers
    can persist both (e.g. the schedule store) without paying the HF cost
    twice.

    ``envoy_class`` selects which nnsight wrapper class to use (defaults to
    ``LanguageModel``); the resulting model_key is prefixed with that class's
    import path so the server can reconstruct it.
    """
    model_key = get_model_key(checkpoint, revision, envoy_class)
    return extract_repo_id_from_model_key(model_key), revision, model_key


def get_current_deployments(level: str = "HOT") -> list[dict]:
    """Fetch current deployments from the controller.

    Requires Ray to be initialized first.

    Args:
        level: Deployment level to filter by ("HOT", "WARM", "COLD", or None for all)

    Returns:
        List of deployment dicts with repo_id, revision, pinned, model_key, etc.
    """
    import ray
    from ...common.providers.ray import get_controller_actor_handle

    controller = get_controller_actor_handle()
    status_ref = controller.status.remote()
    status = ray.get(status_ref)

    deployments = status.get("deployments", {})

    if level:
        return [
            dep for dep in deployments.values() if dep.get("deployment_level") == level
        ]
    return list(deployments.values())


def wait_for_replica_ready(
    model_key: str, replica_id: str, timeout: int = 300
) -> bool:
    """Wait for a specific replica's actor to be ready.

    Polls the actor's ``__ray_ready__`` until it returns successfully,
    indicating the replica is fully loaded and ready for inference.

    Tolerated transient states (keep polling):
    - ``"Failed to look up actor"`` — actor not yet registered (fresh deploy
      mid-creation).
    - ``RayActorError`` — actor died and is in the process of being respawned
      (the path ``restart()`` triggers via ``ray.kill(no_restart=False)``).

    Args:
        model_key: The model key.
        replica_id: The specific replica to wait on.
        timeout: Maximum seconds to wait (default: 300 = 5 minutes).

    Returns:
        True if ready, False if timeout.

    Raises:
        Exception: On a non-transient initialization error.
    """
    import ray
    import ray.exceptions
    from ...common.providers.ray import get_model_actor_handle

    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            handle = get_model_actor_handle(model_key, replica_id)
            ray.get(handle.__ray_ready__.remote())
            return True
        except ray.exceptions.RayActorError:
            time.sleep(2)
            continue
        except Exception as e:
            if "Failed to look up actor" in str(e):
                time.sleep(2)
                continue
            raise

    return False
