"""Load the packaged tab-bar renderer for older source integrations."""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

draw_tab = import_module("kisesh.integration.tab_bar").draw_tab

__all__ = ["draw_tab"]
