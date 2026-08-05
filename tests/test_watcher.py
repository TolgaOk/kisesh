"""Regression tests for event-driven Kitty watcher persistence."""

from __future__ import annotations

import json
import subprocess
import unittest
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

from kitty_workbench import watcher


class Child(watcher.WatcherChild):
    """Expose the process state read by watcher callbacks."""

    environ: object = {"PATH": "/initial", "KITTY_LISTEN_ON": "unix:/tmp/kitty"}
    foreground_environ: object = {"PATH": "/fresh"}
    foreground_cwd: object = "/tmp/project"


class LineBuffer(watcher.WatcherLineBuffer):
    """Expose the hidden main lines behind an alternate-screen application."""

    def __init__(self, history: str) -> None:
        """Store the fake main-line buffer."""
        self.text = history
        self.ansi_requests: list[bool] = []

    def as_text(
        self,
        callback: Callable[[str], object],
        as_ansi: bool,
        add_wrap_markers: bool,
    ) -> None:
        """Return the fake main-line buffer."""
        del add_wrap_markers
        self.ansi_requests.append(as_ansi)
        callback(self.text)


class Screen(watcher.WatcherScreen):
    """Expose main history separately from the visible alternate screen."""

    def __init__(self, history: str) -> None:
        """Store history in the main line buffer for this fake."""
        self.main_linebuf: watcher.WatcherLineBuffer = LineBuffer(history)
        self.ansi_requests: list[bool] = []

    def as_text_for_history_buf(
        self,
        callback: Callable[[str], object],
        as_ansi: bool,
        add_wrap_markers: bool,
    ) -> None:
        """Leave the fake scrollback portion empty."""
        del add_wrap_markers
        self.ansi_requests.append(as_ansi)
        del callback


@dataclass(slots=True, frozen=True)
class WindowMetadata:
    """Optional ownership metadata exposed by a fake Kitty pane."""

    session_slug: str | None = None
    session_scope: str | None = None
    native_session_name: str | None = None
    last_focused_at: float | None = None


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
        alternate_text: str = "",
        metadata: WindowMetadata | None = None,
    ) -> None:
        """Initialize identity, terminal text, and close-time metadata."""
        self.id = window_id
        self.child: watcher.WatcherChild | None = Child()
        self.user_vars: object = (
            {watcher.SESSION_ID_VAR: session_id} if session_id is not None else {}
        )
        variables = self.user_vars
        metadata = metadata or WindowMetadata()
        if metadata.session_slug is not None:
            variables[watcher.SESSION_SLUG_VAR] = metadata.session_slug
        if metadata.session_scope is not None:
            variables[watcher.SESSION_SCOPE_VAR] = metadata.session_scope
        self.native_session_name = metadata.native_session_name
        self.last_focused_at = metadata.last_focused_at
        self.history = history
        self.output = output
        self.alternate_screen = alternate_screen
        self.alternate_text = alternate_text
        self.screen: watcher.WatcherScreen = Screen(history)
        self.history_requests: list[tuple[bool, bool, bool]] = []

    def as_dict(self) -> Mapping[str, object]:
        """Return the pane state available immediately before destruction."""
        state: dict[str, object] = {
            "id": self.id,
            "title": "Shell",
            "cwd": "/tmp/project",
            "user_vars": self.user_vars,
            "env": {"TOKEN": "must-not-cross-the-watcher-boundary"},
            "foreground_processes": [{"cmdline": ["top"]}],
            "at_prompt": False,
            "in_alternate_screen": self.alternate_screen,
        }
        if self.native_session_name is not None:
            state["session_name"] = self.native_session_name
        if self.last_focused_at is not None:
            state["last_focused_at"] = self.last_focused_at
        return state

    def as_text(
        self,
        as_ansi: bool = False,
        add_history: bool = False,
        add_wrap_markers: bool = False,
        alternate_screen: bool = False,
        add_cursor: bool = False,
    ) -> str:
        """Return terminal history while recording the requested capture mode."""
        del add_wrap_markers, add_cursor
        self.history_requests.append((as_ansi, add_history, alternate_screen))
        return self.alternate_text if self.alternate_screen else self.history

    def cmd_output(self) -> str:
        """Return the most recent completed command output."""
        return self.output


