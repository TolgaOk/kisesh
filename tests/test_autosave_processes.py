"""Isolated regressions for autosave concurrency and child-process ownership."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import unittest
from unittest import mock

from kisesh import watcher
from tests.test_watcher import FakeTimer


class AutosaveConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("_timers", "_timer_generations", "_pending_commands"):
            self.enterContext(mock.patch.object(watcher, name, {}))
        self.enterContext(mock.patch.object(watcher, "_quit_state", watcher.QuitSaveState()))
        self.enterContext(mock.patch("kisesh.watcher.threading.Timer", FakeTimer))
        FakeTimer.instances.clear()

    def test_repeated_events_coalesce_while_a_session_is_saving(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        process = mock.Mock()

        def wait(*, timeout: float) -> int:
            del timeout
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release its fake child")
            return 0

        process.wait.side_effect = wait
        with mock.patch.object(watcher, "_launch_autosave", return_value=process) as launch:
            watcher._timer_generations["one"] = 1
            worker = threading.Thread(target=watcher._run_autosave, args=("one", {}, 1))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                for generation in range(2, 7):
                    watcher._timer_generations["one"] = generation
                    watcher._run_autosave("one", {"LATEST": str(generation)}, generation)
                self.assertEqual(launch.call_count, 1)
            finally:
                release.set()
                worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(FakeTimer.instances), 1)
            FakeTimer.instances[-1].fire()
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(launch.call_args.args[1], {"LATEST": "6"})

    def test_global_limit_defers_another_session_until_capacity_is_available(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        process = mock.Mock()

        def wait(*, timeout: float) -> int:
            del timeout
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release its fake child")
            return 0

        process.wait.side_effect = wait
        with (
            mock.patch.object(watcher, "MAX_CONCURRENT_AUTOSAVES", 1, create=True),
            mock.patch.object(watcher, "_launch_autosave", return_value=process) as launch,
        ):
            watcher._timer_generations.update(one=1, two=1)
            worker = threading.Thread(target=watcher._run_autosave, args=("one", {}, 1))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                watcher._run_autosave("two", {}, 1)
                self.assertEqual(launch.call_count, 1)
            finally:
                release.set()
                worker.join(4)
            self.assertFalse(worker.is_alive())
            FakeTimer.instances[-1].fire()
            self.assertEqual(launch.call_args.args[0], "two")

    def test_spawn_failure_releases_capacity_for_a_retry(self) -> None:
        watcher._timer_generations["one"] = 1
        with mock.patch.object(watcher, "_launch_autosave", return_value=None) as launch:
            watcher._run_autosave("one", {}, 1)
            watcher._timer_generations["one"] = 2
            watcher._run_autosave("one", {}, 2)
        self.assertEqual(launch.call_count, 2)

    def test_timeout_kills_and_reaps_its_owned_child_and_retains_events(self) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(30)",
            ],
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
        assert child.stdout is not None
        self.assertTrue(select.select([child.stdout], [], [], 3)[0])
        self.assertEqual(child.stdout.readline(), "ready\n")
        watcher._timer_generations["one"] = 1
        watcher._pending_commands["one"] = [{"command": "must survive"}]
        with (
            mock.patch.object(watcher, "AUTOSAVE_COMPLETION_TIMEOUT_SECONDS", 0.05),
            mock.patch.object(watcher, "AUTOSAVE_TERMINATION_GRACE_SECONDS", 0.05, create=True),
            mock.patch.object(watcher, "_launch_autosave", return_value=child),
        ):
            watcher._run_autosave("one", {}, 1)
        self.assertIsNotNone(child.returncode, "timed-out child was abandoned")
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(watcher._pending_commands["one"], [{"command": "must survive"}])
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
