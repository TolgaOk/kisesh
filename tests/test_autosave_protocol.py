"""Prove durable acknowledgement survives a competing native child reaper."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from typing import cast
from unittest import mock

from kisesh import watcher
from kisesh.cli import AutosaveSession, _run_maintenance
from kisesh.service import KiSeshService

ACKNOWLEDGEMENT = "kisesh:autosave:committed\n"


class AutosaveAcknowledgementTests(unittest.TestCase):
    def test_reaped_failure_does_not_erase_pending_events(self) -> None:
        self._externally_reaped_save("raise SystemExit(7)", expected_saved=False)

    def test_reaped_success_requires_a_durable_receipt(self) -> None:
        self._externally_reaped_save(f"print({ACKNOWLEDGEMENT.strip()!r})", expected_saved=True)

    def _externally_reaped_save(self, program: str, *, expected_saved: bool) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", program],
            env={"PATH": os.defpath},
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        def cleanup() -> None:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()

        self.addCleanup(cleanup)
        import time

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child.pid, os.WNOHANG)
            if pid:
                break
            time.sleep(0.01)
        else:
            self.fail("disposable child did not exit")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0 if expected_saved else 7)
        pending = {"one": [{"window_id": 1, "command": "must survive failure"}]}
        with (
            mock.patch.object(watcher, "_pending_commands", pending),
            mock.patch.object(watcher, "_pending_closes", {}),
            mock.patch.object(watcher, "_running_saves", set()),
            mock.patch.object(watcher, "_deferred_saves", {}),
            mock.patch.object(watcher, "_timer_generations", {"one": 1}),
            mock.patch.object(watcher, "_quit_state", watcher.QuitSaveState()),
            mock.patch.object(watcher, "_launch_autosave", return_value=child),
        ):
            watcher._run_autosave("one", {}, 1)
        self.assertEqual("one" not in pending, expected_saved)
        self.assertEqual(child.returncode, 0, "model the native reaper's lost status")

    def test_cli_receipt_is_emitted_only_after_successful_persistence(self) -> None:
        service = mock.Mock()
        output = io.StringIO()

        def saved(*args: object) -> None:
            del args
            self.assertEqual(output.getvalue(), "")

        service.save.side_effect = saved
        with redirect_stdout(output):
            _run_maintenance(AutosaveSession("one"), cast(KiSeshService, service))
        self.assertEqual(output.getvalue(), ACKNOWLEDGEMENT)
        output = io.StringIO()
        service.save.side_effect = OSError("disk full")
        with redirect_stdout(output), self.assertRaises(OSError):
            _run_maintenance(AutosaveSession("one"), cast(KiSeshService, service))
        self.assertEqual(output.getvalue(), "")
