"""Opt-in end-to-end close tests against an isolated hidden Kitty server."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

from kisesh.kitty_client import KittyClient
from kisesh.model import (
    KISESH_UI_VAR,
    SESSION_ID_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    KittyOsWindowState,
    KittyTabState,
    KittyWindow,
)
from kisesh.store import SessionStore

PROJECT = Path(__file__).parents[1]
LIVE_TESTS_ENABLED = os.environ.get("KISESH_LIVE_TESTS") == "1"
LIVE_TEST_REASON = "set KISESH_LIVE_TESTS=1 to start an isolated hidden Kitty"
StatePredicate = Callable[[list[KittyOsWindowState]], bool]


class IsolatedKitty:
    """Own one temporary hidden Kitty process and its exact remote socket."""

    def __init__(self, root: Path) -> None:
        """Create isolated paths and environment without starting Kitty yet."""
        self.root = root
        self.home = root / "home"
        self.data = root / "data" / "kisesh"
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
        install = self.home / ".local" / "lib" / "kisesh"
        binary = install / "bin"
        install.mkdir(parents=True)
        binary.mkdir()
        (install / "kisesh").symlink_to(PROJECT / "kisesh", target_is_directory=True)
        (install / "integration").symlink_to(
            PROJECT / "kisesh" / "integration", target_is_directory=True
        )
        (binary / "kisesh").symlink_to(Path(sys.executable).with_name("kisesh"))
        self.config.write_text(
            "allow_remote_control yes\n"
            f"listen_on {self.socket}\n"
            "font_size 13\n"
            "confirm_os_window_close 0\n"
            "include ~/.local/lib/kisesh/integration/kisesh.conf\n",
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
                "KiSesh isolated close test",
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

    def tab_filter(self) -> str:
        """Read the isolated process's effective native tab filter."""
        probe = self.root / "tab_filter_probe.py"
        probe.write_text(
            '"""Report Kitty\'s effective native tab filter."""\n'
            "from kittens.tui.handler import result_handler\n"
            "from kitty.fast_data_types import get_options\n"
            "def main(args):\n"
            "    del args\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target_window_id, boss):\n"
            "    del args, answer, target_window_id, boss\n"
            "    return get_options().tab_bar_filter\n",
            encoding="utf-8",
        )
        return self.remote("kitten", str(probe)).stdout.strip()

    def configured_font_size(self) -> float:
        """Read the font size from the isolated process's current options."""
        probe = self.root / "font_size_probe.py"
        probe.write_text(
            '"""Report Kitty\'s configured font size."""\n'
            "from kittens.tui.handler import result_handler\n"
            "from kitty.fast_data_types import get_options\n"
            "def main(args):\n"
            "    del args\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target_window_id, boss):\n"
            "    del args, answer, target_window_id, boss\n"
            "    return str(get_options().font_size)\n",
            encoding="utf-8",
        )
        return float(self.remote("kitten", str(probe)).stdout.strip())

    def visible_session_ids(self) -> dict[str, list[str]]:
        """Return the session identities each native OS-window bar exposes."""
        probe = self.root / "visible_tabs_probe.py"
        probe.write_text(
            '"""Report each native tab bar\'s filtered sessions."""\n'
            "import json\n"
            "from kittens.tui.handler import result_handler\n"
            "def main(args):\n"
            "    del args\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target_window_id, boss):\n"
            "    del args, answer, target_window_id\n"
            "    return json.dumps({\n"
            "        str(manager.os_window_id): [\n"
            "            next((\n"
            "                window.user_vars.get('kisesh_session', '')\n"
            "                for window in tab\n"
            "                if window.user_vars.get('kisesh_session')\n"
            "            ), '')\n"
            "            for tab in manager.tabs_to_be_shown_in_tab_bar\n"
            "        ]\n"
            "        for manager in boss.all_tab_managers\n"
            "    })\n",
            encoding="utf-8",
        )
        result = json.loads(self.remote("kitten", str(probe)).stdout)
        return cast(dict[str, list[str]], result)

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
    """Flatten all panes belonging to one live KiSesh session."""
    return [window for tab in _session_tabs(state, session_id) for window in tab.get("windows", [])]


def _ui_windows(state: list[KittyOsWindowState]) -> list[KittyWindow]:
    """Return native prompts marked as transient KiSesh UI."""
    return [
        window
        for tab in _tabs(state)
        for window in tab.get("windows", [])
        if window.get("user_vars", {}).get(KISESH_UI_VAR) == "yes"
    ]


def _mapped_close_action() -> list[str]:
    """Read the exact action attached to Command-W in the shipped integration."""
    integration = (PROJECT / "kisesh/integration/kisesh.conf").read_text(encoding="utf-8")
    definition = next(
        line.split(maxsplit=2)[2]
        for line in integration.splitlines()
        if line.startswith("map cmd+w ")
    )
    return shlex.split(definition)