class Boss(watcher.WatcherBoss):
    """Return configured tab membership for watcher identity and location queries."""

    def __init__(
        self,
        tabs: Iterable[Iterable[watcher.WatcherWindow]] = (),
        *,
        remote_error: bool = False,
    ) -> None:
        """Copy tabs so repeated Kitty-style matching stays deterministic."""
        self.tabs = [list(tab) for tab in tabs]
        self.expressions: list[str] = []
        self.remote_calls: list[tuple[int, tuple[str, ...]]] = []
        self.remote_error = remote_error

    def match_tabs(self, expression: str) -> Iterable[Iterable[watcher.WatcherWindow]]:
        """Match all tabs or the tab containing one requested window."""
        self.expressions.append(expression)
        if expression in {"all", "state:focused_os_window"}:
            return self.tabs
        prefix = "window_id:"
        if expression.startswith(prefix):
            window_id = int(expression.removeprefix(prefix))
            return [tab for tab in self.tabs if any(window.id == window_id for window in tab)]
        return []

    def call_remote_control(
        self,
        window: watcher.WatcherWindow,
        command: tuple[str, ...],
    ) -> object:
        """Record one in-process remote-control inheritance request."""
        if self.remote_error:
            raise RuntimeError("remote control unavailable")
        self.remote_calls.append((window.id, command))
        return None


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

    def call_remote_control(
        self,
        window: watcher.WatcherWindow,
        command: tuple[str, ...],
    ) -> object:
        """Raise when Kitty can no longer mutate window ownership."""
        del window, command
        raise RuntimeError("state unavailable")


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
        watcher._timer_generations.clear()
        watcher._pending_commands.clear()
        FakeTimer.instances.clear()

    def tearDown(self) -> None:
        """Prevent watcher state from leaking into later scenarios."""
        watcher._timers.clear()
        watcher._timer_generations.clear()
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

    def test_new_native_session_tab_is_stamped_before_its_autosave(self) -> None:
        """Persist a new tab as part of the session it inherited from Kitty."""
        owner = Window(
            metadata=WindowMetadata(
                session_slug="current-project",
                session_scope="1",
                native_session_name="/tmp/current.kitty-session",
                last_focused_at=10.0,
            )
        )
        new_tab = Window(
            3,
            session_id=None,
            metadata=WindowMetadata(
                native_session_name="/tmp/current.kitty-session",
                last_focused_at=20.0,
            ),
        )
        boss = Boss([[owner], [new_tab]])

        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_tab_bar_dirty(boss, new_tab, {"tabs": "changed"})

        self.assertEqual(
            boss.remote_calls,
            [
                (
                    new_tab.id,
                    (
                        "set-user-vars",
                        "--match",
                        "id:3",
                        f"{watcher.SESSION_ID_VAR}=session-id",
                        f"{watcher.SESSION_SLUG_VAR}=current-project",
                        f"{watcher.SESSION_SCOPE_VAR}=1",
                    ),
                )
            ],
        )
        schedule.assert_called_once_with(
            new_tab,
            {"key": watcher.SESSION_ID_VAR, "value": "session-id"},
            boss,
        )

    def test_unrelated_native_tab_remains_unowned(self) -> None:
        """Keep the explicit switch choice for a tab outside the active session."""
        owner = Window(
            metadata=WindowMetadata(
                session_slug="current-project",
                session_scope="1",
                native_session_name="/tmp/current.kitty-session",
            )
        )
        unrelated = Window(
            3,
            session_id=None,
            metadata=WindowMetadata(native_session_name="/tmp/other.kitty-session"),
        )
        boss = Boss([[owner], [unrelated]])
        event = {"tabs": "changed"}

        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_tab_bar_dirty(boss, unrelated, event)

        self.assertEqual(boss.remote_calls, [])
        schedule.assert_called_once_with(unrelated, event, boss)

    def test_new_split_receives_complete_identity_from_its_tab_sibling(self) -> None:
        """Stamp every new pane so closing the original cannot orphan its tab."""
        owner = Window(
            metadata=WindowMetadata(
                session_slug="current-project",
                session_scope="1",
                last_focused_at=10.0,
            )
        )
        new_pane = Window(3, session_id=None)
        boss = Boss([[owner, new_pane]])

        self.assertEqual(watcher._inherit_tab_ownership(boss, new_pane), "session-id")

        self.assertEqual(boss.remote_calls[0][1][2], "id:3")

    def test_inheritance_failures_leave_tab_ownership_unchanged(self) -> None:
        """Decline ambiguous, incomplete, unavailable, and transient UI inheritance."""
        incomplete = Window()
        new_pane = Window(3, session_id=None)
        self.assertIsNone(watcher._tab_inheritance(3, Boss([[incomplete, new_pane]])))

        already_owned = Window(4)
        self.assertIsNone(watcher._tab_inheritance(4, Boss([[already_owned]])))

        workbench_ui = Window(5, session_id=None)
        workbench_ui.user_vars = {watcher.WORKBENCH_UI_VAR: "yes"}
        self.assertIsNone(watcher._tab_inheritance(5, Boss([[workbench_ui]])))

        broken_window = BrokenWindow(6, session_id=None)
        identity = watcher._window_identity(broken_window)
        self.assertIsNone(identity.native_session_name)
        self.assertEqual(identity.last_focused_at, 0.0)
        self.assertIsNone(watcher._tab_inheritance(6, BrokenBoss()))

        owner = Window(
            metadata=WindowMetadata(
                session_slug="current-project",
                session_scope="1",
                native_session_name="/tmp/current.kitty-session",
            )
        )
        new_tab = Window(
            7,
            session_id=None,
            metadata=WindowMetadata(native_session_name="/tmp/current.kitty-session"),
        )
        self.assertIsNone(
            watcher._inherit_tab_ownership(
                Boss([[owner], [new_tab]], remote_error=True),
                new_tab,
            )
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
            self.assertEqual(event["completed_at"], 1785843000.0)

    def test_autosave_uses_shared_launcher_and_receives_socket_before_subcommand(self) -> None:
        """Invoke the installed launcher with global options in Tyro order."""
        environment = {"PATH": "/fresh", "KITTY_LISTEN_ON": "unix:/tmp/kitty"}
        watcher._timer_generations["session-id"] = 1
        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            watcher._run_autosave("session-id", environment, 1)

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
            popen.return_value.wait.return_value = 0
            FakeTimer.instances[-1].fire()

        payload = _written_payload(popen)
        events = cast(list[dict[str, object]], payload["command_events"])
        self.assertEqual([event["command"] for event in events], ["pytest -q", "git status"])
        self.assertNotIn("session-id", watcher._pending_commands)

    def test_expired_debounce_callback_cannot_drain_newer_commands(self) -> None:
        """Model a canceled timer that had already begun firing on Kitty's thread."""
        boss = Boss()
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "echo first", "time": 1785843000.0},
            )
            stale = FakeTimer.instances[-1]
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "echo second", "time": 1785843001.0},
            )
            current = FakeTimer.instances[-1]

            with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
                stale.fire()
                popen.assert_not_called()
                self.assertEqual(
                    [event["command"] for event in watcher._pending_commands["session-id"]],
                    ["echo first", "echo second"],
                )
                current.fire()
                events = cast(
                    list[dict[str, object]],
                    _written_payload(popen)["command_events"],
                )
                self.assertEqual(
                    [event["command"] for event in events],
                    ["echo first", "echo second"],
                )

                watcher.on_cmd_startstop(
                    boss,
                    Window(),
                    {"is_start": False, "cmdline": "echo third", "time": 1785843002.0},
                )
                newest = FakeTimer.instances[-1]
                popen.reset_mock()
                stale.fire()
                popen.assert_not_called()
                newest.fire()

        later_events = cast(
            list[dict[str, object]],
            _written_payload(popen)["command_events"],
        )
        self.assertEqual([event["command"] for event in later_events], ["echo third"])

    def test_cmd_w_can_drain_commands_while_an_autosave_process_is_in_flight(self) -> None:
        """Keep events queued until the isolated writer confirms persistence."""
        boss = Boss()
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "pwd", "time": 1785843000.0},
            )

        drained: list[dict[str, object]] = []

        def close_during_wait(timeout: float) -> int:
            self.assertEqual(timeout, watcher.AUTOSAVE_COMPLETION_TIMEOUT_SECONDS)
            drained.extend(watcher._drain_closing_events("session-id"))
            return 0

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.wait.side_effect = close_during_wait
            FakeTimer.instances[-1].fire()

        self.assertEqual([event["command"] for event in drained], ["pwd"])
        self.assertNotIn("session-id", watcher._pending_commands)

    def test_command_arriving_during_autosave_remains_queued(self) -> None:
        """Remove only events handled by a successful in-flight writer."""
        boss = Boss()
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_cmd_startstop(
                boss,
                Window(),
                {"is_start": False, "cmdline": "first", "time": 1785843000.0},
            )

        def append_new_event(timeout: float) -> int:
            del timeout
            watcher._pending_commands["session-id"].append(
                {
                    "window_id": 2,
                    "command": "second",
                    "completed_at": 1785843001.0,
                }
            )
            return 0

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.wait.side_effect = append_new_event
            FakeTimer.instances[-1].fire()

        self.assertEqual(
            [event["command"] for event in watcher._pending_commands["session-id"]],
            ["second"],
        )

    def test_failed_autosaves_leave_events_available_to_cmd_w(self) -> None:
        """Retain completed commands across launch, timeout, wait, and exit failures."""
        failures: tuple[object, ...] = (
            None,
            7,
            subprocess.TimeoutExpired("autosave", 30),
            OSError("wait failed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                watcher._timers.clear()
                watcher._timer_generations.clear()
                watcher._pending_commands.clear()
                FakeTimer.instances.clear()
                with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
                    watcher.on_cmd_startstop(
                        Boss(),
                        Window(),
                        {"is_start": False, "cmdline": "pwd", "time": 1785843000.0},
                    )
                process = mock.MagicMock()
                if isinstance(failure, BaseException):
                    process.wait.side_effect = failure
                else:
                    process.wait.return_value = failure
                launched = None if failure is None else process
                with mock.patch.object(watcher, "_launch_autosave", return_value=launched):
                    FakeTimer.instances[-1].fire()

                recovered = watcher._drain_closing_events("session-id")
                self.assertEqual([event["command"] for event in recovered], ["pwd"])

    def test_cmd_w_captures_pending_history_before_kitty_destroys_the_screen(self) -> None:
        """Persist reopened-pane changes immediately when Cmd-W closes the pane."""
        old_lines = "".join(f"old-{index:04d}\n" for index in range(1998))
        closing = Window(
            99,
            history=f"{old_lines}pwd\n/tmp/project\n",
            output="/tmp/project\n",
            alternate_screen=True,
            alternate_text="Processes: 412 total\nCPU usage: 8.4%\n",
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
        self.assertEqual(closing.history_requests, [(True, False, True)])
        screen = cast(Screen, closing.screen)
        self.assertEqual(screen.ansi_requests, [True])
        self.assertEqual(cast(LineBuffer, screen.main_linebuf).ansi_requests, [True])
        payload = _written_payload(popen)
        capture = cast(dict[str, object], payload["closing_pane"])
        self.assertEqual((capture["tab_index"], capture["pane_index"]), (0, 1))
        self.assertEqual(capture["terminal_history"], closing.history)
        self.assertEqual(capture["alternate_screen_text"], closing.alternate_text)
        self.assertEqual(capture["last_command_output"], "/tmp/project\n")
        events = cast(list[dict[str, object]], capture["command_events"])
        self.assertEqual([event["command"] for event in events], [["pwd"]])
        captured_window = cast(dict[str, object], capture["window"])
        self.assertEqual(captured_window["id"], 99)
        self.assertNotIn("env", captured_window)

    def test_shell_close_requests_ansi_scrollback_for_styled_prompts(self) -> None:
        """Capture Spaceship prompt colors from a normal shell's main screen."""
        history = "\x1b[38;2;245;130;65m ~/dotfiles \x1b[0m ls\n"
        window = Window(history=history)

        capture = watcher._closing_pane_capture(window, None, "session-id", [])

        self.assertEqual(capture["terminal_history"], history)
        self.assertEqual(capture["alternate_screen_text"], "")
        self.assertEqual(window.history_requests, [(True, True, False)])

    def test_triggered_autosave_timer_is_one_shot_not_a_polling_loop(self) -> None:
        """Complete one scheduled save without creating another timer."""
        with mock.patch("kitty_workbench.watcher.threading.Timer", FakeTimer):
            watcher.on_title_change(Boss(), Window(), {"title": "settled"})
            self.assertEqual(len(FakeTimer.instances), 1)
            with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
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
                self.assertIsNone(
                    watcher._launch_autosave("session-id", {}, {"command_events": []})
                )

        with mock.patch("kitty_workbench.watcher.subprocess.Popen") as popen:
            popen.return_value.stdin.write.side_effect = BrokenPipeError("closed")
            self.assertIs(
                watcher._launch_autosave("session-id", {}, {"command_events": []}),
                popen.return_value,
            )

    def test_unstable_text_and_location_apis_never_break_close(self) -> None:
        self.assertEqual(watcher._read_window_text(lambda: 7), "")

        def broken_reader() -> object:
            raise RuntimeError("screen destroyed")

        self.assertEqual(watcher._read_window_text(broken_reader), "")
        window = Window()
        broken_screen = mock.MagicMock()
        broken_screen.as_text_for_history_buf.side_effect = RuntimeError("screen destroyed")
        window.screen = cast(watcher.WatcherScreen, broken_screen)
        self.assertEqual(watcher._read_hidden_main_buffer(window), "")
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
        with (
            mock.patch.object(watcher, "_schedule") as schedule,
            mock.patch("kitty_workbench.watcher.time.time", return_value=1785843999.0),
        ):
            watcher.on_cmd_startstop(
                boss,
                window,
                {"is_start": False, "cmdline": ("git", "status"), "time": 1},
            )
        event = schedule.call_args.args[3]
        self.assertEqual(event["command"], ["git", "status"])
        self.assertEqual(event["cwd"], "/tmp/callable")
        self.assertEqual(event["completed_at"], 1785843999.0)

        window.child.foreground_cwd = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        with mock.patch.object(watcher, "_schedule") as schedule:
            watcher.on_cmd_startstop(boss, window, {"is_start": False, "cmdline": None})
        self.assertNotIn("cwd", schedule.call_args.args[3])

        window.child = None
        self.assertIsNone(watcher._foreground_cwd(window))

    def test_command_completion_replaces_each_invalid_kitty_clock_value(self) -> None:
        """Convert Kitty's monotonic callback time to one stable wall-clock value."""
        for reported in (None, True, 19.927957, "19.927957"):
            with (
                self.subTest(reported=reported),
                mock.patch.object(watcher, "_schedule") as schedule,
                mock.patch("kitty_workbench.watcher.time.time", return_value=1785843999.0),
            ):
                watcher.on_cmd_startstop(
                    Boss(),
                    Window(),
                    {"is_start": False, "cmdline": "pwd", "time": reported},
                )
            self.assertEqual(schedule.call_args.args[3]["completed_at"], 1785843999.0)


if __name__ == "__main__":
    unittest.main()
