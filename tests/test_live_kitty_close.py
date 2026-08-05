"""Opt-in end-to-end close tests against an isolated hidden Kitty server."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

from kitty_workbench.domain import KittyOsWindowState, KittyTabState, KittyWindow
from kitty_workbench.model import SESSION_ID_VAR, SESSION_SLUG_VAR, WORKBENCH_UI_VAR
from kitty_workbench.store import SessionStore

PROJECT = Path(__file__).parents[1]
LIVE_TESTS_ENABLED = os.environ.get("KITTY_WORKBENCH_LIVE_TESTS") == "1"
LIVE_TEST_REASON = "set KITTY_WORKBENCH_LIVE_TESTS=1 to start an isolated hidden Kitty"
StatePredicate = Callable[[list[KittyOsWindowState]], bool]


class IsolatedKitty:
    """Own one temporary hidden Kitty process and its exact remote socket."""

    def __init__(self, root: Path) -> None:
        """Create isolated paths and environment without starting Kitty yet."""
        self.root = root
        self.home = root / "home"
        self.data = root / "data" / "kitty-workbench"
        self.config = root / "kitty.conf"
        self.socket = f"unix:{root / 'kitty.sock'}"
        self.kitty = shutil.which("kitty") or "kitty"
        self.kitten = shutil.which("kitten") or "kitten"
        self.process: subprocess.Popen[str] | None = None
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(root / "data"),
            }
        )

    def start(self) -> None:
        """Install the checkout into a temporary home and start Kitty hidden."""
        install = self.home / ".local" / "lib" / "kitty-workbench"
        install.parent.mkdir(parents=True)
        install.symlink_to(PROJECT, target_is_directory=True)
        self.config.write_text(
            "allow_remote_control yes\n"
            f"listen_on {self.socket}\n"
            "confirm_os_window_close 0\n"
            "include ~/.local/lib/kitty-workbench/integration/kitty-workbench.conf\n",
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [
                self.kitty,
                "--config",
                str(self.config),
                "--start-as=hidden",
                "--listen-on",
                self.socket,
                "--title",
                "Workbench isolated close test",
                "/bin/sh",
                "-c",
                "while :; do sleep 1; done",
            ],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for(lambda state: len(_tabs(state)) == 1)

    def remote(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command against only the temporary Kitty socket."""
        result = subprocess.run(
            [self.kitten, "@", "--to", self.socket, *arguments],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result

    def state(self) -> list[KittyOsWindowState]:
        """Return the temporary server's decoded live state."""
        result = self.remote("ls")
        return cast(list[KittyOsWindowState], json.loads(result.stdout))

    def wait_for(
        self,
        predicate: StatePredicate,
        timeout: float = 15.0,
    ) -> list[KittyOsWindowState]:
        """Poll bounded live state until a practical lifecycle condition holds."""
        deadline = time.monotonic() + timeout
        last_error = "Kitty did not accept remote control"
        while time.monotonic() < deadline:
            process = self.process
            if process is not None and process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise AssertionError(f"isolated Kitty exited early: {stderr}")
            result = self.remote("ls", check=False)
            if result.returncode == 0:
                try:
                    state = cast(list[KittyOsWindowState], json.loads(result.stdout))
                except json.JSONDecodeError as error:
                    last_error = str(error)
                else:
                    if predicate(state):
                        return state
                    last_error = f"last state: {result.stdout[:2000]}"
            else:
                last_error = result.stderr or result.stdout
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for isolated Kitty: {last_error}")

    def stop(self) -> None:
        """Close every disposable window, then terminate only this test process."""
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            self.remote("close-window", "--match", "all", check=False)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


def _tabs(state: list[KittyOsWindowState]) -> list[KittyTabState]:
    """Flatten tabs across the isolated server's operating-system windows."""
    return [tab for os_window in state for tab in os_window.get("tabs", [])]


def _session_tabs(state: list[KittyOsWindowState], session_id: str) -> list[KittyTabState]:
    """Return tabs containing at least one pane stamped with a session ID."""
    return [
        tab
        for tab in _tabs(state)
        if any(
            window.get("user_vars", {}).get(SESSION_ID_VAR) == session_id
            for window in tab.get("windows", [])
        )
    ]


def _session_windows(state: list[KittyOsWindowState], session_id: str) -> list[KittyWindow]:
    """Flatten all panes belonging to one live Workbench session."""
    return [window for tab in _session_tabs(state, session_id) for window in tab.get("windows", [])]


def _ui_windows(state: list[KittyOsWindowState]) -> list[KittyWindow]:
    """Return native prompts marked as transient Workbench UI."""
    return [
        window
        for tab in _tabs(state)
        for window in tab.get("windows", [])
        if window.get("user_vars", {}).get(WORKBENCH_UI_VAR) == "yes"
    ]


def _mapped_close_action() -> list[str]:
    """Read the exact action attached to Command-W in the shipped integration."""
    integration = (PROJECT / "integration/kitty-workbench.conf").read_text(encoding="utf-8")
    definition = next(
        line.split(maxsplit=2)[2]
        for line in integration.splitlines()
        if line.startswith("map cmd+w ")
    )
    return shlex.split(definition)


def _focus_session(server: IsolatedKitty, session_id: str) -> int:
    """Focus one session tab and return its active pane identifier."""
    server.remote("focus-tab", "--match", f"var:{SESSION_ID_VAR}={session_id}")
    state = server.wait_for(
        lambda current: any(tab.get("is_active") for tab in _session_tabs(current, session_id))
    )
    tabs = _session_tabs(state, session_id)
    active = next((tab for tab in tabs if tab.get("is_active")), tabs[0])
    windows = active.get("windows", [])
    return next((window["id"] for window in windows if window.get("is_active")), windows[0]["id"])


@unittest.skipUnless(LIVE_TESTS_ENABLED, LIVE_TEST_REASON)
@unittest.skipUnless(shutil.which("kitty") and shutil.which("kitten"), "Kitty is required")
class LiveKittyCloseTests(unittest.TestCase):
    """Exercise the resolved close action through a disposable real Kitty boss."""

    def test_command_w_action_closes_one_tab_then_guards_saves_and_promotes(self) -> None:
        """Run the complete multi-tab and final-tab lifecycle in hidden Kitty."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                created = subprocess.run(
                    [
                        str(PROJECT / "bin" / "kitty-workbench"),
                        "--socket",
                        server.socket,
                        "create",
                        "Closing",
                    ],
                    env=server.environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                store = SessionStore(server.data)
                closing = store.get("closing")
                successor = store.create("Successor", "/tmp/successor")
                child = ["/bin/sh", "-c", "while :; do sleep 1; done"]
                for title, session in (("Closing two", closing), ("Successor", successor)):
                    server.remote(
                        "launch",
                        "--type=tab",
                        f"--tab-title={title}",
                        f"--var={SESSION_ID_VAR}={session.manifest.id}",
                        f"--var={SESSION_SLUG_VAR}={session.manifest.slug}",
                        *child,
                    )
                server.wait_for(lambda state: len(_tabs(state)) == 3)

                first_window_id = _focus_session(server, closing.manifest.id)
                server.remote(
                    "action",
                    "--match",
                    f"id:{first_window_id}",
                    *_mapped_close_action(),
                )
                server.wait_for(
                    lambda state: (
                        len(_session_tabs(state, closing.manifest.id)) == 1
                        and len(_session_tabs(state, successor.manifest.id)) == 1
                    )
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if store.get(closing.manifest.id).manifest.summary.tab_count == 1:
                        break
                    time.sleep(0.05)
                self.assertEqual(
                    store.get(closing.manifest.id).manifest.summary.tab_count,
                    1,
                )

                final_window_id = _focus_session(server, closing.manifest.id)
                server.remote(
                    "action",
                    "--match",
                    f"id:{final_window_id}",
                    *_mapped_close_action(),
                )
                prompt_state = server.wait_for(lambda state: len(_ui_windows(state)) == 1)
                self.assertEqual(len(_session_tabs(prompt_state, closing.manifest.id)), 1)
                prompt_window_id = _ui_windows(prompt_state)[0]["id"]
                server.remote(
                    "action",
                    "--match",
                    f"id:{prompt_window_id}",
                    "close_window",
                )
                cancelled = server.wait_for(lambda state: not _ui_windows(state))
                self.assertEqual(len(_session_tabs(cancelled, closing.manifest.id)), 1)

                final_window_id = _focus_session(server, closing.manifest.id)
                server.remote(
                    "action",
                    "--match",
                    f"id:{final_window_id}",
                    *_mapped_close_action(),
                )
                prompt_state = server.wait_for(lambda state: len(_ui_windows(state)) == 1)
                prompt_window_id = _ui_windows(prompt_state)[0]["id"]
                server.remote(
                    "send-text",
                    "--match",
                    f"id:{prompt_window_id}",
                    "y",
                )
                promoted = server.wait_for(
                    lambda state: (
                        not _session_tabs(state, closing.manifest.id)
                        and len(_session_tabs(state, successor.manifest.id)) == 1
                        and any(
                            tab.get("is_active")
                            for tab in _session_tabs(state, successor.manifest.id)
                        )
                    ),
                    timeout=30,
                )

                self.assertEqual(_session_windows(promoted, closing.manifest.id), [])
                self.assertEqual(len(_session_tabs(promoted, successor.manifest.id)), 1)
                self.assertEqual(store.get(closing.manifest.id).manifest.summary.tab_count, 1)
                self.assertIsNotNone(store.read_context(closing.manifest.id))
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
