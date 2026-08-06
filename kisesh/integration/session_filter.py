"""No-UI Kitty kitten that changes only KiSesh's native tab filter."""

from __future__ import annotations

import os
import sys
from importlib import import_module, reload
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from kittens.tui.handler import result_handler
from kitty.fast_data_types import get_options

if TYPE_CHECKING:
    from kitty.boss import Boss

    from kisesh.session_filter import SessionFilterBoss, SessionFilterOptions


class SessionFilterHandler(Protocol):
    """Callable contract exported by the installable session-filter module."""

    def __call__(
        self,
        expression: str,
        boss: SessionFilterBoss,
        options: SessionFilterOptions,
    ) -> None:
        """Apply one filter without reloading Kitty's other options."""


PROJECT_ROOT = Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SESSION_FILTER_MODULE = reload(import_module("kisesh.session_filter"))
set_session_filter = cast(SessionFilterHandler, SESSION_FILTER_MODULE.set_session_filter)


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
    """Apply the requested filter without opening a pane or reloading config."""
    del answer, target_window_id
    if len(args) != 2:
        raise ValueError("session filter requires exactly one expression")
    set_session_filter(
        args[1],
        cast("SessionFilterBoss", boss),
        cast("SessionFilterOptions", get_options()),
    )
