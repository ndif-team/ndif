import re
import warnings
from typing import Callable

from accelerate.hooks import remove_hook_from_module
from huggingface_hub import scan_cache_dir
from huggingface_hub.utils._cache_manager import CachedRepoInfo
import threading
import ctypes

import logging

logger = logging.getLogger("ndif")

pattern = re.compile(
    r"expected file size is:\s*([0-9]+(?:\.[0-9]+)?)\s*MB.*?only has\s*([0-9]+(?:\.[0-9]+)?)\s*MB",
    re.IGNORECASE,
)


def remove_accelerate_hooks(model):
    for name, module in model.named_modules():
        if hasattr(module, "_hf_hook") and module._hf_hook is not None:
            remove_hook_from_module(module)
    return model


def downloaded(repo: CachedRepoInfo):
    for revision in repo.revisions:
        for file in revision.files:
            if not file.file_name.endswith(".config"):
                return True

    return False


def make_room(expected_mb: float, available_mb: float):
    hf_info = scan_cache_dir()

    repos_by_access = sorted(hf_info.repos, key=lambda repo: repo.last_accessed)

    mb_needed = expected_mb - available_mb

    evictions = []

    for repo in repos_by_access:
        if not downloaded(repo):
            continue

        evictions.extend([revision.commit_hash for revision in repo.revisions])

        mb_needed -= repo.size_on_disk / (1024 * 1024)  # Convert bytes to MB

        logger.error(
            f"==> Evicting {repo.repo_id} with size {repo.size_on_disk / (1024 * 1024)} MB from HF cache"
        )

        if mb_needed <= 0:
            break

    strategy = hf_info.delete_revisions(*evictions)
    strategy.execute()


def load_with_cache_deletion_retry(load_fn: Callable):
    warnings.filterwarnings(
        "error", message="Not enough free disk space to download the file."
    )

    try:
        return load_fn()

    except Exception as exception:
        if "Not enough free disk space to download the file." in str(
            exception.__cause__
        ):
            m = pattern.search(str(exception.__cause__))

            if m:
                expected_mb = float(m.group(1))
                available_mb = float(m.group(2))

                logger.error(
                    f"=> Not enough free disk space to download the model. Making room for {expected_mb} MB of space. Available: {available_mb} MB"
                )

                make_room(expected_mb, available_mb)

                return load_fn()

        raise exception


def get_downloaded_models():
    hf_info = scan_cache_dir()

    return [repo.repo_id for repo in hf_info.repos if downloaded(repo)]



def kill_thread(ident: int, exc_type=SystemExit):
    if not isinstance(exc_type, type) or not issubclass(exc_type, BaseException):
        raise TypeError("exc_type must be an exception type")

    # Optionally check if thread ID exists
    if ident not in [t.ident for t in threading.enumerate()]:
        raise ValueError("Thread ID not found")

    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(ident), ctypes.py_object(exc_type)
    )
    if res == 0:
        raise ValueError("Invalid thread ID")
    elif res > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(ident), None)
        raise SystemError("PyThreadState_SetAsyncExc failed")
