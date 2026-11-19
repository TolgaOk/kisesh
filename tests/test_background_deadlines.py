"""Bound background lifecycle operations without connecting to any Kitty instance."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kisesh.deadline import OperationDeadlineExceeded, operation_deadline
from kisesh.store import SessionStore

PROJECT = Path(__file__).resolve().parents[1]


class BackgroundDeadlineTests(unittest.TestCase):
    def test_deadline_cannot_be_swallowed_by_recoverable_exception_handlers(self) -> None:
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(OperationDeadlineExceeded), operation_deadline(0.01):
            try:
                time.sleep(0.1)
            except Exception:
                self.fail("deadline was swallowed as a recoverable operation error")
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_nested_deadline_restores_the_enclosing_timer(self) -> None:
        with operation_deadline(10):
            before = signal.getitimer(signal.ITIMER_REAL)[0]
            with operation_deadline(20):
                self.assertLessEqual(signal.getitimer(signal.ITIMER_REAL)[0], before)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        with self.assertRaises(ValueError), operation_deadline(0):
            self.fail("invalid deadline accepted")

    def run_worker(self, root: Path, program: str, payload: str = "") -> tuple[int, str, str]:
        child = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=PROJECT,
            env={
                "PATH": os.defpath,
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "PYTHONPATH": str(PROJECT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            try:
                output, errors = child.communicate(payload, timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("background operation did not enforce its deadline")
            assert child.returncode is not None
            return child.returncode, output, errors
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def test_autosave_deadline_interrupts_a_capture_lock_wait(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kisesh-deadline-test-") as temporary:
            root = Path(temporary)
            store = SessionStore(root / "sessions")
            stored = store.create("Test", str(root))
            args = [
                "--data-dir",
                str(store.root),
                "--kitty",
                sys.executable,
                "--socket",
                f"unix:{root / 'never-contacted.sock'}",
                "autosave",
                stored.manifest.id,
            ]
            program = (
                "import kisesh.cli as cli; cli.BACKGROUND_OPERATION_TIMEOUT_SECONDS = 0.1; "
                f"raise SystemExit(cli.main({args!r}))"
            )
            with store.capturing(stored.manifest.id):
                status, output, errors = self.run_worker(root, program)
            self.assertEqual(status, 1)
            self.assertEqual(output, "")
            self.assertIn("deadline", errors)
            with store.capturing(stored.manifest.id):
                self.assertEqual(store.get(stored.manifest.id).manifest.revision, 0)

    def test_close_capture_deadline_interrupts_a_global_store_lock_wait(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kisesh-deadline-test-") as temporary:
            root = Path(temporary)
            store = SessionStore(root / "sessions")
            stored = store.create("Test", str(root))
            args = [
                "--data-dir",
                str(store.root),
                "--kitty",
                sys.executable,
                "--socket",
                f"unix:{root / 'never-contacted.sock'}",
                "autosave",
                stored.manifest.id,
                "--payload-stdin",
            ]
            payload = json.dumps(
                {
                    "closing_pane": {
                        "tab_index": 0,
                        "pane_index": 0,
                        "window": {"id": 1},
                        "terminal_history": "",
                        "last_command_output": "",
                        "command_events": [],
                    }
                }
            )
            program = (
                "import kisesh.cli as cli; cli.BACKGROUND_OPERATION_TIMEOUT_SECONDS = 0.1; "
                f"raise SystemExit(cli.main({args!r}))"
            )
            with store.locked():
                status, output, errors = self.run_worker(root, program, payload)
            self.assertEqual(status, 1)
            self.assertEqual(output, "")
            self.assertIn("deadline", errors)

    def test_save_and_close_has_the_same_whole_operation_deadline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kisesh-deadline-test-") as temporary:
            program = """import time
from types import SimpleNamespace
import kisesh.cli as cli
cli.BACKGROUND_OPERATION_TIMEOUT_SECONDS = 0.1
cli._service = lambda config: SimpleNamespace(save_and_close=lambda *args: time.sleep(30))
raise SystemExit(cli.main(['close', 'test-session']))
"""
            status, output, errors = self.run_worker(Path(temporary), program)
            self.assertEqual(status, 1)
            self.assertEqual(output, "")
            self.assertIn("deadline", errors)
