"""Expose the installed KiSesh native tab-bar renderer to Kitty."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast


def _add_runtime_import_path() -> None:
    """Expose the runtime package before importing the shared renderer."""
    runtime = str(Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser())
    if runtime not in sys.path:
        sys.path.insert(0, runtime)


_add_runtime_import_path()

DrawTab = Callable[[object, object, object, int, int, int, bool, object], int]
renderer = importlib.reload(importlib.import_module("kisesh.session_bar"))
draw_tab = cast(DrawTab, renderer.draw_tab)

__all__ = ["draw_tab"]
