"""Load the packaged tab-bar reload kitten for older source integrations."""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_implementation = import_module("kisesh.integration.reload_tab_bar")
handle_result = _implementation.handle_result
main = _implementation.main

__all__ = ["handle_result", "main"]
