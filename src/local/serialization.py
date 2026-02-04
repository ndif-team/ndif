"""Serialization utilities for local mode.

NOTE: TensorStoragePickler and cpu_pickle_module are duplicated from
src/common/schema/mixins.py (lines 25-68) to avoid importing from the common
package which has top-level dependencies on boto3 and ray.
"""

import pickle
import types
from io import BytesIO
from pathlib import Path

import torch
import zstandard as zstd


class TensorStoragePickler(pickle.Pickler):
    """Custom pickler that moves GPU tensors to CPU before serialization."""

    def reducer_override(self, obj):
        if torch.is_tensor(obj):
            if obj.device.type != "cpu":
                cpu_tensor = obj.detach().to("cpu")
                return cpu_tensor.__reduce_ex__(pickle.HIGHEST_PROTOCOL)
            return NotImplemented
        return NotImplemented


cpu_pickle_module = types.ModuleType("cpu_pickle_module")
for key, value in pickle.__dict__.items():
    setattr(cpu_pickle_module, key, value)
cpu_pickle_module.Pickler = TensorStoragePickler


def save_result(saves: dict, path: Path, compress: bool) -> None:
    """Serialize execution result to a file on disk.

    Args:
        saves: Dict of saved values from tracer.execute().
        path: Destination file path.
        compress: Whether to apply zstd compression.
    """
    data = BytesIO()
    torch.save(saves, data, pickle_module=cpu_pickle_module)

    if compress:
        compressed = BytesIO()
        with zstd.ZstdCompressor(level=6).stream_writer(compressed, closefd=False) as writer:
            data.seek(0)
            while chunk := data.read(64 * 1024):
                writer.write(chunk)
        data = compressed

    data.seek(0)
    path.write_bytes(data.read())
