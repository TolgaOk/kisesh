from __future__ import annotations

import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import cast

from kisesh.kitty_actions import (
    ManagerCloseBoss,
    close_manager_overlay,
    toggle_session_layout,
)
from kisesh.model import KISESH_UI_VAR, RESTORE_LAYOUT_VAR


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


class ManagerWindow:
    """Minimal live pane state used by the manager-close action."""

    def __init__(self, window_id: int, variables: Mapping[str, str]) -> None:
        """Store one immutable-looking identity and mutable native variables."""
        self.id = window_id
        self.user_vars = dict(variables)


class ManagerLayoutTab(Tab):
    """Layout fake that also exposes its contained manager pane."""

    def __init__(self, window: ManagerWindow, *, reject_restore: bool = False) -> None:
        """Start stacked with optional restoration failure injection."""
        super().__init__("stack", None)
        self.window = window
        self.reject_restore = reject_restore

    def __iter__(self) -> Iterator[ManagerWindow]:
        """Yield the manager overlay as the tab's only test pane."""
        yield self.window

    def goto_layout(self, layout_name: str) -> None:
        """Apply the requested layout unless failure injection is active."""
        if self.reject_restore:
            raise RuntimeError("layout unavailable")
        super().goto_layout(layout_name)


class ManagerBoss:
    """Record focused-overlay close behavior inside Kitty's process."""

    def __init__(self, window: ManagerWindow, tab: ManagerLayoutTab) -> None:
        """Focus the supplied manager overlay and containing tab."""
        self.active_window: ManagerWindow | None = window
        self.active_tab: ManagerLayoutTab | None = tab
        self.window_id_map: Mapping[int, ManagerWindow] = {window.id: window}
        self.closed = False

    def close_window(self) -> None:
        """Record the native overlay dismissal."""
        self.closed = True


class UnavailableManagerBoss:
    """Model Kitty state disappearing while a queued close action resolves."""

    active_window: ManagerWindow | None = None
    active_tab: ManagerLayoutTab | None = None

    @property
    def window_id_map(self) -> Mapping[int, ManagerWindow]:
        """Reject access after the target operating-system window has closed."""
        raise RuntimeError("window state unavailable")

    def close_window(self) -> None:
        """Reject any attempt to close an already-disappeared overlay."""
        raise AssertionError("unavailable overlay cannot be closed")


class ManagerCloseTests(unittest.TestCase):
    """Exercise atomic restoration and dismissal of the manager overlay."""

    def test_recorded_layout_is_restored_before_the_overlay_closes(self) -> None:
        manager = ManagerWindow(
            11,
            {KISESH_UI_VAR: "yes", RESTORE_LAYOUT_VAR: "splits"},
        )
        tab = ManagerLayoutTab(manager)
        boss = ManagerBoss(manager, tab)

        close_manager_overlay(11, cast(ManagerCloseBoss, boss))

        self.assertEqual(tab.current_layout.name, "splits")
        self.assertEqual(tab.operations, [("goto", "splits")])
        self.assertTrue(boss.closed)

    def test_preexisting_stack_and_failed_restore_still_close_the_overlay(self) -> None:
        preexisting = ManagerWindow(12, {KISESH_UI_VAR: "yes"})
        preexisting_tab = ManagerLayoutTab(preexisting)
        preexisting_boss = ManagerBoss(preexisting, preexisting_tab)
        failing = ManagerWindow(
            13,
            {KISESH_UI_VAR: "yes", RESTORE_LAYOUT_VAR: "splits"},
        )
        failing_tab = ManagerLayoutTab(failing, reject_restore=True)
        failing_boss = ManagerBoss(failing, failing_tab)

        close_manager_overlay(12, cast(ManagerCloseBoss, preexisting_boss))
        close_manager_overlay(13, cast(ManagerCloseBoss, failing_boss))

        self.assertEqual(preexisting_tab.operations, [])
        self.assertTrue(preexisting_boss.closed)
        self.assertEqual(failing_tab.current_layout.name, "stack")
        self.assertTrue(failing_boss.closed)

    def test_only_the_focused_marked_overlay_can_change_or_close_its_tab(self) -> None:
        manager = ManagerWindow(14, {KISESH_UI_VAR: "yes", RESTORE_LAYOUT_VAR: "splits"})
        tab = ManagerLayoutTab(manager)

        for mutate in (
            lambda boss: setattr(boss, "active_window", None),
            lambda boss: setattr(boss, "active_tab", None),
            lambda boss: setattr(boss, "window_id_map", {}),
            lambda boss: boss.active_window.user_vars.pop(KISESH_UI_VAR),
        ):
            boss = ManagerBoss(manager, tab)
            manager.user_vars[KISESH_UI_VAR] = "yes"
            mutate(boss)
            close_manager_overlay(14, cast(ManagerCloseBoss, boss))
            self.assertFalse(boss.closed)

        self.assertEqual(tab.operations, [])

        close_manager_overlay(14, cast(ManagerCloseBoss, UnavailableManagerBoss()))
