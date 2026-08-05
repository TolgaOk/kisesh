"""No-UI Kitty kitten that routes Command-W through Workbench safeguards."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from kittens.tui.handler import result_handler

if TYPE_CHECKING:
    from kitty.boss import Boss


class CloseHandler(Protocol):
    """Callable contract exported by the installable close-guard module."""

    def __call__(self, target_window_id: int, boss: Boss) -> None:
        """Route one Kitty close request."""


PROJECT_ROOT = Path.home() / ".local" / "lib" / "kitty-workbench"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
request_tab_close = cast(
    CloseHandler,
    import_module("kitty_workbench.close_guard").request_tab_close,
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
    """Route Command-W without creating a terminal overlay process."""
    del args, answer
    request_tab_close(target_window_id, boss)
