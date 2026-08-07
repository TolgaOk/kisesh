from __future__ import annotations

import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

from kisesh.kitty_actions import SessionCloseBoss, close_live_session
from kisesh.model import KISESH_UI_VAR, SESSION_ID_VAR, SESSION_SCOPE_VAR

Event = tuple[str, object]


@dataclass(slots=True)
class Window:
    id: int
    user_vars: dict[str, str]
    events: list[Event]

    def set_user_var(self, key: str, value: str | bytes | None) -> None:
        self.events.append(("variable", (self.id, key, value)))
        if value is None:
            self.user_vars.pop(key, None)
        else:
            self.user_vars[key] = value.decode() if isinstance(value, bytes) else value


@dataclass(slots=True)
class Tab:
    id: int
    os_window_id: int
    windows: list[Window]
    active_window: Window | None = None

    def __post_init__(self) -> None:
        if self.active_window is None and self.windows:
            self.active_window = self.windows[0]

    def __iter__(self) -> Iterator[Window]:
        return iter(self.windows)


@dataclass(slots=True)
class Manager:
    events: list[Event]

    def mark_tab_bar_dirty(self) -> None:
        self.events.append(("bar", "dirty"))

    def update_tab_bar_data(self) -> None:
        self.events.append(("bar", "update"))


@dataclass(slots=True)
class Options:
    tab_bar_filter: str
    font_size: float
    theme: str


@dataclass(slots=True)
class Boss:
    tabs: list[Tab]
    events: list[Event]
    managers: list[Manager] = field(default_factory=list)

    @property
    def all_tabs(self) -> list[Tab]:
        return list(self.tabs)

    @property
    def all_tab_managers(self) -> list[Manager]:
        return self.managers

    def set_active_window(
        self,
        window: Window,
        switch_os_window_if_needed: bool = False,
    ) -> int | None:
        self.events.append(("focus", (window.id, switch_os_window_if_needed)))
        return window.id

    def close_tab_no_confirm(self, tab: Tab) -> None:
        self.events.append(("close", tab.id))
        self.tabs.remove(tab)


def session_tab(
    tab_id: int,
    os_window_id: int,
    session_id: str,
    events: list[Event],
    *,
    overlay: bool = False,
) -> Tab:
    windows = [Window(tab_id * 10, {SESSION_ID_VAR: session_id}, events)]
    if overlay:
        windows.append(Window(tab_id * 10 + 1, {KISESH_UI_VAR: "yes"}, events))
    return Tab(tab_id, os_window_id, windows, windows[-1])


class SessionCloseTests(unittest.TestCase):
    def test_filter_and_focus_finish_before_closing_the_manager_host_session(self) -> None:
        events: list[Event] = []
        closing = session_tab(1, 7, "closing", events, overlay=True)
        successor = session_tab(2, 7, "successor", events)
        hidden = session_tab(3, 7, "hidden", events)
        other_window = session_tab(4, 8, "other", events)
        other_window.windows[0].user_vars[SESSION_SCOPE_VAR] = "stale"
        managers = [Manager(events), Manager(events)]
        boss = Boss([closing, successor, hidden, other_window], events, managers)
        options = Options("all", 21.5, "runtime-theme")

        close_live_session("closing", "successor", 2, cast(SessionCloseBoss, boss), options)

        self.assertEqual([tab.id for tab in boss.tabs], [2, 3, 4])
        self.assertEqual(events[-1], ("close", 1))
        self.assertLess(events.index(("focus", (20, True))), events.index(("close", 1)))
        self.assertLess(events.index(("bar", "update")), events.index(("close", 1)))
        self.assertEqual(
            options.tab_bar_filter,
            f"var:{SESSION_ID_VAR}=successor or not var:{SESSION_SCOPE_VAR}=7",
        )
        self.assertEqual(options.font_size, 21.5)
        self.assertEqual(options.theme, "runtime-theme")
        for tab in (successor, hidden):
            for window in tab:
                self.assertEqual(window.user_vars[SESSION_SCOPE_VAR], "7")
        self.assertNotIn(SESSION_SCOPE_VAR, other_window.windows[0].user_vars)
        self.assertEqual(
            [event for event in events if event[0] == "bar"],
            [("bar", "dirty"), ("bar", "update")] * 2,
        )

    def test_close_without_a_successor_clears_scopes_and_reveals_unowned_tabs(self) -> None:
        events: list[Event] = []
        closing = session_tab(1, 7, "closing", events)
        unowned = Tab(
            2,
            7,
            [
                Window(
                    20,
                    {SESSION_SCOPE_VAR: "7"},
                    events,
                )
            ],
        )
        boss = Boss([closing, unowned], events, [Manager(events)])
        options = Options("broken", 13.0, "theme")

        close_live_session("closing", None, None, cast(SessionCloseBoss, boss), options)

        self.assertEqual([tab.id for tab in boss.tabs], [2])
        self.assertEqual(options.tab_bar_filter, "all")
        self.assertFalse(any(event[0] == "focus" for event in events))
        self.assertNotIn(SESSION_SCOPE_VAR, unowned.windows[0].user_vars)

    def test_missing_target_is_a_complete_no_op(self) -> None:
        events: list[Event] = []
        remaining = session_tab(2, 7, "remaining", events)
        boss = Boss([remaining], events, [Manager(events)])
        options = Options("existing-filter", 13.0, "theme")

        close_live_session("missing", "remaining", 2, cast(SessionCloseBoss, boss), options)

        self.assertEqual([tab.id for tab in boss.tabs], [2])
        self.assertEqual(options.tab_bar_filter, "existing-filter")
        self.assertEqual(events, [])

    def test_invalid_or_unfocusable_successor_never_preserves_the_closing_session(self) -> None:
        for successor_id, successor_tab_id in (
            ("other", None),
            (None, 2),
            ("other", 99),
            ("closing", 2),
        ):
            with self.subTest(successor_id=successor_id, successor_tab_id=successor_tab_id):
                events: list[Event] = []
                closing = session_tab(1, 7, "closing", events)
                candidate = session_tab(2, 7, "closing", events)
                boss = Boss([closing, candidate], events, [Manager(events)])
                options = Options("old", 13.0, "theme")

                close_live_session(
                    "closing",
                    successor_id,
                    successor_tab_id,
                    cast(SessionCloseBoss, boss),
                    options,
                )

                self.assertEqual(boss.tabs, [])
                self.assertEqual(options.tab_bar_filter, "all")
                self.assertFalse(any(event[0] == "focus" for event in events))

        events = []
        closing = session_tab(1, 7, "closing", events)
        successor = session_tab(2, 7, "other", events)
        successor.active_window = None
        boss = Boss([closing, successor], events, [Manager(events)])
        options = Options("old", 13.0, "theme")

        close_live_session("closing", "other", 2, cast(SessionCloseBoss, boss), options)

        self.assertEqual([tab.id for tab in boss.tabs], [2])
        self.assertIn("var:kisesh_session=other", options.tab_bar_filter)
        self.assertFalse(any(event[0] == "focus" for event in events))


if __name__ == "__main__":
    unittest.main()
