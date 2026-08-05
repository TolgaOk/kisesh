"""Update Kitty's live session filter without reloading unrelated options."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


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
