"""Confirmed-quit persistence and cancellation through native watcher callbacks."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock

from kisesh import watcher
from tests.test_watcher import Boss, FakeTimer, LayoutTab, Window


@dataclass(frozen=True)
class SaveJob:
    command: list[str]
    payload: dict[str, object]
    callback: Callable[[int, Exception | None], None]
    stderr: int


class QuitBoss(Boss):
    def __init__(self, windows: list[Window]) -> None:
        super().__init__([[window] for window in windows])
        self.window = windows[0]
        self.jobs: list[SaveJob] = []
        self.errors: list[str] = []
        self.continued = 0
        self.veto = False
        self.launch_error: Exception | None = None
        self.native = ModuleType("kitty.fast_data_types")
        self.native.__dict__.update(
            IMPERATIVE_CLOSE_REQUESTED=2,
            current_application_quit_request=lambda: 0 if self.veto else 2,
        )

    def run_background_process(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin: bytes,
        notify_on_death: Callable[[int, Exception | None], None],
        stdout: int,
        stderr: int,
    ) -> None:
        del cwd, env, stdout
        if self.launch_error is not None:
            raise self.launch_error
        self.jobs.append(SaveJob(cmd, json.loads(stdin), notify_on_death, stderr))

    def handle_quit_confirmation(self, confirmed: bool) -> None:
        self.continued += 1
        event: dict[str, object] = {"confirmed": confirmed}
        watcher.on_quit(self, self.window, event)
        if event.get("aborted"):
            raise AssertionError("quit continuation started another save")

    def show_error(self, title: str, message: str) -> None:
        self.errors.append(f"{title}: {message}")


class QuitWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        launcher = self.runtime / "bin" / "kisesh"
        launcher.parent.mkdir()
        launcher.touch()
        self.enterContext(mock.patch.dict(os.environ, {"KISESH_INSTALL_ROOT": str(self.runtime)}))
        self.enterContext(mock.patch.object(watcher, "_quit_state", watcher.QuitSaveState()))
        self.enterContext(mock.patch.object(watcher, "_timers", {}))
        self.enterContext(mock.patch.object(watcher, "_timer_generations", {}))
        self.enterContext(mock.patch.object(watcher, "_pending_commands", {}))
        self.enterContext(mock.patch.object(threading, "Timer", FakeTimer))
        FakeTimer.instances.clear()

    def boss(self, *windows: Window) -> QuitBoss:
        boss = QuitBoss(list(windows))
        self.enterContext(mock.patch.dict("sys.modules", {"kitty.fast_data_types": boss.native}))
        return boss

    def test_confirmed_quit_saves_every_session_once_before_teardown(self) -> None:
        first = Window(1, session_id="first")
        second = Window(2, session_id="second")
        sibling = Window(3, session_id="first")
        manager = Window(4, session_id="first")
        manager.user_vars = {watcher.KISESH_UI_VAR: "yes", watcher.SESSION_ID_VAR: "first"}
        boss = self.boss(first, second, sibling, manager, Window(5, session_id=None))
        watcher.on_cmd_startstop(
            boss, first, {"is_start": False, "cmdline": "pwd", "time": 1785843002.0}
        )
        pending = FakeTimer.instances[-1]
        event: dict[str, object] = {"confirmed": True}

        watcher.on_quit(boss, first, event)

        self.assertTrue(event["aborted"])
        self.assertTrue(pending.cancelled)
        self.assertEqual(boss.continued, 0)
        self.assertEqual(boss.jobs, [])
        watcher.on_close(boss, manager, {})
        self.assertEqual(boss.jobs[0].command[-2:], ["first", "--payload-stdin"])
        self.assertEqual(boss.jobs[0].payload["command_events"], watcher._pending_commands["first"])
        with mock.patch.object(watcher, "_launch_autosave") as launch:
            pending.fire()
        launch.assert_not_called()
        boss.jobs[0].callback(0, None)
        self.assertEqual(boss.continued, 0)
        self.assertEqual(boss.jobs[1].command[-2:], ["second", "--payload-stdin"])
        boss.jobs[1].callback(0, None)
        self.assertEqual(boss.continued, 1)
        self.assertEqual(watcher._pending_commands, {})
        with mock.patch.object(watcher, "_launch_autosave") as launch:
            watcher.on_close(boss, first, {})
            watcher.on_title_change(boss, second, {"title": "closing"})
        launch.assert_not_called()
        self.assertEqual(watcher._timers, {})
        self.assertEqual(boss.errors, [])

    def test_duplicate_quit_requests_do_not_launch_extra_savers_or_prompts(self) -> None:
        window = Window()
        boss = self.boss(window)
        watcher.on_quit(boss, window, {"confirmed": True})
        for confirmed in (False, True):
            event: dict[str, object] = {"confirmed": confirmed}
            watcher.on_quit(boss, window, event)
            self.assertTrue(event["aborted"])
        self.assertEqual(len(boss.jobs), 1)
        boss.jobs[0].callback(0, None)
        self.assertEqual(boss.continued, 1)

    def test_command_arriving_during_capture_is_saved_before_quit_continues(self) -> None:
        window = Window()
        boss = self.boss(window)
        watcher.on_quit(boss, window, {"confirmed": True})
        watcher.on_cmd_startstop(
            boss, window, {"is_start": False, "cmdline": "ls", "time": 1785843002.0}
        )
        boss.jobs[0].callback(0, None)
        self.assertEqual(boss.continued, 0)
        self.assertEqual(len(boss.jobs), 2)
        self.assertEqual(
            boss.jobs[1].payload["command_events"], watcher._pending_commands["session-id"]
        )
        boss.jobs[1].callback(0, None)
        self.assertEqual(boss.continued, 1)
        self.assertEqual(watcher._pending_commands, {})
        self.assertEqual(FakeTimer.instances, [])

    def test_save_failure_preserves_events_and_allows_a_new_quit_attempt(self) -> None:
        window = Window()
        boss = self.boss(window)
        watcher.on_cmd_startstop(
            boss, window, {"is_start": False, "cmdline": "pwd", "time": 1785843002.0}
        )
        watcher.on_quit(boss, window, {"confirmed": True})
        os.write(boss.jobs[0].stderr, b"No space left on device")
        boss.jobs[0].callback(1, None)
        self.assertEqual(boss.continued, 0)
        self.assertIn("No space left", boss.errors[-1])
        self.assertEqual(len(watcher._pending_commands["session-id"]), 1)
        watcher.on_quit(boss, window, {"confirmed": True})
        boss.jobs[1].callback(0, None)
        self.assertEqual(boss.continued, 1)

    def test_unconfirmed_or_unowned_quit_uses_native_behavior(self) -> None:
        window = Window(session_id=None)
        boss = self.boss(window)
        for confirmed in (False, True):
            event: dict[str, object] = {"confirmed": confirmed}
            watcher.on_quit(boss, window, event)
            self.assertNotIn("aborted", event)
        self.assertEqual(boss.jobs, [])
        self.assertEqual(boss.errors, [])

    def test_another_quit_watchers_veto_releases_autosave_suppression(self) -> None:
        window = Window()
        boss = self.boss(window)
        boss.veto = True
        watcher.on_quit(boss, window, {"confirmed": True})
        boss.jobs[0].callback(0, None)
        watcher.on_title_change(boss, window, {"title": "working again"})
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertEqual(len(FakeTimer.instances), 1)

    def test_launch_and_state_failures_cancel_quit(self) -> None:
        window = Window()
        boss = self.boss(window)
        for failure in (OSError("launcher failed"), None):
            with self.subTest(failure=failure):
                boss.launch_error = failure
                if failure is None:
                    (self.runtime / "bin" / "kisesh").unlink()
                event: dict[str, object] = {"confirmed": True}
                watcher.on_quit(boss, window, event)
                self.assertTrue(event["aborted"])
                self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
                self.assertEqual(boss.continued, 0)
        with mock.patch.object(boss, "match_tabs", side_effect=RuntimeError("state unavailable")):
            watcher.on_quit(boss, window, {"confirmed": True})
        self.assertIn("state unavailable", boss.errors[-1])

    def test_all_managers_close_and_restore_before_the_first_capture(self) -> None:
        window = Window(1)
        first = Window(2)
        second = Window(3)
        for manager in (first, second):
            manager.user_vars = {
                watcher.KISESH_UI_VAR: "yes",
                watcher.RESTORE_LAYOUT_VAR: "splits",
            }
        boss = self.boss(window, first, second)
        tab = LayoutTab([window, first, second])
        boss.tabs = [tab]
        watcher.on_quit(boss, window, {"confirmed": True})
        self.assertEqual(boss.jobs, [])
        self.assertEqual(boss.remote_calls[-1][1], ("close-window", "--match", "id:2 or id:3"))
        watcher.on_close(boss, first, {})
        self.assertEqual(boss.jobs, [])
        watcher.on_close(boss, second, {})
        self.assertEqual(tab.restored_layouts, ["splits", "splits"])
        self.assertEqual(len(boss.jobs), 1)
        boss.jobs[0].callback(0, None)
        self.assertEqual(boss.continued, 1)

    def test_manager_restore_failure_cancels_quit_before_persistence(self) -> None:
        window = Window()
        manager = Window(3)
        manager.user_vars = {
            watcher.KISESH_UI_VAR: "yes",
            watcher.RESTORE_LAYOUT_VAR: "splits",
        }
        boss = self.boss(window, manager)
        boss.remote_error = True
        watcher.on_quit(boss, window, {"confirmed": True})
        self.assertEqual(boss.jobs, [])
        self.assertEqual(watcher._quit_state.closing_managers, set())
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertIn("remote control unavailable", boss.errors[-1])

    def test_process_and_native_continuation_failures_leave_quit_retryable(self) -> None:
        window = Window()
        window.child = None
        boss = self.boss(window)
        for error in (None, OSError("child monitor failed")):
            with (
                self.subTest(error=error),
                mock.patch.dict(os.environ, {"KITTY_LISTEN_ON": "unix:/tmp/kitty"}, clear=True),
            ):
                with mock.patch.object(watcher, "_runtime_root", return_value=self.runtime):
                    watcher.on_quit(boss, window, {"confirmed": True})
                self.assertEqual(boss.jobs[-1].command[1:3], ["--socket", "unix:/tmp/kitty"])
                boss.jobs[-1].callback(9, error)
                self.assertIn(str(error) if error else "Save exited with 9", boss.errors[-1])
                self.assertEqual(boss.continued, 0)
        with mock.patch.dict(os.environ, {"KITTY_LISTEN_ON": "unix:/tmp/kitty"}):
            watcher.on_quit(boss, window, {"confirmed": True})
        with mock.patch.object(boss, "handle_quit_confirmation", side_effect=RuntimeError("veto")):
            boss.jobs[-1].callback(0, None)
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertIn("veto", boss.errors[-1])

    def test_quit_without_remote_socket_keeps_the_session_open(self) -> None:
        window = Window()
        window.child = None
        boss = self.boss(window)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(watcher, "_runtime_root", return_value=self.runtime),
        ):
            watcher.on_quit(boss, window, {"confirmed": True})
        self.assertEqual(boss.jobs, [])
        self.assertEqual(boss.continued, 0)
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertIn("remote-control socket is unavailable", boss.errors[-1])
