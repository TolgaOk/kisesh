"""Opt-in final layout persistence through Kitty's native application quit."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kisesh.kitty_client import KittyClient
from kisesh.model import SESSION_ID_VAR, JsonValue
from kisesh.service import KiSeshService
from kisesh.store import SessionStore, StoredSession
from tests.test_live_kitty_close import (
    LIVE_TEST_REASON,
    LIVE_TESTS_ENABLED,
    IsolatedKitty,
    _launch_manager,
    _session_tabs,
    _session_windows,
    _tabs,
)


def _split_shape(snapshot: str) -> JsonValue:
    """Normalize native window IDs while preserving split axes and exact proportions."""
    line = next(line for line in snapshot.splitlines() if line.startswith("set_layout_state "))
    layout = json.loads(line.removeprefix("set_layout_state "))
    order = {
        group["id"]: index for index, group in enumerate(layout["all_windows"]["window_groups"])
    }

    def normalize(pair: JsonValue) -> JsonValue:
        """Translate tree leaves to their pane positions without changing layout parameters."""
        if isinstance(pair, int):
            return order[pair]
        assert isinstance(pair, dict)
        return {
            key: normalize(value) if key in {"one", "two"} else value for key, value in pair.items()
        }

    return normalize(layout["pairs"])


@unittest.skipUnless(LIVE_TESTS_ENABLED, LIVE_TEST_REASON)
@unittest.skipUnless(shutil.which("kitty") and shutil.which("kitten"), "Kitty is required")
class LiveKittyQuitTests(unittest.TestCase):
    """Exercise final saves against only disposable hidden Kitty processes."""

    def assert_reopens_split_session(self, original: IsolatedKitty, stored: StoredSession) -> None:
        """Restore saved panes into a fresh Kitty and compare rendered geometry and history."""
        reopened = IsolatedKitty(original.root / "reopened")
        reopened.environment["XDG_DATA_HOME"] = str(original.data.parent)
        try:
            reopened.start()
            result = subprocess.run(
                [
                    str(Path(sys.executable).with_name("kisesh")),
                    "--socket",
                    reopened.socket,
                    "open",
                    stored.manifest.id,
                    "--unowned-tabs=discard",
                ],
                env=reopened.environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = reopened.wait_for(
                lambda current: len(_session_windows(current, stored.manifest.id)) == 3
            )
            self.assertEqual(len(_session_tabs(state, stored.manifest.id)), 1)
            self.assertEqual(_session_tabs(state, stored.manifest.id)[0]["layout"], "splits")
            captured = reopened.root / "restored.kitty"
            client = KittyClient(executable=reopened.kitty, socket=reopened.socket)
            client.capture_session(stored.manifest.id, captured)
            self.assertEqual(
                _split_shape(captured.read_text()), _split_shape(stored.snapshot_path.read_text())
            )
            reopened.wait_for(
                lambda current: any(
                    "retained output" in client.terminal_history(window["id"])
                    for window in _session_windows(current, stored.manifest.id)
                )
            )
        finally:
            reopened.stop()

    def test_native_quit_captures_layout_without_waiting_for_autosave(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                client = KittyClient(executable=server.kitty, socket=server.socket)
                store = SessionStore(server.data)
                service = KiSeshService(store, client)
                stored = service.create_from_active("Work")
                tab = _tabs(server.state())[0]
                server.remote("goto-layout", "--match", f"id:{tab['id']}", "tall")
                self.assertNotIn("layout tall", stored.snapshot_path.read_text())

                server.remote("action", "quit")

                assert server.process is not None
                self.assertEqual(server.process.wait(timeout=30), 0)
                saved = store.get(stored.manifest.id)
                self.assertIn("layout tall", saved.snapshot_path.read_text())
                context = store.read_context(saved.manifest.id)
                assert context is not None
                self.assertEqual(context["snapshot_revision"], saved.manifest.revision)
            finally:
                server.stop()

    def test_quit_with_manager_saves_underlying_split_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                client = KittyClient(executable=server.kitty, socket=server.socket)
                store = SessionStore(server.data)
                service = KiSeshService(store, client)
                stored = service.create_from_active("Work")
                server.remote("goto-layout", "splits")
                for location in ("vsplit", "hsplit"):
                    server.remote(
                        "launch",
                        f"--location={location}",
                        f"--var={SESSION_ID_VAR}={stored.manifest.id}",
                        "/bin/sh",
                        "-c",
                        "printf 'retained output\\n'; while :; do sleep 1; done",
                    )
                server.remote("resize-window", "--axis=horizontal", "--increment=5")
                expected = server.root / "expected.kitty"
                client.capture_session(stored.manifest.id, expected)
                expected_layout = next(
                    line
                    for line in expected.read_text().splitlines()
                    if line.startswith("set_layout_state ")
                )
                _launch_manager(server)

                server.remote("action", "quit")

                assert server.process is not None
                self.assertEqual(server.process.wait(timeout=30), 0)
                snapshot = store.get(stored.manifest.id).snapshot_path.read_text()
                self.assertIn("layout splits", snapshot)
                self.assertIn(expected_layout, snapshot)
                self.assertEqual(
                    sum(line.startswith("launch ") for line in snapshot.splitlines()), 3
                )
                context = store.read_context(stored.manifest.id)
                assert context is not None
                self.assertIn("retained output", str(context))
                self.assert_reopens_split_session(server, stored)
            finally:
                server.stop()

    def test_quit_captures_hidden_sessions_and_another_os_window(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                client = KittyClient(executable=server.kitty, socket=server.socket)
                store = SessionStore(server.data)
                service = KiSeshService(store, client)
                active = service.create_from_active("Active")
                hidden = store.create("Hidden", temporary)
                other = store.create("Other window", temporary)
                for kind, session in (("tab", active), ("tab", hidden), ("os-window", other)):
                    server.remote(
                        "launch",
                        f"--type={kind}",
                        "--os-window-state=minimized",
                        f"--var={SESSION_ID_VAR}={session.manifest.id}",
                        "/bin/sh",
                        "-c",
                        "while :; do sleep 1; done",
                    )
                for session in (active, hidden, other):
                    service.save(session.manifest.id)
                initial = server.state()
                self.assertEqual(len(initial), 2)
                self.assertEqual(len(_session_tabs(initial, active.manifest.id)), 2)
                client.activate_session(
                    active.manifest.id, client.tabs_for_session(active.manifest.id)[0]
                )
                self.assertNotIn(hidden.manifest.id, str(server.visible_session_ids()))
                for session, layout in ((active, "tall"), (hidden, "grid"), (other, "fat")):
                    server.remote(
                        "goto-layout",
                        "--match",
                        f"var:{SESSION_ID_VAR}={session.manifest.id}",
                        layout,
                    )

                server.remote("action", "quit")

                assert server.process is not None
                self.assertEqual(server.process.wait(timeout=30), 0)
                for session, layout, tabs in (
                    (active, "tall", 2),
                    (hidden, "grid", 1),
                    (other, "fat", 1),
                ):
                    saved = store.get(session.manifest.id)
                    self.assertEqual(saved.manifest.summary.tab_count, tabs)
                    self.assertEqual(
                        saved.snapshot_path.read_text().count(f"layout {layout}\n"), tabs
                    )
                    context = store.read_context(saved.manifest.id)
                    assert context is not None
                    self.assertEqual(len(context["tabs"]), tabs)
            finally:
                server.stop()

    def test_failed_capture_keeps_kitty_open_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                store = SessionStore(server.data)
                client = KittyClient(executable=server.kitty, socket=server.socket)
                stored = KiSeshService(store, client).create_from_active("Work")
                server.remote("goto-layout", "tall")
                locks = server.data / ".capture-locks"
                parked = server.data / ".capture-locks.backup"
                locks.rename(parked)
                locks.touch()

                server.remote("action", "quit")

                failed = server.wait_for(
                    lambda state: (
                        len(_tabs(state)[0].get("windows", [])) == 2
                        and "KiSesh quit cancelled"
                        in server.remote(
                            "get-text", "--match", f"id:{_tabs(state)[0]['windows'][-1]['id']}"
                        ).stdout
                    )
                )
                assert server.process is not None
                self.assertIsNone(server.process.poll())
                error_window = _tabs(failed)[0]["windows"][-1]
                self.assertIn(
                    "KiSesh quit cancelled",
                    server.remote("get-text", "--match", f"id:{error_window['id']}").stdout,
                )
                self.assertEqual(len(_session_tabs(failed, stored.manifest.id)), 1)
                locks.unlink()
                parked.rename(locks)
                server.remote("close-window", "--match", f"id:{error_window['id']}")
                server.wait_for(lambda state: len(_tabs(state)[0].get("windows", [])) == 1)
                server.remote("action", "quit")
                self.assertEqual(server.process.wait(timeout=30), 0)
                self.assertIn("layout tall", stored.snapshot_path.read_text())
            finally:
                server.stop()

    def test_native_confirmation_can_cancel_then_accept_without_saving_its_prompt(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            server = IsolatedKitty(Path(temporary))
            try:
                server.start()
                store = SessionStore(server.data)
                client = KittyClient(executable=server.kitty, socket=server.socket)
                stored = KiSeshService(store, client).create_from_active("Work")
                server.remote("load-config", "--override", "confirm_os_window_close=1")
                server.remote("goto-layout", "tall")
                for answer in ("n", "y"):
                    server.remote("action", "quit")
                    state = server.wait_for(
                        lambda current: len(_tabs(current)[0].get("windows", [])) == 2
                    )
                    prompt = _tabs(state)[0]["windows"][-1]
                    server.remote("send-text", "--match", f"id:{prompt['id']}", answer)
                    assert server.process is not None
                    if answer == "n":
                        server.wait_for(
                            lambda current: len(_tabs(current)[0].get("windows", [])) == 1
                        )
                        self.assertIsNone(server.process.poll())
                assert server.process is not None
                self.assertEqual(server.process.wait(timeout=30), 0)
                saved = store.get(stored.manifest.id)
                self.assertEqual(saved.manifest.summary.pane_count, 1)
                self.assertIn("layout tall", saved.snapshot_path.read_text())
                context = store.read_context(stored.manifest.id)
                self.assertNotIn("Are you sure you want to quit kitty", str(context))
            finally:
                server.stop()
