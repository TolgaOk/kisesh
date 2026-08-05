"""Provide reliable zoom toggling for restored Kitty session tabs."""

from __future__ import annotations

from typing import Protocol


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
