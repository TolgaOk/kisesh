from __future__ import annotations

import unittest
from dataclasses import dataclass

from kisesh.kitty_actions import toggle_session_layout


@dataclass(slots=True)
class Layout:
    """Mutable fake matching Kitty's current-layout identity."""

    name: str


class Tab:
    """Record layout operations with configurable previous-layout behavior."""

    def __init__(self, current: str, previous: str | None) -> None:
        """Initialize current and previously used layout identities."""
        self.current_layout = Layout(current)
        self.previous = previous
        self.operations: list[tuple[str, str | None]] = []

    def goto_layout(self, layout_name: str) -> None:
        """Record and apply a direct layout transition."""
        self.operations.append(("goto", layout_name))
        self.current_layout.name = layout_name

    def last_used_layout(self) -> None:
        """Record and apply Kitty's no-op-capable previous-layout transition."""
        self.operations.append(("last", None))
        if self.previous is not None:
            self.current_layout.name = self.previous


@dataclass(slots=True)
class Boss:
    """Fake Kitty controller exposing an optional active tab."""

    active_tab: Tab | None


class LayoutToggleTests(unittest.TestCase):
    def test_restored_stack_without_history_always_unzooms_to_splits(self) -> None:
        tab = Tab("stack", None)

        toggle_session_layout(Boss(tab))

        self.assertEqual(tab.current_layout.name, "splits")
        self.assertEqual(tab.operations, [("last", None), ("goto", "splits")])

    def test_existing_layout_history_and_zoom_direction_are_preserved(self) -> None:
        stacked = Tab("stack", "tall")
        tiled = Tab("splits", "stack")

        toggle_session_layout(Boss(stacked))
        toggle_session_layout(Boss(tiled))
        toggle_session_layout(Boss(None))

        self.assertEqual(stacked.current_layout.name, "tall")
        self.assertEqual(stacked.operations, [("last", None)])
        self.assertEqual(tiled.current_layout.name, "stack")
        self.assertEqual(tiled.operations, [("goto", "stack")])
