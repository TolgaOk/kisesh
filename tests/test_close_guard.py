"""Behavioral coverage for Kitty-native safe tab closing."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest import mock

from kisesh.close_guard import (
    CloseGuardBoss,
    CloseGuardTab,
    CloseGuardWindow,
    CloseRequest,
    TabOwnership,
    _close_command,
    _close_finished,
    _confirmed_close,
    _pending_sessions,
    _release_session,
    _reserve_session,
    _string_mapping,
    _tab_ownership,
    _window_environment,
    request_tab_close,
)
from kisesh.model import KISESH_UI_VAR, SESSION_ID_VAR, SESSION_SLUG_VAR


@dataclass(slots=True)
class FakeChild:
    environ: object = field(default_factory=dict)
    foreground_environ: object = field(default_factory=dict)


@dataclass(slots=True)
class FakeWindow:
    id: int
    user_vars: object = field(default_factory=dict)
    child: FakeChild | None = None
    overlay_parent: object = None
    assigned_vars: list[tuple[str, str | bytes | None]] = field(default_factory=list)
    reject_user_var: bool = False

    def set_user_var(self, key: str, value: str | bytes | None) -> None:
        if self.reject_user_var:
            raise RuntimeError("prompt disappeared")
        self.assigned_vars.append((key, value))


@dataclass(slots=True)
class FakeTab:
    id: int
    os_window_id: int
    windows: list[FakeWindow]

    def __iter__(self) -> Iterator[FakeWindow]:
        return iter(self.windows)


@dataclass(slots=True)
class Confirmation:
    message: str
    callback: Callable[..., None]
    args: tuple[object, ...]
    keyword_arguments: dict[str, object]


@dataclass(slots=True)
class BackgroundRequest:
    command: list[str]
    keyword_arguments: dict[str, object]


class FakeBoss:
    def __init__(self, active_tab: FakeTab | None, tabs: list[FakeTab] | None = None) -> None:
        self.active_tab = active_tab
        self.active_window = active_tab.windows[0] if active_tab is not None else None
        self.window_id_map: Mapping[int, FakeWindow] = (
            {window.id: window for window in active_tab.windows} if active_tab is not None else {}
        )
        self.tabs = tabs if tabs is not None else ([active_tab] if active_tab is not None else [])
        self.closed_tabs: list[int] = []
        self.closed_windows = 0
        self.confirmations: list[Confirmation] = []
        self.background_requests: list[BackgroundRequest] = []
        self.errors: list[tuple[str, str]] = []
        self.prompt = FakeWindow(999, overlay_parent=1)
        self.reject_match = False
        self.reject_close_tab = False
        self.reject_close_window = False
        self.reject_confirmation = False
        self.reject_background = False

    def match_tabs(self, expression: str) -> list[FakeTab]:
        self.assert_expression(expression)
        if self.reject_match:
            raise RuntimeError("Kitty state unavailable")
        return self.tabs

    def assert_expression(self, expression: str) -> None:
        if expression != "all":
            raise AssertionError(f"unexpected expression: {expression}")

    def close_tab(self, tab: FakeTab | None = None) -> None:
        if self.reject_close_tab:
            raise RuntimeError("tab already closed")
        if tab is not None:
            self.closed_tabs.append(tab.id)

    def close_window(self) -> None:
        if self.reject_close_window:
            raise RuntimeError("window already closed")
        self.closed_windows += 1

    def confirm(
        self,
        message: str,
        callback: Callable[..., None],
        *args: object,
        window: FakeWindow | None = None,
        confirm_on_cancel: bool = False,
        confirm_on_accept: bool = True,
        title: str = "",
    ) -> FakeWindow:
        if self.reject_confirmation:
            raise RuntimeError("confirmation unavailable")
        self.confirmations.append(
            Confirmation(
                message,
                callback,
                args,
                {
                    "window": window,
                    "confirm_on_cancel": confirm_on_cancel,
                    "confirm_on_accept": confirm_on_accept,
                    "title": title,
                },
            )
        )
        return self.prompt

    def answer(self, confirmed: bool) -> None:
        confirmation = self.confirmations[-1]
        confirmation.callback(confirmed, *confirmation.args)

    def run_background_process(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        notify_on_death: Callable[[int, Exception | None], None] | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> None:
        if self.reject_background:
            raise OSError("cannot launch")
        self.background_requests.append(
            BackgroundRequest(
                command,
                {
                    "cwd": cwd,
                    "env": env,
                    "notify_on_death": notify_on_death,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
        )

    def finish_background(
        self,
        exit_status: int,
        error: Exception | None = None,
    ) -> None:
        callback = self.background_requests[-1].keyword_arguments["notify_on_death"]
        assert callable(callback)
        callback(exit_status, error)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class BrokenMapping:
    def get(self, key: int) -> FakeWindow | None:
        del key
        raise RuntimeError("mapping unavailable")


class FlakyTab(FakeTab):
    def __init__(self, tab_id: int, os_window_id: int, windows: list[FakeWindow]) -> None:
        super().__init__(tab_id, os_window_id, windows)
        self.iterations = 0

    def __iter__(self) -> Iterator[FakeWindow]:
        self.iterations += 1
        if self.iterations > 1:
            raise RuntimeError("tab changed")
        return super().__iter__()


class BrokenTab(FakeTab):
    def __iter__(self) -> Iterator[FakeWindow]:
        raise RuntimeError("tab unavailable")


def owned_window(
    window_id: int = 11,
    session_id: str = "session-a",
    slug: str = "research",
) -> FakeWindow:
    return FakeWindow(
        window_id,
        {
            SESSION_ID_VAR: session_id,
            SESSION_SLUG_VAR: slug,
        },
        FakeChild(
            {"KITTY_LISTEN_ON": "unix:/tmp/kitty.sock", "BASE": "child"},
            {"FOREGROUND": "yes"},
        ),
    )


def route_close(target_window_id: int, boss: FakeBoss) -> None:
    request_tab_close(target_window_id, cast(CloseGuardBoss, boss))


class CloseGuardTests(unittest.TestCase):
    def tearDown(self) -> None:
        for session_id in tuple(_pending_sessions):
            _release_session(session_id)

    def test_mapping_and_environment_boundaries_normalize_unstable_kitty_values(self) -> None:
        def broken_provider() -> object:
            raise RuntimeError("gone")

        self.assertEqual(_string_mapping({"A": 1, "EMPTY": None}), {"A": "1"})
        self.assertEqual(_string_mapping(lambda: {"B": 2}), {"B": "2"})
        self.assertEqual(_string_mapping(broken_provider), {})
        self.assertEqual(_string_mapping("not a mapping"), {})

        without_child = FakeWindow(1)
        with mock.patch.dict("kisesh.close_guard.os.environ", {"BASE": "kitty"}, clear=True):
            self.assertEqual(
                _window_environment(cast(CloseGuardWindow, without_child)),
                {"BASE": "kitty"},
            )
            environment = _window_environment(
                cast(
                    CloseGuardWindow,
                    FakeWindow(
                        2,
                        child=FakeChild(
                            {"BASE": "child", "CHILD": "yes"},
                            lambda: {"BASE": "foreground"},
                        ),
                    ),
                ),
            )
        self.assertEqual(
            environment,
            {"BASE": "foreground", "CHILD": "yes"},
        )

    def test_tab_ownership_rejects_conflicts_and_uses_stable_labels(self) -> None:
        consistent = FakeTab(
            7,
            1,
            [owned_window(), FakeWindow(12, {SESSION_ID_VAR: "session-a"})],
        )
        conflicting = FakeTab(
            8,
            1,
            [owned_window(), owned_window(13, "session-b", "other")],
        )
        ambiguous_label = FakeTab(
            9,
            1,
            [owned_window(), owned_window(14, "session-a", "renamed")],
        )

        self.assertEqual(
            _tab_ownership(cast(CloseGuardTab, consistent)),
            TabOwnership("session-a", "research", True),
        )
        self.assertEqual(
            _tab_ownership(cast(CloseGuardTab, conflicting)),
            TabOwnership(None, None, False),
        )
        self.assertEqual(
            _tab_ownership(cast(CloseGuardTab, ambiguous_label)),
            TabOwnership("session-a", "session-a", True),
        )

    def test_unowned_and_multitab_requests_close_the_exact_active_tab_immediately(self) -> None:
        unowned_tab = FakeTab(7, 1, [FakeWindow(11)])
        unowned = FakeBoss(unowned_tab)

        route_close(11, unowned)

        self.assertEqual(unowned.closed_tabs, [7])
        self.assertEqual(unowned.confirmations, [])

        active = FakeTab(8, 1, [owned_window()])
        sibling = FakeTab(9, 1, [owned_window(12)])
        tracked = FakeBoss(active, [active, sibling])

        route_close(11, tracked)

        self.assertEqual(tracked.closed_tabs, [8])
        self.assertEqual(tracked.confirmations, [])

        for boss in (unowned, tracked):
            boss.reject_close_tab = True
            route_close(11, boss)

    def test_final_tab_uses_a_native_default_no_confirmation_and_blocks_repeats(self) -> None:
        tab = FakeTab(7, 41, [owned_window()])
        boss = FakeBoss(tab)

        route_close(11, boss)
        route_close(11, boss)

        self.assertEqual(len(boss.confirmations), 1)
        confirmation = boss.confirmations[0]
        self.assertEqual(
            confirmation.message,
            'Save and close the final tab of "research"?',
        )
        self.assertEqual(confirmation.keyword_arguments["window"], tab.windows[0])
        self.assertFalse(confirmation.keyword_arguments["confirm_on_cancel"])
        self.assertFalse(confirmation.keyword_arguments["confirm_on_accept"])
        self.assertEqual(confirmation.keyword_arguments["title"], "Close KiSesh session")
        self.assertEqual(boss.prompt.assigned_vars, [(KISESH_UI_VAR, "yes")])
        self.assertEqual(boss.closed_tabs, [])

        boss.answer(False)
        route_close(11, boss)
        self.assertEqual(len(boss.confirmations), 2)

    def test_confirmation_overlay_and_kisesh_ui_close_without_touching_a_tab(self) -> None:
        for transient in (
            FakeWindow(11, overlay_parent=7),
            FakeWindow(11, {KISESH_UI_VAR: "yes"}),
        ):
            tab = FakeTab(7, 1, [transient])
            boss = FakeBoss(tab)
            route_close(11, boss)
            self.assertEqual(boss.closed_windows, 1)
            self.assertEqual(boss.closed_tabs, [])

            boss.reject_close_window = True
            route_close(11, boss)

    def test_confirmed_close_uses_kitty_lifecycle_and_releases_only_after_exit(self) -> None:
        tab = FakeTab(7, 41, [owned_window()])
        boss = FakeBoss(tab)

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            launcher = runtime / "bin" / "kisesh"
            launcher.parent.mkdir()
            launcher.touch()
            with mock.patch("kisesh.close_guard.runtime_root", return_value=runtime):
                route_close(11, boss)
                boss.answer(True)
                route_close(11, boss)
                self.assertEqual(len(boss.confirmations), 1)
                self.assertEqual(len(boss.background_requests), 1)
                boss.finish_background(0)
                route_close(11, boss)

            background = boss.background_requests[0]
            self.assertEqual(
                background.command,
                [
                    str(launcher),
                    "--socket",
                    "unix:/tmp/kitty.sock",
                    "close",
                    "session-a",
                    "--promote-os-window",
                    "41",
                ],
            )
            self.assertEqual(background.keyword_arguments["cwd"], str(runtime))
        environment = cast(dict[str, str], background.keyword_arguments["env"])
        self.assertEqual(environment["KITTY_LISTEN_ON"], "unix:/tmp/kitty.sock")
        self.assertEqual(len(boss.confirmations), 2)
        self.assertEqual(boss.errors, [])

    def test_close_command_is_shell_free_and_prefers_the_explicit_socket(self) -> None:
        environment = {
            "KISESH_TARGET_SOCKET": "unix:/tmp/preferred.sock",
            "KITTY_LISTEN_ON": "unix:/tmp/fallback.sock",
        }
        request = CloseRequest("session-a", 41, environment)

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            launcher = runtime / "bin" / "kisesh"
            launcher.parent.mkdir()
            launcher.touch()
            with mock.patch("kisesh.close_guard.runtime_root", return_value=runtime):
                command = _close_command(request)
                without_socket = _close_command(CloseRequest("session-b", 9, {}))

            self.assertEqual(
                command,
                [
                    str(launcher),
                    "--socket",
                    "unix:/tmp/preferred.sock",
                    "close",
                    "session-a",
                    "--promote-os-window",
                    "41",
                ],
            )
            self.assertEqual(
                without_socket,
                [str(launcher), "close", "session-b", "--promote-os-window", "9"],
            )

    def test_cancel_launch_and_child_failures_release_guard_and_report_errors(self) -> None:
        tab = FakeTab(7, 41, [owned_window()])
        request = CloseRequest("session-a", 41, {})
        boss = FakeBoss(tab)

        self.assertTrue(_reserve_session(request.session_id))
        _confirmed_close(False, request, cast(CloseGuardBoss, boss))
        self.assertNotIn(request.session_id, _pending_sessions)

        with mock.patch(
            "kisesh.close_guard.runtime_root",
            return_value=Path("/definitely/missing/kisesh"),
        ):
            route_close(11, boss)
            boss.answer(True)
        self.assertEqual(
            boss.errors[-1],
            ("KiSesh close failed", "The installed kisesh launcher is unavailable."),
        )
        self.assertNotIn(request.session_id, _pending_sessions)

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            launcher = runtime / "bin" / "kisesh"
            launcher.parent.mkdir()
            launcher.touch()
            boss.reject_background = True
            with mock.patch("kisesh.close_guard.runtime_root", return_value=runtime):
                route_close(11, boss)
                boss.answer(True)
        self.assertIn("cannot launch", boss.errors[-1][1])
        self.assertNotIn(request.session_id, _pending_sessions)

        self.assertTrue(_reserve_session(request.session_id))
        _close_finished(7, None, request, cast(CloseGuardBoss, boss))
        self.assertIn("status 7", boss.errors[-1][1])
        self.assertNotIn(request.session_id, _pending_sessions)

        self.assertTrue(_reserve_session(request.session_id))
        _close_finished(1, OSError("lost child"), request, cast(CloseGuardBoss, boss))
        self.assertIn("lost child", boss.errors[-1][1])
        self.assertNotIn(request.session_id, _pending_sessions)

    def test_confirmation_and_prompt_marker_failures_do_not_close_tabs(self) -> None:
        for reject_confirmation, reject_marker in ((True, False), (False, True)):
            tab = FakeTab(7, 1, [owned_window()])
            boss = FakeBoss(tab)
            boss.reject_confirmation = reject_confirmation
            boss.prompt.reject_user_var = reject_marker
            route_close(11, boss)
            self.assertEqual(boss.closed_tabs, [])
            if not reject_confirmation:
                boss.answer(False)

    def test_stale_ambiguous_and_unavailable_state_never_crosses_a_session_boundary(self) -> None:
        active = FakeTab(7, 1, [owned_window()])
        cases: list[FakeBoss] = []

        missing_active = FakeBoss(None)
        cases.append(missing_active)

        missing_target = FakeBoss(active)
        missing_target.window_id_map = {}
        cases.append(missing_target)

        mismatched_target = FakeBoss(active)
        mismatched_target.active_window = FakeWindow(12)
        cases.append(mismatched_target)

        broken_map = FakeBoss(active)
        broken_map.window_id_map = cast(Mapping[int, FakeWindow], BrokenMapping())
        cases.append(broken_map)

        no_tab = FakeBoss(active)
        no_tab.active_tab = None
        cases.append(no_tab)

        wrong_tab = FakeBoss(active)
        wrong_tab.active_tab = FakeTab(8, 1, [owned_window(12)])
        cases.append(wrong_tab)

        flaky = FakeBoss(FlakyTab(7, 1, [owned_window()]))
        cases.append(flaky)

        broken_tab = FakeBoss(BrokenTab(7, 1, [owned_window()]))
        cases.append(broken_tab)

        ambiguous_tab = FakeTab(
            7,
            1,
            [owned_window(), owned_window(12, "session-b", "other")],
        )
        cases.append(FakeBoss(ambiguous_tab))

        unavailable = FakeBoss(active)
        unavailable.reject_match = True
        cases.append(unavailable)

        absent_from_state = FakeBoss(active, [])
        cases.append(absent_from_state)

        inconsistent_sibling = FakeTab(
            8,
            1,
            [owned_window(12), owned_window(13, "session-b", "other")],
        )
        cases.append(FakeBoss(active, [active, inconsistent_sibling]))

        for boss in cases:
            with self.subTest(case=len(cases)):
                route_close(11, boss)
                self.assertEqual(boss.closed_tabs, [])
                self.assertEqual(boss.closed_windows, 0)
                self.assertEqual(boss.confirmations, [])

    def test_transient_property_and_active_tab_access_errors_fail_closed(self) -> None:
        class BrokenOverlayWindow(FakeWindow):
            def __getattribute__(self, name: str) -> object:
                if name == "overlay_parent":
                    raise RuntimeError("overlay state unavailable")
                return super().__getattribute__(name)

        broken = BrokenOverlayWindow(11)
        boss = FakeBoss(FakeTab(7, 1, [broken]))
        route_close(11, boss)
        self.assertEqual(boss.closed_tabs, [])


if __name__ == "__main__":
    unittest.main()
