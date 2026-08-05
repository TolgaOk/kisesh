"""Regression tests for event-driven Kitty watcher persistence."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

from kitty_workbench import watcher


class Child(watcher.WatcherChild):
    """Expose the process state read by watcher callbacks."""

    environ: object = {"PATH": "/initial", "KITTY_LISTEN_ON": "unix:/tmp/kitty"}
    foreground_environ: object = {"PATH": "/fresh"}
    foreground_cwd: object = "/tmp/project"


class Window(watcher.WatcherWindow):
    """Provide a controllable Kitty window for watcher scenarios."""

    def __init__(
        self,
        window_id: int = 2,
        *,
        session_id: str | None = "session-id",
        history: str = "",
        output: str = "",
        alternate_screen: bool = False,
    ) -> None:
        """Initialize identity, terminal text, and close-time metadata."""
        self.id = window_id
        self.child: watcher.WatcherChild | None = Child()
        self.user_vars: object = (
            {watcher.SESSION_ID_VAR: session_id} if session_id is not None else {}
        )
        self.history = history
        self.output = output
        self.alternate_screen = alternate_screen
        self.history_requests: list[bool] = []

    def as_dict(self) -> Mapping[str, object]:
        """Return the pane state available immediately before destruction."""
        return {
            "id": self.id,
            "title": "Shell",
            "cwd": "/tmp/project",
            "user_vars": self.user_vars,
            "foreground_processes": [{"cmdline": ["top"]}],
            "at_prompt": False,
            "in_alternate_screen": self.alternate_screen,
        }

    def as_text(
        self,
        as_ansi: bool = False,
        add_history: bool = False,
        add_wrap_markers: bool = False,
        alternate_screen: bool = False,
        add_cursor: bool = False,
    ) -> str:
        """Return terminal history while recording the requested capture mode."""
        del as_ansi, add_wrap_markers, alternate_screen, add_cursor
        self.history_requests.append(add_history)
        return self.history

    def cmd_output(self) -> str:
        """Return the most recent completed command output."""
        return self.output


class Boss(watcher.WatcherBoss):
    """Return configured tab membership for watcher identity and location queries."""

    def __init__(self, tabs: Iterable[Iterable[watcher.WatcherWindow]] = ()) -> None:
        """Copy tabs so repeated Kitty-style matching stays deterministic."""
        self.tabs = [list(tab) for tab in tabs]
        self.expressions: list[str] = []

    def match_tabs(self, expression: str) -> Iterable[Iterable[watcher.WatcherWindow]]:
        """Match all tabs or the tab containing one requested window."""
        self.expressions.append(expression)
        if expression == "all":
            return self.tabs
        prefix = "window_id:"
        if expression.startswith(prefix):
            window_id = int(expression.removeprefix(prefix))
            return [tab for tab in self.tabs if any(window.id == window_id for window in tab)]
        return []


class BrokenWindow(Window):
    """Represent pane metadata disappearing during Kitty shutdown."""

    def as_dict(self) -> Mapping[str, object]:
        """Raise as a destroyed Kitty window may do."""
        raise RuntimeError("window closing")


class BrokenBoss(watcher.WatcherBoss):
    """Represent Kitty state lookup failing during shutdown."""

    def match_tabs(self, expression: str) -> Iterable[Iterable[watcher.WatcherWindow]]:
        """Raise as Kitty may do once its OS window is disappearing."""
        raise RuntimeError(f"state unavailable for {expression}")


class FakeTimer:
    """Record debounce timer lifecycle without sleeping in tests."""

    instances: ClassVar[list[FakeTimer]] = []

    def __init__(
        self,
        delay: float,
        callback: Callable[..., None],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> None:
        """Store the callback and arguments for deterministic triggering."""
        self.delay = delay
        self.callback = callback
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False
        self.daemon = False
        self.instances.append(self)

    def cancel(self) -> None:
        """Record cancellation of an obsolete debounce timer."""
        self.cancelled = True

    def start(self) -> None:
        """Record activation without creating a background thread."""
        self.started = True

    def fire(self) -> None:
        """Run the timer callback with its captured invocation arguments."""
        self.callback(*self.args, **self.kwargs)


def _written_payload(popen: mock.MagicMock) -> dict[str, object]:
    """Decode the JSON payload written to an autosave subprocess."""
    encoded = cast(str, popen.return_value.stdin.write.call_args.args[0])
    return cast(dict[str, object], json.loads(encoded))


class WatcherTests(unittest.TestCase):
    """Cover ownership, debounce, and pane-close capture behavior."""

    def setUp(self) -> None:
        """Reset process-global watcher queues before each scenario."""
        watcher._timers.clear()
        watcher._pending_commands.clear()
        FakeTimer.instances.clear()

    def tearDown(self) -> None:
        """Prevent watcher state from leaking into later scenarios."""
        watcher._timers.clear()
        watcher._pending_commands.clear()

    def test_environment_prefers_foreground_path_and_keeps_socket(self) -> None:
        """Use the foreground shell PATH while retaining Kitty's socket."""
        environment = watcher._window_environment(Window())
        self.assertEqual(environment["PATH"], "/fresh")
        self.assertEqual(environment["KITTY_LISTEN_ON"], "unix:/tmp/kitty")

    def test_new_pane_inherits_autosave_identity_from_stamped_sibling(self) -> None:
        """Associate an unstamped split with a stamped sibling immediately."""
        stamped = Window()
        new_pane = Window(3, session_id=None)
        self.assertEqual(
            watcher._session_id(new_pane, boss=Boss([[stamped, new_pane]])), "session-id"
        )

    def test_transient_ui_never_inherits_a_sibling_session_identity(self) -> None:
        """Exclude the overlay itself before any sibling ownership lookup."""
        workbench_ui = Window(55, session_id=None)
        workbench_ui.user_vars = {watcher.WORKBENCH_UI_VAR: "yes"}
        boss = Boss([[workbench_ui, Window()]])

        self.assertIsNone(watcher._session_id(workbench_ui, boss=boss))
        self.assertEqual(boss.expressions, [])

    def test_events_debounce_per_session(self) -> None:
        """Replace a pending timer instead of polling or writing twice."""
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher._schedule(Window())
            watcher._schedule(Window())

        self.assertEqual(len(FakeTimer.instances), 2)
        self.assertTrue(FakeTimer.instances[0].cancelled)
        self.assertTrue(FakeTimer.instances[1].started)
        self.assertIs(watcher._timers["session-id"], FakeTimer.instances[1])

    def test_command_stop_schedules_but_command_start_does_not(self) -> None:
        """Queue only completed commands and include their foreground cwd."""
        boss = Boss()
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_cmd_startstop(boss, Window(), {"is_start": True})
            schedule.assert_not_called()
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "pytest -q", "time": 1785843000.0},
            )
            schedule.assert_called_once()
            event = schedule.call_args.args[3]
            self.assertEqual(event["window_id"], 2)
            self.assertEqual(event["command"], "pytest -q")
            self.assertEqual(event["cwd"], "/tmp/project")

    def test_autosave_uses_shared_launcher_and_receives_socket_before_subcommand(self) -> None:
        """Invoke the installed launcher with global options in Tyro order."""
        environment = {"PATH": "/fresh", "KITTY_LISTEN_ON": "unix:/tmp/kitty"}
        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            watcher._run_autosave("session-id", environment)

        command = popen.call_args.args[0]
        self.assertTrue(command[0].endswith("/bin/kitty-workbench"))
        self.assertEqual(command[1:4], ["--socket", "unix:/tmp/kitty", "autosave"])
        self.assertEqual(command[4:], ["session-id", "--payload-stdin"])
        self.assertEqual(_written_payload(popen), {"command_events": []})

    def test_rapid_completed_commands_survive_debounce_and_reach_autosave(self) -> None:
        """Retain every completion when rapid events replace the timer."""
        boss = Boss()
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "pytest -q", "time": 1785843000.0},
            )
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "git status", "time": 1785843001.0},
            )

        self.assertTrue(FakeTimer.instances[0].cancelled)
        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            FakeTimer.instances[-1].fire()

        payload = _written_payload(popen)
        events = cast(list[dict[str, object]], payload["command_events"])
        self.assertEqual([event["command"] for event in events], ["pytest -q", "git status"])
        self.assertNotIn("session-id", watcher._pending_commands)

    def test_cmd_w_captures_pending_history_before_kitty_destroys_the_screen(self) -> None:
        """Persist reopened-pane changes immediately when Cmd-W closes the pane."""
        old_lines = "".join(f"old-{index:04d}\n" for index in range(1998))
        closing = Window(
            99,
            history=f"{old_lines}pwd\n/tmp/project\ntop\n",
            output="/tmp/project\n",
            alternate_screen=True,
        )
        foreign = Window(41, session_id="other-session")
        sibling = Window(98)
        boss = Boss([[foreign], [sibling, closing]])

        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_cmd_startstop(
                boss,
                closing,
                {"is_start": False, "cmdline": ["pwd"], "time": 1785843002.0},
            )
        pending_timer = FakeTimer.instances[-1]

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            watcher.on_close(boss, closing, {})

        self.assertTrue(pending_timer.cancelled)
        self.assertNotIn("session-id", watcher._timers)
        self.assertNotIn("session-id", watcher._pending_commands)
        self.assertEqual(closing.history_requests, [True])
        payload = _written_payload(popen)
        capture = cast(dict[str, object], payload["closing_pane"])
        self.assertEqual((capture["tab_index"], capture["pane_index"]), (0, 1))
        self.assertEqual(capture["terminal_history"], closing.history)
        self.assertEqual(capture["last_command_output"], "/tmp/project\n")
        events = cast(list[dict[str, object]], capture["command_events"])
        self.assertEqual([event["command"] for event in events], [["pwd"]])
        self.assertEqual(cast(dict[str, object], capture["window"])["id"], 99)

    def test_triggered_autosave_timer_is_one_shot_not_a_polling_loop(self) -> None:
        """Complete one scheduled save without creating another timer."""
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_title_change(Boss(), Window(), {"title": "settled"})
            self.assertEqual(len(FakeTimer.instances), 1)
            with mock.patch("kitty_workbench.watcher.subprocess.Popen"):
                FakeTimer.instances[0].fire()

        self.assertEqual(len(FakeTimer.instances), 1)
        self.assertNotIn("session-id", watcher._timers)

    def test_focus_changes_alone_never_trigger_disk_autosave(self) -> None:
        """Avoid writes for focus churn that has no durable session change."""
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_focus_change(Boss(), Window(), {"focused": True})
            watcher.on_focus_change(Boss(), Window(), {"focused": False})
            schedule.assert_not_called()

    def test_mapping_providers_and_missing_children_degrade_to_safe_empty_values(self) -> None:
        self.assertEqual(watcher._string_mapping(lambda: {"A": 1, "B": None}), {"A": "1"})

        def broken_provider() -> object:
            raise RuntimeError("provider disappeared")

        self.assertEqual(watcher._string_mapping(broken_provider), {})
        self.assertEqual(watcher._string_mapping("not-a-mapping"), {})
        window = Window()
        window.child = None
        with mock.patch.dict("os.environ", {"BASE": "kept"}, clear=True):
            self.assertEqual(watcher._window_environment(window), {"BASE": "kept"})

    def test_session_identity_prefers_assignment_and_tolerates_failed_sibling_lookup(self) -> None:
        unstamped = Window(session_id=None)
        self.assertEqual(
            watcher._session_id(
                unstamped,
                {"key": watcher.SESSION_ID_VAR, "value": "new-session"},
                BrokenBoss(),
            ),
            "new-session",
        )
        self.assertIsNone(watcher._session_id(unstamped, boss=None))
        self.assertIsNone(watcher._session_id(unstamped, boss=BrokenBoss()))

    def test_autosave_launch_rejects_unserializable_or_unavailable_process_boundaries(self) -> None:
        watcher._launch_autosave("session-id", {}, {"bad": {1, 2, 3}})

        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen,
        ):
            watcher._launch_autosave("session-id", {}, {"command_events": []})
        popen.assert_not_called()

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.stdin = None
            watcher._launch_autosave("session-id", {}, {"command_events": []})
        command = popen.call_args.args[0]
        self.assertNotIn("--socket", command)

        for error in (OSError("spawn failed"), BrokenPipeError("closed")):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch("kitty_workbench.watcher.subprocess.Popen", side_effect=error),
            ):
                watcher._launch_autosave("session-id", {}, {"command_events": []})

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.stdin.write.side_effect = BrokenPipeError("closed")
            watcher._launch_autosave("session-id", {}, {"command_events": []})

    def test_unstable_text_and_location_apis_never_break_close(self) -> None:
        self.assertEqual(watcher._read_window_text(lambda: 7), "")

        def broken_reader() -> object:
            raise RuntimeError("screen destroyed")

        self.assertEqual(watcher._read_window_text(broken_reader), "")
        window = Window()
        self.assertEqual(watcher._session_location(window, None, "session-id"), (-1, -1))
        self.assertEqual(
            watcher._session_location(window, BrokenBoss(), "session-id"),
            (-1, -1),
        )
        self.assertEqual(
            watcher._session_location(Window(404), Boss([[window]]), "session-id"),
            (-1, -1),
        )

        broken_window = BrokenWindow()
        capture = watcher._closing_pane_capture(broken_window, None, "session-id", [])
        self.assertEqual(
            cast(dict[str, object], capture["window"]),
            {"id": broken_window.id},
        )

    def test_drain_without_timer_and_unsaved_windows_are_noops(self) -> None:
        watcher._pending_commands["session-id"] = [{"command": "pwd"}]
        self.assertEqual(watcher._drain_closing_events("session-id"), [{"command": "pwd"}])
        self.assertEqual(watcher._drain_closing_events("missing"), [])

        unstamped = Window(session_id=None)
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher._schedule(unstamped)
        self.assertEqual(FakeTimer.instances, [])
        with mock.patch.object(watcher, "_launch_autosave") as launch:
            watcher.on_close(Boss(), unstamped, {})
        launch.assert_not_called()

    def test_all_material_callbacks_schedule_but_unrelated_variables_do_not(self) -> None:
        boss = Boss()
        window = Window()
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_resize(boss, window, {"size": "changed"})
            watcher.on_title_change(boss, window, {"title": "changed"})
            watcher.on_tab_bar_dirty(boss, window, {"tabs": "changed"})
            watcher.on_set_user_var(
                boss,
                window,
                {"key": watcher.SESSION_ID_VAR, "value": "session-id"},
            )
            watcher.on_set_user_var(boss, window, {"key": "unrelated", "value": "value"})
        self.assertEqual(schedule.call_count, 4)

    def test_command_completion_accepts_argv_and_optional_callable_cwd(self) -> None:
        boss = Boss()
        window = Window()
        assert window.child is not None
        window.child.foreground_cwd = lambda: "/tmp/callable"
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_cmd_startstop(
                boss,
                window,
                {"is_start": False, "cmdline": ("git", "status"), "time": 1},
            )
        event = schedule.call_args.args[3]
        self.assertEqual(event["command"], ["git", "status"])
        self.assertEqual(event["cwd"], "/tmp/callable")

        window.child.foreground_cwd = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_cmd_startstop(boss, window, {"is_start": False, "cmdline": None})
        self.assertNotIn("cwd", schedule.call_args.args[3])

        window.child = None
        self.assertIsNone(watcher._foreground_cwd(window))


if __name__ == "__main__":
    unittest.main()
