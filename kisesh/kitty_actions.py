"""Apply KiSesh layout, filter, and close transitions inside Kitty."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from .model import SESSION_ID_VAR, SESSION_SCOPE_VAR


class CurrentLayout(Protocol):
    """Layout identity exposed by a live Kitty tab."""

    name: str


class LayoutTab(Protocol):
    """Live Kitty tab operations needed to toggle its zoom layout."""

    @property
    def current_layout(self) -> CurrentLayout:
        """Return the tab's current layout."""

    def goto_layout(self, layout_name: str) -> None:
        """Switch the tab to a named enabled layout."""

    def last_used_layout(self) -> None:
        """Return to the previous layout when Kitty has recorded one."""


class LayoutBoss(Protocol):
    """Subset of Kitty's controller needed by the layout action."""

    @property
    def active_tab(self) -> LayoutTab | None:
        """Return the active native tab, if one still exists."""


def toggle_session_layout(boss: LayoutBoss) -> None:
    """Toggle stack zoom and fall back to splits after a direct stack restore."""
    tab = boss.active_tab
    if tab is None:
        return
    if tab.current_layout.name != "stack":
        tab.goto_layout("stack")
        return
    tab.last_used_layout()
    if tab.current_layout.name == "stack":
        tab.goto_layout("splits")


class SessionFilterOptions(Protocol):
    """Mutable Kitty option required by the native session filter."""

    tab_bar_filter: str


class SessionFilterManager(Protocol):
    """Native tab-manager operations required after a filter change."""

    def mark_tab_bar_dirty(self) -> None:
        """Request a native tab-bar repaint."""

    def update_tab_bar_data(self) -> None:
        """Rebuild visible native tab data with the current filter."""


class SessionFilterBoss(Protocol):
    """Kitty controller state required to refresh every native tab bar."""

    @property
    def all_tab_managers(self) -> Iterable[SessionFilterManager]:
        """Return every native tab manager controlled by this Kitty process."""


def set_session_filter(
    expression: str,
    boss: SessionFilterBoss,
    options: SessionFilterOptions,
) -> None:
    """Apply one tab filter without resetting runtime font or theme state."""
    options.tab_bar_filter = expression
    for manager in boss.all_tab_managers:
        manager.mark_tab_bar_dirty()
        manager.update_tab_bar_data()


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
    """Read one tab's session marker."""
    return next((value for window in tab if (value := window.user_vars.get(SESSION_ID_VAR))), None)


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
    for tab in tabs:
        tab_scope = scope if tab.os_window_id == successor_os_window_id else None
        for window in tab:
            window.set_user_var(SESSION_SCOPE_VAR, tab_scope)
    if successor is not None and successor.active_window is not None:
        boss.set_active_window(successor.active_window, switch_os_window_if_needed=True)
    expression = "all"
    if successor is not None and scope is not None:
        expression = (
            f"var:{SESSION_ID_VAR}={successor_session_id} or not var:{SESSION_SCOPE_VAR}={scope}"
        )
    set_session_filter(expression, boss, options)
    for tab in closing_tabs:
        boss.close_tab_no_confirm(tab)
