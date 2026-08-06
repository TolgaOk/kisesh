"""No-UI Kitty kitten that closes a session after restoring tab isolation."""

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

    from kisesh.session_close import SessionCloseBoss
    from kisesh.session_filter import SessionFilterOptions


class SessionCloseHandler(Protocol):
    """Callable contract exported by the installable session-close module."""

    def __call__(
        self,
        session_id: str,
        successor_session_id: str | None,
        successor_tab_id: int | None,
        boss: SessionCloseBoss,
        options: SessionFilterOptions,
    ) -> None:
        """Close one session through Kitty's in-process state."""


PROJECT_ROOT = Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SESSION_CLOSE_MODULE = reload(import_module("kisesh.session_close"))
close_live_session = cast(SessionCloseHandler, SESSION_CLOSE_MODULE.close_live_session)


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
    """Apply one validated close transition without relying on the caller surviving."""
    del answer, target_window_id
    if len(args) != 4:
        raise ValueError("session close requires a target and one optional successor")
    successor_session_id = None if args[2] == "-" else args[2]
    successor_tab_id = None if args[3] == "-" else int(args[3])
    if (successor_session_id is None) != (successor_tab_id is None):
        raise ValueError("session close successor identity is incomplete")
    close_live_session(
        args[1],
        successor_session_id,
        successor_tab_id,
        cast("SessionCloseBoss", boss),
        cast("SessionFilterOptions", get_options()),
    )
