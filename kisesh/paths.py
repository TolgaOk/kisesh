"""Resolve the KiSesh session-data directory."""

from __future__ import annotations

import os
from pathlib import Path


def data_root(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the session-data root using explicit, environment, then XDG values."""
    if override:
        return Path(override).expanduser()
    configured = os.environ.get("KISESH_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return base / "kisesh"


def runtime_root() -> Path:
    """Resolve the development source or stable installed runtime root."""
    configured = os.environ.get("KISESH_INSTALL_ROOT")
    if configured:
        return Path(configured).expanduser()
    source = Path(__file__).absolute().parents[1]
    if (source / "bin" / "kisesh").is_file() and (source / "integration" / "kisesh.conf").is_file():
        return source
    return Path("~/.local/lib/kisesh").expanduser()
