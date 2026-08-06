"""Crash-safe filesystem primitives shared by persistence components."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    mode: int | None = None,
    prefix: str | None = None,
) -> None:
    """Replace a text file atomically after flushing its bytes to disk."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=prefix or f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def temporary_path(directory: Path, *, prefix: str, suffix: str) -> Iterator[Path]:
    """Yield a closed temporary path and remove it after the operation."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(descriptor)
    path = Path(temporary)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
