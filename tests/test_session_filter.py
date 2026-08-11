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
class ReloadManager(Manager):
    os_window_id: int = 0
    active_tab: Tab | None = None


@dataclass(slots=True)
class ReloadBoss:
    all_tab_managers: list[ReloadManager]
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

    def test_reload_uses_fresh_options_and_preserves_all_window_selections(self) -> None:
        runtime_filter = (
            "(var:kisesh_session=left or not var:kisesh_scope=31) and "
            "(var:kisesh_session=focused or not var:kisesh_scope=41)"
        )
        previous = Options(runtime_filter, 22.5, "runtime-theme")
        configured = Options("all", 16.0, "reloaded-theme")
        windows = [
            Window({SESSION_ID_VAR: "focused"}),
            Window({KISESH_UI_VAR: "yes"}),
            Window({SESSION_ID_VAR: "focused", SESSION_SCOPE_VAR: "41"}),
        ]
        managers = [
            ReloadManager(
                os_window_id=31,
                active_tab=Tab([Window({SESSION_ID_VAR: "left"})]),
            ),
            ReloadManager(os_window_id=41, active_tab=Tab(windows)),
        ]
        boss = ReloadBoss(managers, previous, configured)

        reload_config_preserving_session(
            cast(SessionReloadBoss, boss),
            boss.get_options,
        )

        self.assertEqual(boss.events, ["options", "reload", "options"])
        self.assertIs(boss.options, configured)
        self.assertEqual(previous.tab_bar_filter, runtime_filter)
        self.assertEqual(configured.tab_bar_filter, runtime_filter)
        self.assertEqual((configured.font_size, configured.theme), (16.0, "reloaded-theme"))
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 2)

    def test_reload_recovers_each_window_selection_after_filter_was_already_lost(self) -> None:
        previous = Options("all", 22.5, "runtime-theme")
        configured = Options("all", 16.0, "reloaded-theme")
        managers = [
            ReloadManager(
                os_window_id=31,
                active_tab=Tab(
                    [
                        Window({SESSION_ID_VAR: "left"}),
                        Window({}),
                    ]
                ),
            ),
            ReloadManager(
                os_window_id=41,
                active_tab=Tab([Window({SESSION_ID_VAR: "focused"})]),
            ),
            ReloadManager(os_window_id=51, active_tab=Tab([Window({})])),
            ReloadManager(os_window_id=61),
            ReloadManager(
                os_window_id=71,
                active_tab=Tab(
                    [
                        Window({SESSION_ID_VAR: "one"}),
                        Window({SESSION_ID_VAR: "two"}),
                    ]
                ),
            ),
        ]
        boss = ReloadBoss(managers, previous, configured)

        reload_config_preserving_session(cast(SessionReloadBoss, boss), boss.get_options)

        self.assertEqual(boss.events, ["options", "reload", "options"])
        self.assertEqual(
            configured.tab_bar_filter,
            "(var:kisesh_session=left or not var:kisesh_scope=31) and "
            "(var:kisesh_session=focused or not var:kisesh_scope=41)",
        )
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 5)

    def test_reload_respects_config_filter_when_runtime_filter_is_not_owned(self) -> None:
        runtime_filters = {
            "default": "all",
            "configured query": "title:work",
            "missing scope": "var:kisesh_session=partial",
            "missing session": "not var:kisesh_scope=41",
            "partly owned conjunction": (
                "(var:kisesh_session=one or not var:kisesh_scope=41) and (title:work)"
            ),
            "wrapped single target": ("(var:kisesh_session=one or not var:kisesh_scope=41)"),
            "noncanonical target order": (
                "(var:kisesh_session=two or not var:kisesh_scope=42) and "
                "(var:kisesh_session=one or not var:kisesh_scope=41)"
            ),
        }
        for label, runtime_filter in runtime_filters.items():
            with self.subTest(label=label):
                configured = Options("configured-filter", 16.0, "reloaded-theme")
                manager = ReloadManager()
                boss = ReloadBoss(
                    [manager],
                    Options(runtime_filter, 22.5, "old-theme"),
                    configured,
                )

                reload_config_preserving_session(
                    cast(SessionReloadBoss, boss),
                    boss.get_options,
                )

                self.assertEqual(boss.events, ["options", "reload"])
                self.assertEqual(configured.tab_bar_filter, "configured-filter")
                self.assertEqual(manager.events, [])
