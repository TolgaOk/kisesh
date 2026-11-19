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
from tests.test_watcher import Boss, FakeTimer, Window


class AutosaveConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in (
            "_timers",
            "_timer_generations",
            "_pending_commands",
            "_deferred_saves",
            "_pending_closes",
        ):
            self.enterContext(mock.patch.object(watcher, name, {}))
        for name in ("_running_saves", "_full_save_requested"):
            self.enterContext(mock.patch.object(watcher, name, set()))
        self.enterContext(mock.patch.object(watcher, "_window_environment", return_value={}))
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
        process.communicate.return_value = (watcher.AUTOSAVE_ACKNOWLEDGEMENT, "")
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
        process.communicate.return_value = (watcher.AUTOSAVE_ACKNOWLEDGEMENT, "")
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
            env={"PATH": os.defpath},
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

    def test_close_queues_capture_without_spawning_or_discarding_events(self) -> None:
        window = Window(history="valuable scrollback")
        watcher._pending_commands["session-id"] = [{"window_id": 2, "command": "unsaved"}]
        process = mock.Mock()
        process.wait.return_value = 0
        process.communicate.return_value = (watcher.AUTOSAVE_ACKNOWLEDGEMENT, "")
        with mock.patch.object(watcher, "_launch_autosave", return_value=process) as launch:
            watcher.on_close(Boss([[window]]), window, {})
            launch.assert_not_called()
            self.assertEqual(len(watcher._pending_commands["session-id"]), 1)
            FakeTimer.instances[-1].fire()
            self.assertEqual(launch.call_count, 1)
            self.assertNotIn("session-id", watcher._pending_commands)

    def test_failed_close_keeps_capture_and_only_acknowledges_its_pane_on_retry(self) -> None:
        window = Window(history="before destruction")
        own = {"window_id": 2, "command": "own"}
        sibling = {"window_id": 3, "command": "sibling"}
        watcher._pending_commands["session-id"] = [own, sibling]
        process = mock.Mock()
        process.wait.return_value = 1
        process.communicate.return_value = (watcher.AUTOSAVE_ACKNOWLEDGEMENT, "")
        with mock.patch.object(watcher, "_launch_autosave", return_value=process) as launch:
            watcher.on_close(Boss([[window]]), window, {})
            window.history = "screen destroyed"
            FakeTimer.instances[-1].fire()
            self.assertEqual(watcher._pending_commands["session-id"], [own, sibling])
            self.assertEqual(len(watcher._pending_closes["session-id"]), 1)
            process.wait.return_value = 0
            watcher._schedule(window, capture_live=False)
            FakeTimer.instances[-1].fire()
            self.assertEqual(watcher._pending_commands["session-id"], [sibling])
            self.assertNotIn("session-id", watcher._pending_closes)
            capture = launch.call_args.args[2]["closing_pane"]
            self.assertEqual(capture["terminal_history"], "before destruction")

    def test_close_queue_and_text_are_bounded_without_spawning_on_overload(self) -> None:
        window = Window(history="x" * (watcher.CLOSE_TEXT_LIMIT + 1))
        with (
            mock.patch.object(watcher, "MAX_PENDING_CLOSE_CAPTURES", 1),
            mock.patch.object(watcher, "_launch_autosave") as launch,
            mock.patch("sys.stderr") as errors,
        ):
            watcher.on_close(Boss([[window]]), window, {})
            watcher.on_close(Boss([[window]]), window, {})
            self.assertEqual(launch.call_count, 0)
            captures = watcher._pending_closes["session-id"]
            self.assertEqual(len(captures), 1)
            self.assertEqual(len(str(captures[0]["terminal_history"])), watcher.CLOSE_TEXT_LIMIT)
            self.assertTrue(errors.write.called)

    def test_blocked_stdin_is_covered_by_the_process_deadline(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            env={"PATH": os.defpath},
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        def cleanup() -> None:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
            if child.stdin is not None:
                child.stdin.close()

        self.addCleanup(cleanup)
        with (
            mock.patch.object(watcher, "AUTOSAVE_COMPLETION_TIMEOUT_SECONDS", 0.05),
            mock.patch.object(watcher, "AUTOSAVE_TERMINATION_GRACE_SECONDS", 0.05),
        ):
            self.assertFalse(watcher._complete_autosave(child, {"history": "x" * (2 * 1024**2)}))
        self.assertIsNotNone(child.returncode)
        self.assertTrue(child.stdin is not None and child.stdin.closed)
