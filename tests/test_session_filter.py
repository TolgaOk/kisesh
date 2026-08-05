from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from kitty_workbench.session_filter import set_session_filter


@dataclass(slots=True)
class Options:
    tab_bar_filter: str
    font_size: float
    theme: str


@dataclass(slots=True)
class Manager:
    events: list[str] = field(default_factory=list)

    def mark_tab_bar_dirty(self) -> None:
        self.events.append("dirty")

    def update_tab_bar_data(self) -> None:
        self.events.append("update")


@dataclass(slots=True)
class Boss:
    all_tab_managers: list[Manager]


class SessionFilterTests(unittest.TestCase):
    def test_filter_refresh_changes_only_filter_and_rebuilds_every_native_bar(self) -> None:
        options = Options("all", 22.5, "custom-runtime-theme")
        managers = [Manager(), Manager()]

        set_session_filter("var:kitty_workbench_session=session-id", Boss(managers), options)

        self.assertEqual(options.tab_bar_filter, "var:kitty_workbench_session=session-id")
        self.assertEqual(options.font_size, 22.5)
        self.assertEqual(options.theme, "custom-runtime-theme")
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 2)
