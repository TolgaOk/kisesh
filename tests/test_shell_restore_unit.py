from __future__ import annotations

import io
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kisesh import shell_restore
from kisesh.context import build_context
from kisesh.domain import SessionContext
from kisesh.kitty_client import LiveTab
from kisesh.store import SessionStore


class ShellRestoreUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root / "data")
        self.stored = self.store.create("Restored Shell", "/tmp/project")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(
        self,
        commands: list[str],
        *,
        terminal_history: str = "",
    ) -> SessionContext:
        events = [
            {
                "window_id": 11,
                "command": command,
                "completed_at": f"2026-08-04T12:{index // 60:02d}:{index % 60:02d}Z",
            }
            for index, command in enumerate(commands)
        ]
        return build_context(
            [
                LiveTab(
                    1,
                    7,
                    0,
                    "Shell",
                    "splits",
                    [
                        {
                            "id": 11,
                            "cwd": "/tmp/project",
                            "foreground_processes": [{"cmdline": ["-zsh"]}],
                            "at_prompt": True,
                        }
                    ],
                )
            ],
            command_events=events,
            terminal_histories={11: terminal_history},
        )

    def test_private_zsh_state_keeps_2000_commands_inert_and_sources_user_config(self) -> None:
        user_zdotdir = self.root / "user zsh"
        commands = [f"printf command-{index:04d}" for index in range(2001)]
        commands.append("printf 'line one\nline two'\x00\x7f")

        state = shell_restore.prepare_zsh_startup(
            self.stored,
            self.context(commands),
            0,
            0,
            {"HOME": str(self.root), "ZDOTDIR": str(user_zdotdir)},
        )

        self.assertIsNotNone(state)
        assert state is not None
        history = (state / "history").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(history), 2000)
        self.assertEqual(history[0], "printf command-0002")
        self.assertEqual(history[-1], "printf 'line one line two'")
        self.assertNotIn("\x00", "".join(history))
        self.assertNotIn("\x7f", "".join(history))
        self.assertEqual((state / "original-zdotdir").read_text(), f"{user_zdotdir}\n")
        zshenv = (state / ".zshenv").read_text(encoding="utf-8")
        zshrc = (state / ".zshrc").read_text(encoding="utf-8")
        self.assertIn(str(user_zdotdir / ".zshenv"), zshenv)
        self.assertIn(f"export ZDOTDIR='{user_zdotdir}'", zshenv)
        self.assertIn("builtin fc -R", zshrc)
        self.assertNotIn(history[-1], zshrc)
        for path in (state / "history", state / ".zshenv", state / ".zshrc"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_shell_without_saved_commands_needs_no_private_startup(self) -> None:
        state = shell_restore.prepare_zsh_startup(
            self.stored,
            self.context([]),
            0,
            0,
            {"HOME": str(self.root)},
        )

        self.assertIsNone(state)
        self.assertFalse((self.stored.directory / "shell-state").exists())

    def test_original_zdotdir_survives_repeated_reopens_and_resolution_failures(self) -> None:
        state = self.root / "shell-state"
        state.mkdir()
        marker = state / "original-zdotdir"
        marker.write_text("/user/config\n", encoding="utf-8")

        self.assertEqual(shell_restore._original_zdotdir({"ZDOTDIR": "/other"}, state), "/other")
        self.assertEqual(
            shell_restore._original_zdotdir({"ZDOTDIR": str(state)}, state),
            "/user/config",
        )
        marker.unlink()
        self.assertEqual(shell_restore._original_zdotdir({"ZDOTDIR": str(state)}, state), "")
        with patch.object(Path, "resolve", side_effect=OSError("unreadable")):
            self.assertEqual(
                shell_restore._original_zdotdir({"ZDOTDIR": "/still-user-config"}, state),
                "/still-user-config",
            )

    def test_shell_resolution_uses_environment_passwd_and_safe_fallbacks(self) -> None:
        self.assertEqual(shell_restore._configured_shell({"SHELL": " /bin/zsh "}), "/bin/zsh")
        with patch.object(
            pwd,
            "getpwuid",
            return_value=SimpleNamespace(pw_shell="/bin/fish"),
        ):
            self.assertEqual(shell_restore._configured_shell({}), "/bin/fish")
        with patch.object(
            pwd,
            "getpwuid",
            return_value=SimpleNamespace(pw_shell=""),
        ):
            self.assertEqual(shell_restore._configured_shell({}), "/bin/sh")
        for error in (KeyError("uid"), OSError("passwd unavailable")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(pwd, "getpwuid", side_effect=error),
            ):
                self.assertEqual(shell_restore._configured_shell({}), "/bin/sh")

    def test_kitten_resolution_checks_explicit_path_path_lookup_and_macos_app(self) -> None:
        configured = self.root / "kitten"
        configured.write_text("binary", encoding="utf-8")
        self.assertEqual(
            shell_restore._kitten_executable({"KISESH_KITTEN": str(configured)}),
            str(configured),
        )

        with patch.object(shutil, "which", return_value="/path/bin/kitten"):
            self.assertEqual(
                shell_restore._kitten_executable({"PATH": "/path/bin"}),
                "/path/bin/kitten",
            )

        def macos_only(path: Path) -> bool:
            return str(path) == "/Applications/kitty.app/Contents/MacOS/kitten"

        with patch.object(shutil, "which", return_value=None):
            with patch.object(Path, "is_file", autospec=True, side_effect=macos_only):
                self.assertEqual(
                    shell_restore._kitten_executable({}),
                    "/Applications/kitty.app/Contents/MacOS/kitten",
                )
            with (
                patch.object(Path, "is_file", return_value=False),
                self.assertRaisesRegex(OSError, "cannot find the kitten"),
            ):
                shell_restore._kitten_executable({})

    def test_launch_command_adds_private_startup_only_for_zsh_with_history(self) -> None:
        kitten = self.root / "kitten"
        kitten.write_text("binary", encoding="utf-8")
        environment = {
            "HOME": str(self.root),
            "SHELL": "/bin/zsh -l",
            "KISESH_KITTEN": str(kitten),
        }

        zsh = shell_restore.shell_launch_command(
            self.stored,
            self.context(["pwd"]),
            0,
            0,
            environment,
        )
        empty_zsh = shell_restore.shell_launch_command(
            self.stored,
            self.context([]),
            0,
            0,
            environment,
        )
        fish = shell_restore.shell_launch_command(
            self.stored,
            self.context(["pwd"]),
            0,
            0,
            {**environment, "SHELL": "/bin/fish"},
        )
        fish_with_app = shell_restore.shell_launch_command(
            self.stored,
            self.context(["top"]),
            0,
            0,
            {**environment, "SHELL": "/bin/fish"},
            ["top"],
        )
        malformed = shell_restore.shell_launch_command(
            self.stored,
            self.context(["pwd"]),
            0,
            0,
            {**environment, "SHELL": "'"},
        )

        self.assertEqual(zsh[:3], [str(kitten), "run-shell", "--shell=/bin/zsh -l"])
        self.assertTrue(any(item.startswith("--env=ZDOTDIR=") for item in zsh))
        self.assertIn("--env=KISESH_RESTORING_SHELL=1", zsh)
        self.assertEqual(empty_zsh, [str(kitten), "run-shell", "--shell=/bin/zsh -l"])
        self.assertEqual(fish, [str(kitten), "run-shell", "--shell=/bin/fish"])
        self.assertEqual(
            fish_with_app,
            [str(kitten), "run-shell", "--shell=/bin/fish", "--", "top"],
        )
        self.assertEqual(malformed, [str(kitten), "run-shell", "--shell='"])

    def test_approved_app_runs_before_a_history_backed_restored_shell(self) -> None:
        kitten = self.root / "kitten"
        kitten.write_text("binary", encoding="utf-8")
        context = build_context(
            [
                LiveTab(
                    1,
                    7,
                    0,
                    "Monitor",
                    "splits",
                    [
                        {
                            "id": 11,
                            "cwd": "/tmp/project",
                            "foreground_processes": [],
                            "last_reported_cmdline": "top",
                            "at_prompt": False,
                            "in_alternate_screen": True,
                        }
                    ],
                )
            ],
            terminal_histories={11: "TOP FRAME\n"},
        )
        environment = {
            "HOME": str(self.root),
            "SHELL": "/bin/zsh",
            "KISESH_KITTEN": str(kitten),
        }

        command = shell_restore.shell_launch_command(
            self.stored,
            context,
            0,
            0,
            environment,
            ["top"],
        )

        self.assertNotIn("--", command)
        self.assertTrue(any(item.startswith("--env=ZDOTDIR=") for item in command))
        state = self.stored.directory / "shell-state" / "tab-0000-pane-0000"
        self.assertEqual((state / "history").read_text(encoding="utf-8"), "top\n")
        zshrc = (state / ".zshrc").read_text(encoding="utf-8")
        self.assertGreater(zshrc.index("command top"), zshrc.index("builtin source"))
        self.assertGreater(zshrc.index("command top"), zshrc.index("builtin fc -R"))

    def test_restored_shell_prints_scrollback_then_executes_normal_shell(self) -> None:
        scenarios = (
            ("saved output", f"saved output{shell_restore.SGR_RESET}\n"),
            (
                "\x1b[48;2;245;130;65m ~/dotfiles \x1b[0m",
                f"\x1b[48;2;245;130;65m ~/dotfiles \x1b[0m{shell_restore.SGR_RESET}\n",
            ),
            ("already terminated\n", f"already terminated\n{shell_restore.SGR_RESET}"),
            ("", ""),
        )
        for history, expected in scenarios:
            with self.subTest(history=history):
                output = io.StringIO()
                environment = {"SHELL": "/bin/zsh"}
                with (
                    patch.object(shell_restore, "pane_terminal_history", return_value=history),
                    patch.object(
                        shell_restore,
                        "shell_launch_command",
                        return_value=["/kitten", "run-shell", "--shell=/bin/zsh"],
                    ),
                    patch.object(os, "environ", environment),
                    patch.object(
                        os,
                        "execvpe",
                        side_effect=RuntimeError("exec boundary"),
                    ) as execute,
                    patch.object(sys, "stdout", output),
                    self.assertRaisesRegex(RuntimeError, "exec boundary"),
                ):
                    shell_restore.run_restored_shell(self.stored, None, 0, 0)

                self.assertEqual(output.getvalue(), expected)
                execute.assert_called_once_with(
                    "/kitten",
                    ["/kitten", "run-shell", "--shell=/bin/zsh"],
                    environment,
                )

    def test_restored_shell_rejects_an_exec_implementation_that_returns(self) -> None:
        with (
            patch.object(shell_restore, "pane_terminal_history", return_value=""),
            patch.object(shell_restore, "shell_launch_command", return_value=["/kitten"]),
            patch.object(os, "execvpe", return_value=None),
            self.assertRaisesRegex(AssertionError, "unexpectedly returned"),
        ):
            shell_restore.run_restored_shell(self.stored, None, 0, 0)


if __name__ == "__main__":
    unittest.main()
