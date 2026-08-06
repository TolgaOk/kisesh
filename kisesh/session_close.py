"""Close one live session without exposing unrelated native Kitty tabs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from .legacy import VARIABLE_ALIASES as LEGACY_VARIABLE_ALIASES
from .model import SESSION_ID_VAR, SESSION_SCOPE_VAR
from .session_filter import (
    SessionFilterBoss,
    SessionFilterOptions,
    set_session_filter,
)


class SessionCloseWindow(Protocol):
    """Native Kitty pane operations required by an atomic session close."""

    user_vars: dict[str, str]

    def set_user_var(self, key: str, value: str | bytes | None) -> None:
        """Set or clear one native pane variable."""


class SessionCloseTab(Protocol):
    """Native Kitty tab state required by an atomic session close."""

    id: int
    os_window_id: int
    active_window: SessionCloseWindow | None

    def __iter__(self) -> Iterator[SessionCloseWindow]:
        """Yield every pane in this tab, including transient overlays."""


class SessionCloseBoss(SessionFilterBoss, Protocol):
    """Native Kitty controller operations required by an atomic close."""

    @property
    def all_tabs(self) -> Iterable[SessionCloseTab]:
        """Yield every live tab independently of the visible tab filter."""

    def set_active_window(
        self,
        window: SessionCloseWindow,
        switch_os_window_if_needed: bool = False,
    ) -> int | None:
        """Focus a pane and its containing native tab."""

    def close_tab_no_confirm(self, tab: SessionCloseTab) -> None:
        """Mark every pane in one already-confirmed tab for closure."""


def _tab_session_id(tab: SessionCloseTab) -> str | None:
    """Read one tab's current or compatibility session marker."""
    names = (SESSION_ID_VAR, LEGACY_VARIABLE_ALIASES[SESSION_ID_VAR])
    return next(
        (value for window in tab for name in names if (value := window.user_vars.get(name))),
        None,
    )


def close_live_session(
    session_id: str,
    successor_session_id: str | None,
    successor_tab_id: int | None,
    boss: SessionCloseBoss,
    options: SessionFilterOptions,
) -> None:
    """Filter to a surviving session before closing tabs that may host the caller."""
    tabs = tuple(boss.all_tabs)
    closing_tabs = tuple(tab for tab in tabs if _tab_session_id(tab) == session_id)
    if not closing_tabs:
        return
    successor = next(
        (
            tab
            for tab in tabs
            if successor_session_id is not None
            and successor_tab_id is not None
            and tab.id == successor_tab_id
            and _tab_session_id(tab) == successor_session_id
            and successor_session_id != session_id
        ),
        None,
    )
    successor_os_window_id = successor.os_window_id if successor is not None else None
    scope = str(successor_os_window_id) if successor_os_window_id is not None else None
    legacy_scope = LEGACY_VARIABLE_ALIASES[SESSION_SCOPE_VAR]
    for tab in tabs:
        tab_scope = scope if tab.os_window_id == successor_os_window_id else None
        for window in tab:
            window.set_user_var(SESSION_SCOPE_VAR, tab_scope)
            window.set_user_var(legacy_scope, None)
    if successor is not None and successor.active_window is not None:
        boss.set_active_window(successor.active_window, switch_os_window_if_needed=True)
    expression = "all"
    if successor is not None and scope is not None:
        legacy_session = LEGACY_VARIABLE_ALIASES[SESSION_ID_VAR]
        expression = (
            f"var:{SESSION_ID_VAR}={successor_session_id} or "
            f"var:{legacy_session}={successor_session_id} or "
            f"not var:{SESSION_SCOPE_VAR}={scope}"
        )
    set_session_filter(expression, boss, options)
    for tab in closing_tabs:
        boss.close_tab_no_confirm(tab)
