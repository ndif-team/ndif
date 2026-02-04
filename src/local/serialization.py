"""Serialization utilities for local mode."""

from io import BytesIO
from pathlib import Path

import torch
import zstandard as zstd

from src.common.schema.mixins import cpu_pickle_module


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
