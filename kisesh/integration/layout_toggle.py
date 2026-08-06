"""No-UI Kitty kitten that reliably toggles restored KiSesh layouts."""

from __future__ import annotations

import os
import sys
from importlib import import_module, reload
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from kittens.tui.handler import result_handler

if TYPE_CHECKING:
    from kitty.boss import Boss

    from kisesh.layout_toggle import LayoutBoss


class LayoutToggleHandler(Protocol):
    """Callable contract exported by the installable layout-toggle module."""

    def __call__(self, boss: LayoutBoss) -> None:
        """Toggle the active KiSesh tab's zoom layout."""


PROJECT_ROOT = Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
LAYOUT_TOGGLE_MODULE = reload(import_module("kisesh.layout_toggle"))
toggle_session_layout = cast(
    LayoutToggleHandler,
    LAYOUT_TOGGLE_MODULE.toggle_session_layout,
)


def main(args: list[str]) -> None:
    """Provide the conventional kitten entry point skipped by no-UI mode."""
    del args


@result_handler(no_ui=True)
def handle_result(
    args: list[str],
    answer: object,
    target_window_id: int,
    boss: Boss,
) -> None:
    """Toggle zoom without opening a pane, overlay, or subprocess."""
    del args, answer, target_window_id
    toggle_session_layout(cast("LayoutBoss", boss))