def _mapped_manager_close_action() -> list[str]:
    """Read the conditional action that dismisses a focused manager overlay."""
    integration = (PROJECT / "kisesh/integration/kisesh.conf").read_text(encoding="utf-8")
    definition = next(
        line.split(maxsplit=4)[4]
        for line in integration.splitlines()
        if line.startswith("map --when-focus-on var:kisesh_ui alt+s ")
    )
    return shlex.split(definition)


def _mapped_reload_action() -> list[str]:
    """Read the exact action attached to Kitty's macOS reload chord."""
    integration = (PROJECT / "kisesh/integration/kisesh.conf").read_text(encoding="utf-8")
    definition = next(
        line.split(maxsplit=2)[2]
        for line in integration.splitlines()
        if line.startswith("map ctrl+cmd+, ")
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

    def test_x_close_from_its_own_overlay_preserves_one_surviving_session_filter(self) -> None:
        """Close the overlay's host session without revealing another live group."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                created = subprocess.run(
                    [
                        str(Path(sys.executable).with_name("kisesh")),
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
                hidden = store.create("Hidden", "/tmp/hidden")
                child = ["/bin/sh", "-c", "while :; do sleep 1; done"]
                for session in (successor, hidden):
                    server.remote(
                        "launch",
                        "--type=tab",
                        f"--tab-title={session.manifest.name}",
                        f"--var={SESSION_ID_VAR}={session.manifest.id}",
                        f"--var={SESSION_SLUG_VAR}={session.manifest.slug}",
                        *child,
                    )
                initial = server.wait_for(lambda state: len(_tabs(state)) == 3)
                client = KittyClient(executable=server.kitty, socket=server.socket)
                closing_tab = _session_tabs(initial, closing.manifest.id)[0]
                live_closing = next(
                    tab for tab in client.tabs(initial) if tab.tab_id == closing_tab["id"]
                )
                client.activate_session(closing.manifest.id, live_closing)
                expected_initial_filter = (
                    f"var:{SESSION_ID_VAR}={closing.manifest.id} or "
                    f"not var:{SESSION_SCOPE_VAR}={live_closing.os_window_id}"
                )
                self.assertEqual(server.tab_filter(), expected_initial_filter)
                _focus_session(server, closing.manifest.id)

                server.remote(
                    "launch",
                    "--type=overlay",
                    "--copy-env",
                    "--env=KISESH_CALLER=overlay",
                    f"--var={KISESH_UI_VAR}=yes",
                    "--title=KiSesh close regression",
                    str(Path(sys.executable).with_name("kisesh")),
                    "--socket",
                    server.socket,
                    "close",
                    closing.manifest.id,
                )
                isolated = server.wait_for(
                    lambda state: (
                        not _session_tabs(state, closing.manifest.id)
                        and len(_session_tabs(state, successor.manifest.id)) == 1
                        and len(_session_tabs(state, hidden.manifest.id)) == 1
                        and any(
                            tab.get("is_active")
                            for tab in _session_tabs(state, successor.manifest.id)
                        )
                        and not _ui_windows(state)
                    ),
                    timeout=30,
                )
                scope = str(live_closing.os_window_id)
                remaining_windows = [
                    *_session_windows(isolated, successor.manifest.id),
                    *_session_windows(isolated, hidden.manifest.id),
                ]
                self.assertTrue(remaining_windows)
                self.assertTrue(
                    all(
                        window.get("user_vars", {}).get(SESSION_SCOPE_VAR) == scope
                        for window in remaining_windows
                    )
                )
                self.assertEqual(
                    server.tab_filter(),
                    f"var:{SESSION_ID_VAR}={successor.manifest.id} or "
                    f"not var:{SESSION_SCOPE_VAR}={scope}",
                )
                self.assertIsNotNone(store.read_context(closing.manifest.id))
            finally:
                server.stop()

    def test_manager_close_restores_split_layout_before_removing_overlay(self) -> None:
        """Reproduce Alt-S dismissal against a real, isolated two-pane Kitty tab."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                child = ["/bin/sh", "-c", "while :; do sleep 1; done"]
                server.remote("launch", "--type=window", "--title=Second", *child)
                split_state = server.wait_for(
                    lambda state: len(_tabs(state)[0].get("windows", [])) == 2
                )
                tab_id = _tabs(split_state)[0]["id"]
                server.remote("goto-layout", "--match", f"id:{tab_id}", "splits")
                server.wait_for(lambda state: _tabs(state)[0].get("layout") == "splits")

                server.remote(
                    "launch",
                    "--type=overlay",
                    f"--var={KISESH_UI_VAR}=yes",
                    "--title=KiSesh",
                    *child,
                )
                overlay_state = server.wait_for(lambda state: len(_ui_windows(state)) == 1)
                overlay_id = _ui_windows(overlay_state)[0]["id"]
                server.remote("goto-layout", "--match", f"id:{tab_id}", "stack")
                server.wait_for(lambda state: _tabs(state)[0].get("layout") == "stack")

                server.remote(
                    "action",
                    "--match",
                    f"id:{overlay_id}",
                    *_mapped_manager_close_action(),
                )
                restored = server.wait_for(
                    lambda state: (
                        not _ui_windows(state)
                        and _tabs(state)[0].get("layout") == "splits"
                        and len(_tabs(state)[0].get("windows", [])) == 2
                    )
                )

                self.assertEqual(_tabs(restored)[0].get("layout"), "splits")
                self.assertEqual(len(_tabs(restored)[0].get("windows", [])), 2)
            finally:
                server.stop()

    def test_command_w_action_closes_one_tab_then_guards_saves_and_promotes(self) -> None:
        """Run the complete multi-tab and final-tab lifecycle in hidden Kitty."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                created = subprocess.run(
                    [
                        str(Path(sys.executable).with_name("kisesh")),
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


@unittest.skipUnless(LIVE_TESTS_ENABLED, LIVE_TEST_REASON)
@unittest.skipUnless(shutil.which("kitty") and shutil.which("kitten"), "Kitty is required")
class LiveKittyFilterTests(unittest.TestCase):
    """Exercise a real session switch after applying a runtime font zoom."""

    def test_session_switch_preserves_nondefault_os_window_font_size(self) -> None:
        """Keep a runtime font size distinct from the isolated config default."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            server = IsolatedKitty(root)
            probe = root / "font_probe.py"
            probe.write_text(
                '"""Report the active Kitty OS window font size."""\n'
                "from kittens.tui.handler import result_handler\n"
                "from kitty.fast_data_types import os_window_font_size\n"
                "def main(args):\n"
                "    del args\n"
                "@result_handler(no_ui=True)\n"
                "def handle_result(args, answer, target_window_id, boss):\n"
                "    del args, answer, target_window_id\n"
                "    return str(os_window_font_size(boss.active_window.os_window_id))\n",
                encoding="utf-8",
            )
            try:
                server.start()
                server.remote(
                    "set-user-vars",
                    "--match",
                    "all",
                    f"{SESSION_ID_VAR}=session-id",
                )
                server.remote("set-font-size", "21")
                before = server.remote("kitten", str(probe)).stdout.strip()

                client = KittyClient(executable=server.kitty, socket=server.socket)
                client.activate_session("session-id", client.tabs()[0])

                after = server.remote("kitten", str(probe)).stdout.strip()
                self.assertEqual(before, "21.0")
                self.assertEqual(after, before)
                self.assertNotEqual(after, "13.0")
            finally:
                server.stop()

    def test_each_os_window_keeps_its_selected_session_after_another_opens(self) -> None:
        """Keep both native tab bars isolated while all four sessions remain live."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            left_session = "11111111-1111-4111-8111-111111111111"
            left_hidden_session = "22222222-2222-4222-8222-222222222222"
            right_session = "33333333-3333-4333-8333-333333333333"
            right_hidden_session = "44444444-4444-4444-8444-444444444444"
            try:
                server.start()
                initial = server.state()
                left_window_id = _tabs(initial)[0]["windows"][0]["id"]
                server.remote(
                    "set-tab-title",
                    "--match",
                    f"id:{left_window_id}",
                    "Left selected",
                )
                server.remote(
                    "set-user-vars",
                    "--match",
                    f"id:{left_window_id}",
                    f"{SESSION_ID_VAR}={left_session}",
                )
                child = ["/bin/sh", "-c", "while :; do sleep 1; done"]
                server.remote(
                    "launch",
                    "--match",
                    f"id:{left_window_id}",
                    "--type=tab",
                    "--tab-title=Left hidden",
                    f"--var={SESSION_ID_VAR}={left_hidden_session}",
                    *child,
                )
                left_state = server.wait_for(lambda current: len(_tabs(current)) == 2)
                client = KittyClient(executable=server.kitty, socket=server.socket)
                left = next(
                    tab
                    for tab in client.tabs(left_state)
                    if any(window["id"] == left_window_id for window in tab.windows)
                )
                left_hidden = next(
                    tab
                    for tab in client.tabs(left_state)
                    if tab.os_window_id == left.os_window_id and tab.tab_id != left.tab_id
                )
                server.remote(
                    "set-user-vars",
                    "--match",
                    f"id:{left_hidden.representative_window_id}",
                    f"{SESSION_ID_VAR}={left_hidden_session}",
                )
                client.activate_session(left_session, left)

                server.remote(
                    "launch",
                    "--type=os-window",
                    "--tab-title=Right selected",
                    f"--var={SESSION_ID_VAR}={right_session}",
                    *child,
                )
                two_windows = server.wait_for(lambda current: len(current) == 2)
                right_window = _session_windows(two_windows, right_session)[0]
                server.remote(
                    "launch",
                    "--match",
                    f"id:{right_window['id']}",
                    "--type=tab",
                    "--tab-title=Right hidden",
                    f"--var={SESSION_ID_VAR}={right_hidden_session}",
                    *child,
                )
                four_sessions = server.wait_for(lambda current: len(_tabs(current)) == 4)
                right = next(
                    tab for tab in client.tabs(four_sessions) if tab.session_id() == right_session
                )
                right_hidden = next(
                    tab
                    for tab in client.tabs(four_sessions)
                    if tab.os_window_id == right.os_window_id and tab.tab_id != right.tab_id
                )
                server.remote(
                    "set-user-vars",
                    "--match",
                    f"id:{right_hidden.representative_window_id}",
                    f"{SESSION_ID_VAR}={right_hidden_session}",
                )

                client.activate_session(right_session, right)

                expected_filter = (
                    f"(var:{SESSION_ID_VAR}={left_session} or "
                    f"not var:{SESSION_SCOPE_VAR}={left.os_window_id}) and "
                    f"(var:{SESSION_ID_VAR}={right_session} or "
                    f"not var:{SESSION_SCOPE_VAR}={right.os_window_id})"
                )
                visible = server.visible_session_ids()
                self.assertEqual(server.tab_filter(), expected_filter)
                self.assertEqual(visible[str(left.os_window_id)], [left_session])
                self.assertEqual(visible[str(right.os_window_id)], [right_session])
                self.assertEqual(len(_tabs(server.state())), 4)
            finally:
                server.stop()

    def test_config_reload_keeps_other_live_session_tabs_filtered(self) -> None:
        """Recover a lost filter while applying changed native options."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                server.remote(
                    "set-user-vars",
                    "--match",
                    "all",
                    f"{SESSION_ID_VAR}=focused",
                    f"{SESSION_SLUG_VAR}=focused",
                )
                server.remote(
                    "launch",
                    "--type=tab",
                    "--tab-title=Hidden session",
                    f"--var={SESSION_ID_VAR}=hidden",
                    f"--var={SESSION_SLUG_VAR}=hidden",
                    "/bin/sh",
                    "-c",
                    "while :; do sleep 1; done",
                )
                state = server.wait_for(lambda current: len(_tabs(current)) == 2)
                client = KittyClient(executable=server.kitty, socket=server.socket)
                focused = next(tab for tab in client.tabs(state) if tab.session_id() == "focused")
                client.activate_session("focused", focused)
                focused_window_id = _focus_session(server, "focused")
                expected_filter = (
                    f"var:{SESSION_ID_VAR}=focused or "
                    f"not var:{SESSION_SCOPE_VAR}={focused.os_window_id}"
                )
                self.assertEqual(server.tab_filter(), expected_filter)
                server.remote(
                    "set-user-vars",
                    "--match",
                    f"id:{focused_window_id}",
                    SESSION_SCOPE_VAR,
                )
                server.wait_for(
                    lambda current: all(
                        SESSION_SCOPE_VAR not in window.get("user_vars", {})
                        for window in _session_windows(current, "focused")
                    )
                )
                server.remote(
                    "kitten",
                    str(server.home / ".local/lib/kisesh/integration/actions.py"),
                    "session-filter",
                    "all",
                )
                self.assertCountEqual(
                    server.visible_session_ids()[str(focused.os_window_id)],
                    ["focused", "hidden"],
                )
                server.config.write_text(
                    server.config.read_text(encoding="utf-8").replace(
                        "font_size 13",
                        "font_size 17",
                    ),
                    encoding="utf-8",
                )

                server.remote(
                    "action",
                    "--match",
                    f"id:{focused_window_id}",
                    *_mapped_reload_action(),
                )

                reloaded = server.wait_for(
                    lambda current: (
                        len(_session_tabs(current, "focused")) == 1
                        and len(_session_tabs(current, "hidden")) == 1
                    )
                )
                self.assertEqual(server.configured_font_size(), 17.0)
                self.assertEqual(server.tab_filter(), expected_filter)
                self.assertEqual(
                    server.visible_session_ids()[str(focused.os_window_id)],
                    ["focused"],
                )
                self.assertEqual(len(_tabs(reloaded)), 2)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
