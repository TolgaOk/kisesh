"""Expose the installed Workbench native tab-bar renderer to Kitty."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

INSTALL_ROOT = Path.home() / ".local" / "lib" / "kitty-workbench"
if str(INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(INSTALL_ROOT))

DrawTab = Callable[[object, object, object, int, int, int, bool, object], int]
draw_tab = cast(DrawTab, importlib.import_module("kitty_workbench.session_bar").draw_tab)

__all__ = ["draw_tab"]
