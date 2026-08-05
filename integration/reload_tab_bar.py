"""No-UI Kitty kitten that refreshes the native custom tab-bar renderer."""

from __future__ import annotations

import sys
from importlib import import_module, reload
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from kittens.tui.handler import result_handler

if TYPE_CHECKING:
    from kitty.boss import Boss

    from kitty_workbench.session_bar import SessionBarBoss


class ReloadHandler(Protocol):
    """Callable contract exported by the installable session-bar module."""

    def __call__(self, boss: SessionBarBoss) -> None:
        """Clear Kitty's cached renderer and redraw native tab bars."""


PROJECT_ROOT = Path.home() / ".local" / "lib" / "kitty-workbench"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SESSION_BAR_MODULE = reload(import_module("kitty_workbench.session_bar"))
reload_session_bar = cast(ReloadHandler, SESSION_BAR_MODULE.reload_session_bar)


def main(args: list[str]) -> None:
    """Provide the conventional kitten entry point skipped by no-UI mode."""
    del args


@result_handler(no_ui=True)
def handle_result(
    args: list[str],
    answer: object,
    target_window_id: int,
    boss: Boss,
) -> str:
    """Refresh the renderer without opening a pane, overlay, or subprocess."""
    del args, answer, target_window_id
    reload_session_bar(cast("SessionBarBoss", boss))
    return "native session bar reloaded"
