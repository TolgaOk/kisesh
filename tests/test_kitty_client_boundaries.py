from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kisesh import legacy
from kisesh.domain import KittyOsWindowState
from kisesh.kitty_client import (
    SESSION_CLOSE_KITTEN,
    SESSION_FILTER_KITTEN,
    KittyClient,
    KittyError,
    LiveTab,
    _find_kitty,
    _find_socket,
    _is_socket,
    _require_snapshot,
    _run_command,
)
from kisesh.model import (
    CAPTURE_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
)
from tests.fakes import RecordingCommandRunner


class RaisingRunner:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        input: str | None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del command, check, capture_output, text, input, timeout
        raise self.error


class FailAtRunner(RecordingCommandRunner):
    def __init__(self, fail_at: int) -> None:
        super().__init__(stderr="capture failed")
        self.fail_at = fail_at

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        input: str | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.returncode = 1 if len(self.commands) == self.fail_at else 0
        return super().__call__(
            command,
            check=check,
            capture_output=capture_output,
            text=text,
            input=input,
            timeout=timeout,
        )


class KittyClientBoundaryTests(unittest.TestCase):
    def test_default_runner_and_socketless_command_preserve_stdout_and_stdin(self) -> None:
        result = _run_command(
            ["/usr/bin/printf", "%s", "ok"],
            check=False,
            capture_output=True,
            text=True,
            input=None,
            timeout=5,
        )
        self.assertEqual(result.stdout, "ok")

        runner = RecordingCommandRunner(stdout="done")
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("kisesh.kitty_client._find_socket", return_value=None),
        ):
            client = KittyClient(executable="/kitty", runner=runner)
        self.assertEqual(client.command("ls", check=False), "done")
        self.assertEqual(runner.commands, [["/kitty", "@", "ls"]])

    def test_remote_failures_and_invalid_state_are_reported_without_tracebacks(self) -> None:
        for error in (OSError("spawn failed"), subprocess.TimeoutExpired("kitty", 15)):
            with (
                self.subTest(error=type(error).__name__),
                self.assertRaisesRegex(KittyError, "cannot run Kitty remote command"),
            ):
                KittyClient(
                    executable="/kitty",
                    socket="unix:/tmp/test",
                    runner=RaisingRunner(error),
                ).command("ls")

        for stderr, stdout, expected in (
            ("permission denied", "", "permission denied"),
            ("", "remote failure", "remote failure"),
        ):
            runner = RecordingCommandRunner(stdout=stdout, stderr=stderr, returncode=2)
            client = KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner)
            with self.subTest(expected=expected), self.assertRaisesRegex(KittyError, expected):
                client.command("ls")
            self.assertEqual(client.command("ls", check=False), stdout)

        for payload, message in (("not-json", "invalid window state"), ("{}", "not a list")):
            client = KittyClient(
                executable="/kitty",
                socket="unix:/tmp/test",
                runner=RecordingCommandRunner(stdout=payload),
            )
            with self.subTest(payload=payload), self.assertRaisesRegex(KittyError, message):
                client.list_state()

    def test_live_tab_root_and_representative_pane_follow_focus_with_safe_fallbacks(self) -> None:
        with self.assertRaisesRegex(KittyError, "has no windows"):
            _ = LiveTab(1, 2, 0, "Empty", "splits", []).representative_window_id

        tab = LiveTab(
            1,
            2,
            0,
            "Work",
            "splits",
            [
                {"id": 3, "cwd": "/older", "last_focused_at": 1},
                {
                    "id": 4,
                    "is_active": True,
                    "cwd": "/pane",
                    "last_focused_at": 2,
                    "foreground_processes": [{"cwd": ""}],
                },
            ],
        )
        self.assertEqual(tab.representative_window_id, 4)
        self.assertEqual(tab.suggested_root(), "/pane")
        self.assertIsNone(tab.session_id())

        with mock.patch.object(Path, "cwd", return_value=Path("/fallback")):
            self.assertEqual(
                LiveTab(1, 2, 0, "No cwd", "splits", [{"id": 5}]).suggested_root(),
                "/fallback",
            )

    def test_tab_parsing_drops_empty_ui_tabs_and_supplies_stable_defaults(self) -> None:
        state: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "windows": [{"id": 3, "user_vars": {"kisesh_ui": "YES"}}],
                    },
                    {
                        "id": 4,
                        "title": "",
                        "layout": "",
                        "windows": [{"id": 5, "user_vars": {"kisesh_ui": "false"}}],
                    },
                ],
            }
        ]
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test")

        tabs = client.tabs(state)

        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].tab_id, 4)
        self.assertEqual(tabs[0].title, "untitled")
        self.assertEqual(tabs[0].layout, "splits")

    def test_focused_tab_reports_empty_and_overlay_only_states_precisely(self) -> None:
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test")
        with self.assertRaisesRegex(KittyError, "no OS windows"):
            client.focused_tab([])

        overlay_only: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "windows": [{"id": 3, "user_vars": {"kisesh_ui": "yes"}}],
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(KittyError, "no usable tabs"):
            client.focused_tab(overlay_only)
        with self.assertRaisesRegex(KittyError, "outside the manager"):
            client.focused_tab(overlay_only, exclude_window_id=3)

    def test_session_filter_restamp_focus_and_open_route_exact_remote_commands(self) -> None:
        session_id = "session-id"
        state: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "windows": [
                            {"id": 3, "user_vars": {SESSION_ID_VAR: session_id}},
                            {"id": 4, "user_vars": {SESSION_ID_VAR: session_id}},
                        ],
                    },
                    {
                        "id": 5,
                        "windows": [{"id": 6, "user_vars": {SESSION_ID_VAR: "other"}}],
                    },
                ],
            }
        ]
        runner = RecordingCommandRunner(stdout=json.dumps(state))
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner)

        self.assertEqual([tab.tab_id for tab in client.tabs_for_session(session_id, state)], [2])
        client.restamp_session(session_id, "renamed", "Renamed Session")
        client.focus_tab(2)
        client.open_snapshot(Path("/tmp/session.kitty-session"))

        set_commands = [command for command in runner.commands if "set-user-vars" in command]
        self.assertEqual(len(set_commands), 1)
        self.assertIn("id:3 or id:4", set_commands[0])
        self.assertIn(f"{SESSION_SLUG_VAR}=renamed", set_commands[0])
        self.assertIn(f"{SESSION_NAME_VAR}=Renamed Session", set_commands[0])
        self.assertIn("focus-tab", runner.commands[-2])
        self.assertIn("goto_session", runner.commands[-1])

    def test_session_activation_and_close_use_native_runtime_preserving_transitions(
        self,
    ) -> None:
        session_id = "session-id"
        state: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "windows": [
                            {"id": 3, "user_vars": {SESSION_ID_VAR: session_id}},
                            {"id": 4, "user_vars": {SESSION_ID_VAR: session_id}},
                        ],
                    },
                    {"id": 5, "windows": [{"id": 6, "user_vars": {}}]},
                ],
            },
            {
                "id": 7,
                "tabs": [
                    {
                        "id": 8,
                        "windows": [{"id": 9, "user_vars": {SESSION_SCOPE_VAR: "stale"}}],
                    }
                ],
            },
        ]
        runner = RecordingCommandRunner(stdout=json.dumps(state))
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner)
        target = client.tabs(state)[0]

        client.activate_session(session_id, target)

        scope_commands = [
            command for command in runner.commands if SESSION_SCOPE_VAR in " ".join(command)
        ]
        self.assertEqual(
            [command[command.index("--match") + 1] for command in scope_commands[:-1]],
            ["id:3 or id:4 or id:6", "id:9"],
        )
        self.assertEqual(
            scope_commands[0][-2:],
            [f"{SESSION_SCOPE_VAR}=1", legacy.SESSION_SCOPE_VARIABLE],
        )
        self.assertEqual(
            scope_commands[1][-2:],
            [SESSION_SCOPE_VAR, legacy.SESSION_SCOPE_VARIABLE],
        )
        self.assertEqual(runner.commands[-2][-1], "id:2")
        self.assertEqual(
            runner.commands[-1][-3:],
            [
                "kitten",
                str(SESSION_FILTER_KITTEN),
                f"var:{SESSION_ID_VAR}={session_id} or "
                f"var:{legacy.SESSION_ID_VARIABLE}={session_id} or "
                f"not var:{SESSION_SCOPE_VAR}=1",
            ],
        )
        self.assertFalse(any("load-config" in command for command in runner.commands))

        successor_id = "successor-id"
        successor = LiveTab(
            1,
            5,
            1,
            "Next",
            "splits",
            [{"id": 6, "user_vars": {SESSION_ID_VAR: successor_id}}],
        )
        client.close_session_tabs(session_id, successor)

        self.assertEqual(
            runner.commands[-1][-5:],
            [
                "kitten",
                str(SESSION_CLOSE_KITTEN),
                session_id,
                successor_id,
                "5",
            ],
        )

        client.close_session_tabs(session_id)
        self.assertEqual(
            runner.commands[-1][-5:],
            ["kitten", str(SESSION_CLOSE_KITTEN), session_id, "-", "-"],
        )

        command_count = len(runner.commands)
        with self.assertRaisesRegex(KittyError, "cannot preserve an unowned tab"):
            client.close_session_tabs(
                session_id,
                LiveTab(1, 6, 2, "Unowned", "splits", [{"id": 7, "user_vars": {}}]),
            )
        self.assertEqual(len(runner.commands), command_count)

        client.close_tabs([5, 5, 8])
        self.assertEqual(runner.commands[-1][-2:], ["--match", "id:5 or id:8"])
        command_count = len(runner.commands)
        client.close_tabs([])
        self.assertEqual(len(runner.commands), command_count)

    def test_session_close_never_uses_a_second_destructive_remote_command(self) -> None:
        runner = FailAtRunner(fail_at=0)
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner)

        with self.assertRaisesRegex(KittyError, "capture failed"):
            client.close_session_tabs("session-id")

        self.assertEqual(len(runner.commands), 1)
        self.assertIn("kitten", runner.commands[0])
        self.assertIn(str(SESSION_CLOSE_KITTEN), runner.commands[0])
        self.assertNotIn("load-config", runner.commands[0])
        self.assertNotIn("close-tab", runner.commands[0])

    def test_session_capture_uses_one_action_compatible_restamped_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "session.kitty-session"
            snapshot.write_text("new_tab Work\n", encoding="utf-8")
            runner = RecordingCommandRunner()
            client = KittyClient(
                executable="/kitty",
                socket="unix:/tmp/test",
                runner=runner,
            )

            client.capture_session("session-id", snapshot)

        match = next(argument for argument in runner.commands[0] if argument.startswith("--match="))
        self.assertEqual(match, f"--match=var:{SESSION_ID_VAR}=session-id")
        self.assertNotIn(" or ", match)

    def test_capture_operations_validate_files_and_always_clear_temporary_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tab_snapshot = root / "tab.kitty-session"
            tab_snapshot.write_text("new_tab Tab\n", encoding="utf-8")
            runner = RecordingCommandRunner()
            client = KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner)
            tab = LiveTab(1, 2, 0, "Work", "splits", [{"id": 3}, {"id": 4}])

            client.capture_tab(tab, tab_snapshot, "capture-id")

            capture_commands = [
                command for command in runner.commands if CAPTURE_VAR in " ".join(command)
            ]
            self.assertEqual(len(capture_commands), 3)
            self.assertTrue(
                any(f"{CAPTURE_VAR}=capture-id" in command for command in capture_commands)
            )
            self.assertTrue(
                any(
                    CAPTURE_VAR in command and f"{CAPTURE_VAR}=" not in command
                    for command in capture_commands
                )
            )

            for missing in (root / "missing", root / "empty"):
                if missing.name == "empty":
                    missing.touch()
                with (
                    self.subTest(path=missing),
                    self.assertRaisesRegex(KittyError, "did not produce"),
                ):
                    _require_snapshot(missing, "test")

            failing = FailAtRunner(fail_at=2)
            failing_client = KittyClient(
                executable="/kitty",
                socket="unix:/tmp/test",
                runner=failing,
            )
            with self.assertRaisesRegex(KittyError, "capture failed"):
                failing_client.capture_tab(tab, root / "never-written", "failed-id")
            self.assertIn(CAPTURE_VAR, failing.commands[-1][-1])
            self.assertNotIn(f"{CAPTURE_VAR}=", failing.commands[-1])

    def test_empty_prefill_is_a_noop(self) -> None:
        runner = RecordingCommandRunner()
        KittyClient(executable="/kitty", socket="unix:/tmp/test", runner=runner).send_text(3, "")
        self.assertEqual(runner.commands, [])

    def test_executable_and_socket_discovery_are_unambiguous(self) -> None:
        with mock.patch.object(shutil, "which", return_value="/bin/kitty"):
            self.assertEqual(_find_kitty(), "/bin/kitty")

        def macos_only(path: Path) -> bool:
            return str(path) == "/Applications/kitty.app/Contents/MacOS/kitty"

        with (
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.object(Path, "exists", autospec=True, side_effect=macos_only),
        ):
            self.assertEqual(_find_kitty(), "/Applications/kitty.app/Contents/MacOS/kitty")
        with (
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.object(Path, "exists", return_value=False),
            self.assertRaisesRegex(KittyError, "cannot find the Kitty"),
        ):
            _find_kitty()

        first = Path("/tmp/mykitty")
        second = Path("/tmp/mykitty-two")
        with (
            mock.patch.object(Path, "glob", return_value=iter([second])),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("kisesh.kitty_client._is_socket", return_value=True),
        ):
            self.assertIsNone(_find_socket())
        with (
            mock.patch.object(Path, "glob", return_value=iter([])),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("kisesh.kitty_client._is_socket", return_value=True),
        ):
            self.assertEqual(_find_socket(), f"unix:{first}")
        with (
            mock.patch.object(Path, "glob", return_value=iter([second])),
            mock.patch.object(Path, "exists", side_effect=(OSError("unreadable"), False)),
            mock.patch("kisesh.kitty_client._is_socket", return_value=True),
        ):
            self.assertIsNone(_find_socket())

    def test_socket_probe_distinguishes_unix_sockets_from_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_text("text", encoding="utf-8")
            self.assertFalse(_is_socket(regular))
            with mock.patch.object(
                Path,
                "stat",
                return_value=SimpleNamespace(st_mode=stat.S_IFSOCK),
            ):
                self.assertTrue(_is_socket(root / "socket"))


if __name__ == "__main__":
    unittest.main()
