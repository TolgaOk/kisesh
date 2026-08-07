"""Dispatch KiSesh's no-UI Kitty actions through one runtime entry point."""

from __future__ import annotations

import os
import sys
from importlib import import_module, reload
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from kisesh.integration.kitty_api import get_options, result_handler
    from kisesh.kitty_actions import (
        LayoutBoss,
        SessionCloseBoss,
        SessionFilterBoss,
        SessionFilterOptions,
    )


class LayoutToggleHandler(Protocol):
    """Callable contract for Kitty-native layout toggling."""

    def __call__(self, boss: LayoutBoss) -> None:
        """Toggle the active KiSesh tab's zoom layout."""


class CloseHandler(Protocol):
    """Callable contract for guarded tab closing."""

    def __call__(self, target_window_id: int, boss: object) -> None:
        """Route one Kitty close request."""


class SessionCloseHandler(Protocol):
    """Callable contract for atomic live-session closing."""

    def __call__(
        self,
        session_id: str,
        successor_session_id: str | None,
        successor_tab_id: int | None,
        boss: SessionCloseBoss,
        options: SessionFilterOptions,
    ) -> None:
        """Close one live session and preserve an optional successor."""


class SessionFilterHandler(Protocol):
    """Callable contract for changing Kitty's native tab filter."""

    def __call__(
        self,
        expression: str,
        boss: SessionFilterBoss,
        options: SessionFilterOptions,
    ) -> None:
        """Apply one filter without reloading unrelated Kitty options."""


def _add_runtime_import_path() -> None:
    """Expose the runtime package before importing shared KiSesh behavior."""
    runtime = str(Path(os.environ.get("KISESH_INSTALL_ROOT", "~/.local/lib/kisesh")).expanduser())
    if runtime not in sys.path:
        sys.path.insert(0, runtime)


_add_runtime_import_path()
kitty_api = import_module("kisesh.integration.kitty_api")
get_options = kitty_api.get_options
result_handler = kitty_api.result_handler
kitty_actions = reload(import_module("kisesh.kitty_actions"))
toggle_session_layout = cast(LayoutToggleHandler, kitty_actions.toggle_session_layout)
close_live_session = cast(SessionCloseHandler, kitty_actions.close_live_session)
set_session_filter = cast(SessionFilterHandler, kitty_actions.set_session_filter)
request_tab_close = cast(
    CloseHandler,
    import_module("kisesh.close_guard").request_tab_close,
)


def main(args: list[str]) -> None:
    """Provide the conventional kitten entry point skipped by no-UI mode."""
    del args


@result_handler(no_ui=True)
def handle_result(
    args: list[str],
    answer: object,
    target_window_id: int,
    boss: object,
) -> None:
    """Validate and dispatch one in-process action without opening a pane."""
    del answer
    action = args[1] if len(args) > 1 else ""
    payload = args[2:]
    if action == "layout-toggle" and not payload:
        toggle_session_layout(cast("LayoutBoss", boss))
        return
    if action == "safe-close" and not payload:
        request_tab_close(target_window_id, boss)
        return
    if action == "session-filter":
        if len(payload) != 1:
            raise ValueError("session filter requires exactly one expression")
        set_session_filter(
            payload[0],
            cast("SessionFilterBoss", boss),
            cast("SessionFilterOptions", get_options()),
        )
        return
    if action == "session-close":
        if len(payload) != 3:
            raise ValueError("session close requires a target and one optional successor")
        successor_session_id = None if payload[1] == "-" else payload[1]
        successor_tab_id = None if payload[2] == "-" else int(payload[2])
        if (successor_session_id is None) != (successor_tab_id is None):
            raise ValueError("session close successor identity is incomplete")
        close_live_session(
            payload[0],
            successor_session_id,
            successor_tab_id,
            cast("SessionCloseBoss", boss),
            cast("SessionFilterOptions", get_options()),
        )
        return
    raise ValueError(f"unknown KiSesh action: {action or 'missing'}")
