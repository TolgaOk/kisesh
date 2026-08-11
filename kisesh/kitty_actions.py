"""Apply KiSesh layout, filter, and close transitions inside Kitty."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from .model import KISESH_UI_VAR, SESSION_ID_VAR, SESSION_SCOPE_VAR


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


@dataclass(frozen=True, slots=True)
class SessionFilterTarget:
    """Selected session identity for one Kitty operating-system window."""

    session_id: str
    scope: str

    @property
    def expression(self) -> str:
        """Return the scoped Kitty query that isolates this session."""
        return f"var:{SESSION_ID_VAR}={self.session_id} or not var:{SESSION_SCOPE_VAR}={self.scope}"


def session_filter_expression(targets: Iterable[SessionFilterTarget]) -> str:
    """Combine deterministic per-window selections into one global Kitty filter."""
    ordered = sorted(set(targets), key=lambda target: (target.scope, target.session_id))
    if not ordered:
        return "all"
    if len(ordered) == 1:
        return ordered[0].expression
    return " and ".join(f"({target.expression})" for target in ordered)


def parse_session_filter_expression(
    expression: str,
) -> tuple[SessionFilterTarget, ...] | None:
    """Decode only the canonical filter grammar emitted by KiSesh."""
    target_pattern = re.compile(
        rf"var:{re.escape(SESSION_ID_VAR)}=(?P<session_id>[^\s()]+) "
        rf"or not var:{re.escape(SESSION_SCOPE_VAR)}=(?P<scope>[^\s()]+)"
    )
    clauses = (
        tuple(expression[1:-1].split(") and ("))
        if expression.startswith("(") and expression.endswith(")")
        else (expression,)
    )
    targets = tuple(
        SessionFilterTarget(match["session_id"], match["scope"])
        for clause in clauses
        if (match := target_pattern.fullmatch(clause)) is not None
    )
    if len(targets) != len(clauses):
        return None
    return targets if session_filter_expression(targets) == expression else None


class SessionReloadWindow(Protocol):
    """Pane variables used to recover a selected session after a lost filter."""

    user_vars: dict[str, str]


class SessionReloadTab(Protocol):
    """Native tab contents used to identify one selected session."""

    def __iter__(self) -> Iterator[SessionReloadWindow]:
        """Yield every pane, including any transient KiSesh overlay."""


class SessionReloadManager(SessionFilterManager, Protocol):
    """Per-window selection used to recover a lost runtime filter."""

    os_window_id: int
    active_tab: SessionReloadTab | None


class SessionReloadBoss(SessionFilterBoss, Protocol):
    """Kitty controller operations required for a session-safe reload."""

    @property
    def all_tab_managers(self) -> Iterable[SessionReloadManager]:
        """Return every OS-window manager and its independently active tab."""

    def load_config_file(self) -> None:
        """Reload and apply Kitty's configured options."""


def _selected_session_target(manager: SessionReloadManager) -> SessionFilterTarget | None:
    """Recover one unambiguous session identity from a native window selection."""
    if manager.active_tab is None:
        return None
    session_ids = {
        session_id
        for window in manager.active_tab
        if window.user_vars.get(KISESH_UI_VAR) != "yes"
        if (session_id := window.user_vars.get(SESSION_ID_VAR))
    }
    if len(session_ids) != 1:
        return None
    return SessionFilterTarget(session_ids.pop(), str(manager.os_window_id))


def reload_config_preserving_session(
    boss: SessionReloadBoss,
    options_provider: Callable[[], SessionFilterOptions],
) -> None:
    """Reload Kitty and retain or recover its runtime session isolation."""
    expression = options_provider().tab_bar_filter
    owned_expression = (
        expression if parse_session_filter_expression(expression) is not None else None
    )
    if owned_expression is None and expression == "all":
        targets = tuple(
            target
            for manager in boss.all_tab_managers
            if (target := _selected_session_target(manager)) is not None
        )
        owned_expression = session_filter_expression(targets) if targets else None
    boss.load_config_file()
    if owned_expression is not None:
        set_session_filter(owned_expression, boss, options_provider())


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


class SessionCloseManager(SessionFilterManager, Protocol):
    """Per-window active-tab state required to preserve other selections."""

    os_window_id: int
    active_tab: SessionCloseTab | None


class SessionCloseBoss(SessionFilterBoss, Protocol):
    """Native Kitty controller operations required by an atomic close."""

    @property
    def all_tabs(self) -> Iterable[SessionCloseTab]:
        """Yield every live tab independently of the visible tab filter."""

    @property
    def all_tab_managers(self) -> Iterable[SessionCloseManager]:
        """Yield every OS-window manager and its independently active tab."""

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
    targets = (
        {SessionFilterTarget(successor_session_id, scope)}
        if successor_session_id is not None and scope is not None
        else set()
    )
    closing_identities = {(tab.os_window_id, tab.id) for tab in closing_tabs}
    for manager in boss.all_tab_managers:
        manager_scope = str(manager.os_window_id)
        active = manager.active_tab
        if manager_scope == scope or active is None:
            continue
        active_session_id = _tab_session_id(active)
        has_scope = any(
            window.user_vars.get(SESSION_SCOPE_VAR) == manager_scope for window in active
        )
        if (
            active_session_id is not None
            and has_scope
            and (active.os_window_id, active.id) not in closing_identities
        ):
            targets.add(SessionFilterTarget(active_session_id, manager_scope))
    managed_scopes = {target.scope for target in targets}
    for tab in tabs:
        tab_scope = str(tab.os_window_id) if str(tab.os_window_id) in managed_scopes else None
        for window in tab:
            if (
                window.user_vars.get(KISESH_UI_VAR) != "yes"
                and window.user_vars.get(SESSION_SCOPE_VAR) != tab_scope
            ):
                window.set_user_var(SESSION_SCOPE_VAR, tab_scope)
    if successor is not None and successor.active_window is not None:
        boss.set_active_window(successor.active_window, switch_os_window_if_needed=True)
    set_session_filter(session_filter_expression(targets), boss, options)
    for tab in closing_tabs:
        boss.close_tab_no_confirm(tab)
