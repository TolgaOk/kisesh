from __future__ import annotations

import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

from kisesh.context import build_context
from kisesh.kitty_client import LiveTab
from kisesh.store import SessionStore

PROJECT = Path(__file__).parents[1]
LAUNCHER = PROJECT / "bin" / "kisesh"


def _read_until(fd: int, needle: bytes, timeout: float = 8.0) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while needle not in output and time.monotonic() < deadline:
        readable, _, _ = select.select(
            [fd], [], [], min(0.1, max(0.0, deadline - time.monotonic()))
        )
        if not readable:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


class ShellRestoreTests(unittest.TestCase):
    def _session(self, root: Path, history_command: str, output: str) -> tuple[SessionStore, str]:
        store = SessionStore(root / "data")
        stored = store.create("Shell Restore", "/tmp/project")
        context = build_context(
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
            command_events=[
                {
                    "window_id": 11,
                    "command": history_command,
                    "completed_at": "2026-08-04T11:30:00Z",
                }
            ],
            command_outputs={11: output},
        )
        store.write_context(stored.manifest.id, context)
        return store, stored.manifest.id

    def test_output_reader_prints_text_without_executing_the_saved_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "must-not-exist"
            store, session_id = self._session(root, f"touch {sentinel}", "build completed")

            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "--data-dir",
                    str(store.root),
                    "print-last-output",
                    session_id,
                    "--tab-index",
                    "0",
                    "--pane-index",
                    "0",
                ],
                cwd=PROJECT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "build completed\n")
            self.assertFalse(sentinel.exists())

    @unittest.skipUnless(
        shutil.which("kitten") and shutil.which("zsh"), "kitten and zsh are required"
    )
    def test_rendered_output_precedes_a_normal_shell_with_scrollable_history(self) -> None:
        """Exercise output restore and ZLE history navigation in a real PTY."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "history-must-not-run"
            history_command = f"touch {sentinel}"
            store, session_id = self._session(root, history_command, "LAST BUILD OUTPUT\n")
            zdotdir = root / "zsh"
            zdotdir.mkdir()
            history_file = root / "zsh-history"
            history_file.write_text(history_command + "\n", encoding="utf-8")
            (zdotdir / ".zshrc").write_text(
                "\n".join(
                    (
                        f"HISTFILE={shlex.quote(str(history_file))}",
                        "HISTSIZE=100",
                        "SAVEHIST=100",
                        "setopt share_history",
                        'fc -R -- "$HISTFILE"',
                        "PS1='RESTORED> '",
                        "RPS1=''",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            command = [
                shutil.which("kitten") or "kitten",
                "run-shell",
                f"--shell={shutil.which('zsh') or '/bin/zsh'}",
                f"--env=ZDOTDIR={zdotdir}",
                "--",
                str(LAUNCHER),
                "--data-dir",
                str(store.root),
                "print-last-output",
                session_id,
                "--tab-index",
                "0",
                "--pane-index",
                "0",
            ]
            environment = os.environ.copy()
            environment["HOME"] = str(root)
            environment["TERM"] = "xterm-kitty"
            pid, master = pty.fork()
            if pid == 0:  # pragma: no cover - assertions run in the parent
                os.execvpe(command[0], command, environment)

            try:
                startup = _read_until(master, b"RESTORED> ")
                self.assertIn(b"LAST BUILD OUTPUT", startup)
                self.assertLess(startup.index(b"LAST BUILD OUTPUT"), startup.index(b"RESTORED> "))
                self.assertFalse(sentinel.exists(), "saved history was executed during restore")

                # Exercise the user's normal interactive history gesture.  The
                # recalled line must be editable at the prompt, not executed
                # as startup code by KiSesh.
                os.write(master, b"\x1b[A")
                history_view = _read_until(master, history_command.encode())
                self.assertIn(history_command.encode(), history_view)
                self.assertFalse(sentinel.exists(), "recalling history executed the saved command")
                os.write(master, b"\x03")
                _read_until(master, b"RESTORED> ")
                self.assertFalse(
                    sentinel.exists(), "cancelling a recalled command still executed it"
                )
            finally:
                try:
                    os.write(master, b"\x03")
                    os.write(master, b"exit\r")
                except OSError:
                    pass
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        break
                    time.sleep(0.05)
                else:
                    # Interactive shells can ignore SIGTERM. Kill the isolated
                    # forkpty process group so a failed test cannot leak one.
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(master)

    @unittest.skipUnless(
        shutil.which("kitten") and shutil.which("zsh"), "kitten and zsh are required"
    )
    def test_approved_app_uses_user_zsh_path_before_prompt(self) -> None:
        """Run a restored app found only through the user's zsh configuration."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_bin = root / "user-bin"
            user_bin.mkdir()
            htop = user_bin / "htop"
            htop.write_text("#!/bin/sh\nprintf '__RESTORED_HTOP__\\n'\n", encoding="utf-8")
            htop.chmod(0o755)

            store = SessionStore(root / "data")
            stored = store.create("Homebrew App", "/tmp/project")
            context = build_context(
                [
                    LiveTab(
                        1,
                        7,
                        0,
                        "htop",
                        "splits",
                        [
                            {
                                "id": 11,
                                "cwd": "/tmp/project",
                                "foreground_processes": [],
                                "last_reported_cmdline": "htop",
                                "at_prompt": False,
                                "in_alternate_screen": True,
                            }
                        ],
                    )
                ]
            )
            store.write_context(stored.manifest.id, context)

            user_zdotdir = root / "user-zsh"
            user_zdotdir.mkdir()
            (user_zdotdir / ".zshrc").write_text(
                "\n".join(
                    (
                        f"export PATH={shlex.quote(str(user_bin))}:$PATH",
                        "PS1='PATH-RESTORED> '",
                        "RPS1=''",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            empty_path = root / "empty-path"
            empty_path.mkdir()
            command = [
                sys.executable,
                "-m",
                "kisesh",
                "--data-dir",
                str(store.root),
                "restore-shell",
                stored.manifest.id,
                "--tab-index",
                "0",
                "--pane-index",
                "0",
            ]
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root),
                    "SHELL": shutil.which("zsh") or "/bin/zsh",
                    "TERM": "xterm-kitty",
                    "PATH": str(empty_path),
                    "PYTHONPATH": str(PROJECT),
                    "ZDOTDIR": str(user_zdotdir),
                    "KISESH_KITTEN": shutil.which("kitten") or "kitten",
                }
            )
            pid, master = pty.fork()
            if pid == 0:  # pragma: no cover - assertions run in the parent
                os.execvpe(command[0], command, environment)

            try:
                startup = _read_until(master, b"PATH-RESTORED> ", timeout=12)
                self.assertIn(b"__RESTORED_HTOP__", startup)
                self.assertLess(
                    startup.index(b"__RESTORED_HTOP__"),
                    startup.index(b"PATH-RESTORED> "),
                )
                self.assertNotIn(b"executable file not found", startup)
                self.assertNotIn(b"command not found", startup)
            finally:
                with suppress(OSError):
                    os.write(master, b"\x03exit\r")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        break
                    time.sleep(0.05)
                else:
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(master)

    @unittest.skipUnless(
        shutil.which("kitten") and shutil.which("zsh"), "kitten and zsh are required"
    )
    def test_two_reopens_restore_2000_scrollback_lines_and_shell_commands(self) -> None:
        """Exercise KiSesh's own saved state twice in real interactive zsh PTYs."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionStore(root / "data")
            stored = store.create("Deep History", "/tmp/project")
            sentinel = root / "recalled-command-must-not-run"
            dangerous = f"touch {shlex.quote(str(sentinel))}"
            commands = [
                {
                    "window_id": 11,
                    "command": f"printf history-{index:04d}",
                    "completed_at": f"2026-08-04T11:{index // 60 % 60:02d}:{index % 60:02d}Z",
                }
                for index in range(2004)
            ] + [
                {
                    "window_id": 11,
                    "command": dangerous,
                    "completed_at": "2026-08-04T12:00:00Z",
                }
            ]
            scrollback = "".join(f"SCROLL-{index:04d}\n" for index in range(2010))
            context = build_context(
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
                                "last_reported_cmdline": dangerous,
                                "at_prompt": True,
                            }
                        ],
                    )
                ],
                command_events=commands,
                terminal_histories={11: scrollback},
            )
            store.write_context(stored.manifest.id, context)
            user_zdotdir = root / "user-zsh"
            user_zdotdir.mkdir()
            (user_zdotdir / ".zshenv").write_text(
                "export KISESH_TEST_ZSHENV=loaded\n",
                encoding="utf-8",
            )
            (user_zdotdir / ".zshrc").write_text(
                "\n".join(
                    (
                        "HISTFILE=/dev/null",
                        "HISTSIZE=10",
                        "SAVEHIST=0",
                        "unsetopt share_history",
                        "PS1='RESTORED> '",
                        "[[ $KISESH_TEST_ZSHENV == loaded ]] || PS1='BROKEN-ZSHENV> '",
                        "RPS1=''",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            command = [
                str(LAUNCHER),
                "--data-dir",
                str(store.root),
                "restore-shell",
                stored.manifest.id,
                "--tab-index",
                "0",
                "--pane-index",
                "0",
            ]

            def exercise_reopen() -> None:
                environment = os.environ.copy()
                environment.update(
                    {
                        "HOME": str(root),
                        "SHELL": shutil.which("zsh") or "/bin/zsh",
                        "TERM": "xterm-kitty",
                        "ZDOTDIR": str(user_zdotdir),
                    }
                )
                pid, master = pty.fork()
                if pid == 0:  # pragma: no cover - assertions run in the parent
                    os.execvpe(command[0], command, environment)

                try:
                    startup = _read_until(master, b"RESTORED> ", timeout=12)
                    self.assertNotIn(b"SCROLL-0009", startup)
                    self.assertIn(b"SCROLL-0010", startup)
                    self.assertIn(b"SCROLL-2009", startup)
                    self.assertFalse(sentinel.exists())

                    os.write(master, b"\x1b[A")
                    recalled = _read_until(master, dangerous.encode(), timeout=5)
                    self.assertIn(dangerous.encode(), recalled)
                    self.assertFalse(
                        sentinel.exists(), "Up must recall, not execute, saved history"
                    )
                    os.write(master, b"\x03")
                    _read_until(master, b"RESTORED> ")

                    os.write(master, b"print -r -- __HISTORY_COUNT__${#history}\r")
                    count = _read_until(master, b"__HISTORY_COUNT__2000", timeout=5)
                    self.assertIn(b"__HISTORY_COUNT__2000", count)
                    self.assertFalse(sentinel.exists())
                    _read_until(master, b"RESTORED> ", timeout=5)

                    os.write(master, b"print -r -- __KISESH_COMMAND_EVENT__\r")
                    completed = _read_until(master, b"RESTORED> ", timeout=5)
                    self.assertIn(b"__KISESH_COMMAND_EVENT__", completed)
                    self.assertIn(b"\x1b]133;C;cmdline=", completed)
                finally:
                    with suppress(OSError):
                        os.write(master, b"\x03exit\r")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        waited, _ = os.waitpid(pid, os.WNOHANG)
                        if waited == pid:
                            break
                        time.sleep(0.05)
                    else:
                        os.killpg(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    os.close(master)

            exercise_reopen()
            exercise_reopen()
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
