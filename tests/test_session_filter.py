from __future__ import annotations

import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

from kisesh.kitty_actions import (
    SessionReloadBoss,
    reload_config_preserving_session,
    set_session_filter,
)
from kisesh.model import KISESH_UI_VAR, SESSION_ID_VAR, SESSION_SCOPE_VAR


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


@dataclass(slots=True)
class Window:
    user_vars: dict[str, str]


@dataclass(slots=True)
class Tab:
    windows: list[Window]

    def __iter__(self) -> Iterator[Window]:
        return iter(self.windows)


@dataclass(slots=True)
class ReloadBoss:
    all_tab_managers: list[Manager]
    active_tab: Tab | None
    options: Options
    reloaded_options: Options
    events: list[str] = field(default_factory=list)

    def load_config_file(self) -> None:
        self.events.append("reload")
        self.options = self.reloaded_options

    def get_options(self) -> Options:
        self.events.append("options")
        return self.options


class SessionFilterTests(unittest.TestCase):
    def test_filter_refresh_changes_only_filter_and_rebuilds_every_native_bar(self) -> None:
        options = Options("all", 22.5, "custom-runtime-theme")
        managers = [Manager(), Manager()]

        set_session_filter("var:kisesh_session=session-id", Boss(managers), options)

        self.assertEqual(options.tab_bar_filter, "var:kisesh_session=session-id")
        self.assertEqual(options.font_size, 22.5)
        self.assertEqual(options.theme, "custom-runtime-theme")
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 2)

    def test_reload_uses_fresh_options_and_restores_the_focused_session_only(self) -> None:
        previous = Options("runtime-session-filter", 22.5, "runtime-theme")
        configured = Options("all", 16.0, "reloaded-theme")
        windows = [
            Window({SESSION_ID_VAR: "focused", SESSION_SCOPE_VAR: "41"}),
            Window({KISESH_UI_VAR: "yes"}),
            Window({SESSION_ID_VAR: "focused", SESSION_SCOPE_VAR: "41"}),
        ]
        managers = [Manager(), Manager()]
        boss = ReloadBoss(managers, Tab(windows), previous, configured)

        reload_config_preserving_session(
            cast(SessionReloadBoss, boss),
            boss.get_options,
        )

        self.assertEqual(boss.events, ["reload", "options"])
        self.assertIs(boss.options, configured)
        self.assertEqual(previous.tab_bar_filter, "runtime-session-filter")
        self.assertEqual(
            configured.tab_bar_filter,
            f"var:{SESSION_ID_VAR}=focused or not var:{SESSION_SCOPE_VAR}=41",
        )
        self.assertEqual((configured.font_size, configured.theme), (16.0, "reloaded-theme"))
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 2)

    def test_reload_respects_config_filter_without_one_complete_session_target(self) -> None:
        scenarios = {
            "no active tab": None,
            "manager only": Tab([Window({KISESH_UI_VAR: "yes"})]),
            "missing scope": Tab([Window({SESSION_ID_VAR: "partial"})]),
            "missing session": Tab([Window({SESSION_SCOPE_VAR: "41"})]),
            "mixed ownership": Tab(
                [
                    Window({SESSION_ID_VAR: "one", SESSION_SCOPE_VAR: "41"}),
                    Window({SESSION_ID_VAR: "two", SESSION_SCOPE_VAR: "41"}),
                ]
            ),
        }
        for label, tab in scenarios.items():
            with self.subTest(label=label):
                configured = Options("configured-filter", 16.0, "reloaded-theme")
                manager = Manager()
                boss = ReloadBoss(
                    [manager],
                    tab,
                    Options("old-filter", 22.5, "old-theme"),
                    configured,
                )

                reload_config_preserving_session(
                    cast(SessionReloadBoss, boss),
                    boss.get_options,
                )

                self.assertEqual(boss.events, ["reload"])
                self.assertEqual(configured.tab_bar_filter, "configured-filter")
                self.assertEqual(manager.events, [])
