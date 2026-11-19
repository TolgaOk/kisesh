"""Cross-layer review regressions using real storage and no native Kitty connections."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from kisesh import watcher
from kisesh.model import ClosingPaneCapture, KittyWindow
from kisesh.service import KiSeshService
from kisesh.store import SessionStore
from tests.fakes import FakeKitty
from tests.test_quit_watcher import QuitBoss
from tests.test_watcher import FakeTimer, Window


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="kisesh-review-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        launcher = self.root / "bin" / "kisesh"
        launcher.parent.mkdir()
        launcher.touch()
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.root),
                    "KISESH_INSTALL_ROOT": str(self.root),
                },
                clear=True,
            )
        )
        for name in (
            "_timers",
            "_timer_generations",
            "_pending_commands",
            "_deferred_saves",
            "_pending_closes",
            "_close_environments",
        ):
            self.enterContext(mock.patch.object(watcher, name, {}))
        for name in ("_running_saves", "_full_save_requested"):
            self.enterContext(mock.patch.object(watcher, name, set()))
        self.enterContext(mock.patch.object(watcher, "_quit_state", watcher.QuitSaveState()))
        self.enterContext(mock.patch("kisesh.watcher.threading.Timer", FakeTimer))
        FakeTimer.instances.clear()
        self.store = SessionStore(self.root / "data")
        self.kitty = FakeKitty()
        self.service = KiSeshService(self.store, self.kitty, resume_resolver=lambda *_: {})
        self.stored = self.service.create_from_active("Review")
        self.sid = self.stored.manifest.id
        self.old = Window(11, session_id=self.sid, history="surviving pane")
        self.new = Window(22, session_id=self.sid, history="short-lived pane")
        self.boss = QuitBoss([self.old, self.new])
        self.addCleanup(self.boss.close_job_resources)
        self.boss.tabs = [[self.old, self.new]]
        self.enterContext(
            mock.patch.dict("sys.modules", {"kitty.fast_data_types": self.boss.native})
        )
        self.payloads: list[dict[str, object]] = []
        self.enterContext(
            mock.patch.object(watcher, "_launch_autosave", side_effect=self.transport)
        )

    def transport(
        self, session_id: str, environment: dict[str, str], payload: dict[str, object]
    ) -> mock.Mock:
        del environment, payload
        process = mock.Mock()

        def communicate(*, input: str, timeout: float) -> tuple[str, str]:
            del timeout
            decoded = json.loads(input)
            self.payloads.append(decoded)
            try:
                if "closing_pane" in decoded:
                    self.service.save_closing_pane(session_id, decoded["closing_pane"])
                else:
                    self.service.save(session_id, decoded["command_events"])
            except (OSError, ValueError) as error:
                process.wait.return_value = 1
                return "", str(error)
            process.wait.return_value = 0
            return watcher.AUTOSAVE_ACKNOWLEDGEMENT, ""

        process.communicate.side_effect = communicate
        return process

    def queue_unknown_close(self) -> ClosingPaneCapture:
        watcher._pending_commands[self.sid] = [
            {
                "window_id": 22,
                "command": "short-lived command",
                "completed_at": "2026-08-04T11:30:00Z",
            }
        ]
        watcher.on_close(self.boss, self.new, {})
        self.boss.tabs = [[self.old]]
        return cast(ClosingPaneCapture, watcher._pending_closes[self.sid][0])

    def test_unknown_close_is_durable_and_does_not_block_the_next_live_save(self) -> None:
        capture = self.queue_unknown_close()
        self.kitty.terminal_histories[11] = "new surviving output"
        watcher.on_title_change(self.boss, self.old, {"title": "material change"})
        FakeTimer.instances[-1].fire()
        self.assertNotIn(self.sid, watcher._pending_closes, "unmergeable capture poisoned queue")
        FakeTimer.instances[-1].fire()
        self.assertEqual(["closing_pane" in item for item in self.payloads], [True, False])
        context = self.store.read_context(self.sid)
        assert context is not None
        self.assertEqual(context["tabs"][0]["panes"][0]["terminal_history"], "new surviving output")
        recovered = list((self.store.root / "recovered-panes" / self.sid).glob("*.json"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(json.loads(recovered[0].read_text())["capture"], capture)
        self.service.save_closing_pane(self.sid, capture)
        self.assertEqual(len(list(recovered[0].parent.glob("*.json"))), 1)
        self.assertEqual(recovered[0].stat().st_mode & 0o777, 0o600)

    def test_stale_position_never_overwrites_a_different_surviving_pane(self) -> None:
        capture = self.queue_unknown_close()
        capture["pane_index"] = 0
        before = self.store.read_context(self.sid)
        self.service.save_closing_pane(self.sid, capture)
        self.assertEqual(self.store.read_context(self.sid), before)
        self.assertEqual(len(list((self.store.root / "recovered-panes").rglob("*.json"))), 1)

    def test_recovery_write_failure_retains_queue_and_events_until_retry(self) -> None:
        self.queue_unknown_close()
        events = list(watcher._pending_commands[self.sid])
        with mock.patch("kisesh.store.atomic_write_text", side_effect=OSError("disk full")):
            FakeTimer.instances[-1].fire()
        self.assertIn(self.sid, watcher._pending_closes)
        self.assertEqual(watcher._pending_commands[self.sid], events)
        watcher._schedule(self.old, capture_live=False)
        FakeTimer.instances[-1].fire()
        self.assertNotIn(self.sid, watcher._pending_closes)
        self.assertNotIn(self.sid, watcher._pending_commands)

    def fire_drain(self) -> None:
        timer = watcher._quit_state.drain_timer
        self.assertIsNotNone(timer, "quit did not schedule a nonblocking drain continuation")
        assert timer is not None
        self.boss.deadlines[timer - 1](timer)

    def finish_native_save(self, index: int) -> None:
        payload = self.boss.jobs[index].payload
        if "closing_pane" in payload:
            self.service.save_closing_pane(
                self.sid, cast(ClosingPaneCapture, payload["closing_pane"])
            )
        else:
            self.service.save(self.sid)
        self.boss.jobs[index].callback(0, None)

    def test_quit_drains_an_actual_ordinary_writer_before_the_final_live_save(self) -> None:
        self.kitty.tab.windows.append(cast(KittyWindow, dict(self.new.as_dict())))
        self.kitty.capture_session_text = "new_tab Review\nlaunch\nlaunch\n"
        self.service.save(self.sid)
        watcher.on_close(self.boss, self.old, {})
        self.boss.tabs = [[self.new]]
        self.kitty.tab.windows = [cast(KittyWindow, dict(self.new.as_dict()))]
        self.kitty.capture_session_text = "new_tab Review\nlaunch\n"
        self.kitty.terminal_histories[22] = "surviving B history"
        entered = threading.Event()
        release = threading.Event()
        complete = watcher._complete_autosave

        def blocked_complete(process: mock.Mock, payload: dict[str, object]) -> bool:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release ordinary writer")
            return complete(process, payload)

        with mock.patch.object(watcher, "_complete_autosave", side_effect=blocked_complete):
            worker = threading.Thread(target=FakeTimer.instances[-1].fire)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                watcher.on_quit(self.boss, self.new, {"confirmed": True})
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(self.boss.jobs, [], "quit duplicated an in-flight close writer")
                timer_count = len(self.boss.deadlines)
                watcher._save_next_on_quit(self.boss)
                self.assertEqual(len(self.boss.deadlines), timer_count)
                self.fire_drain()
                self.assertEqual(self.boss.jobs, [])
            finally:
                release.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertNotIn(self.sid, watcher._running_saves)
        self.assertNotIn(self.sid, watcher._pending_closes)
        self.fire_drain()
        self.assertEqual(len(self.boss.jobs), 1)
        self.assertIn("command_events", self.boss.jobs[0].payload)
        self.finish_native_save(0)
        self.assertEqual(self.boss.continued, 1)
        context = self.store.read_context(self.sid)
        assert context is not None
        self.assertEqual(context["tabs"][0]["panes"][0]["window_id"], 22)
        self.assertEqual(context["tabs"][0]["panes"][0]["terminal_history"], "surviving B history")

    def test_quit_retries_a_failed_ordinary_close_only_after_its_slot_is_released(self) -> None:
        self.queue_unknown_close()
        watcher._running_saves.add(self.sid)
        watcher.on_quit(self.boss, self.old, {"confirmed": True})
        self.assertEqual(self.boss.jobs, [])
        watcher._finish_autosave(self.sid)
        self.fire_drain()
        self.assertEqual(len(self.boss.jobs), 1)
        self.assertIn("closing_pane", self.boss.jobs[0].payload)
        self.finish_native_save(0)
        self.assertEqual(len(self.boss.jobs), 2)
        self.finish_native_save(1)
        self.assertEqual(self.boss.continued, 1)

    def test_drain_timeout_does_not_release_an_ordinary_slot_or_launch_replacements(self) -> None:
        self.boss.tabs = [[self.old]]
        watcher._running_saves.add(self.sid)
        watcher.on_quit(self.boss, self.old, {"confirmed": True})
        self.assertEqual(self.boss.jobs, [])
        old_timer = watcher._quit_state.drain_timer
        assert old_timer is not None
        self.boss.deadlines[0](1)
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertIn(self.sid, watcher._running_saves)
        self.boss.deadlines[old_timer - 1](old_timer)
        self.assertEqual(self.boss.jobs, [])
        watcher.on_quit(self.boss, self.old, {"confirmed": True})
        self.boss.deadlines[old_timer - 1](old_timer)
        self.assertEqual(self.boss.jobs, [])
        watcher._finish_autosave(self.sid)
        self.fire_drain()
        self.assertEqual(len(self.boss.jobs), 1)
        self.finish_native_save(0)
        self.assertEqual(self.boss.continued, 1)

    def test_drain_callback_cannot_duplicate_an_already_launched_native_worker(self) -> None:
        self.boss.tabs = [[self.old]]
        watcher._running_saves.add(self.sid)
        watcher.on_quit(self.boss, self.old, {"confirmed": True})
        watcher._finish_autosave(self.sid)
        watcher._save_next_on_quit(self.boss)
        self.assertEqual(len(self.boss.jobs), 1)
        self.fire_drain()
        self.assertEqual(len(self.boss.jobs), 1)
        self.finish_native_save(0)
        self.assertEqual(self.boss.continued, 1)

    def test_failure_to_schedule_drain_cancels_quit_without_releasing_the_worker(self) -> None:
        self.boss.tabs = [[self.old]]
        watcher._running_saves.add(self.sid)
        with mock.patch.object(
            self.boss.native, "add_timer", side_effect=[1, RuntimeError("timer")]
        ):
            watcher.on_quit(self.boss, self.old, {"confirmed": True})
        self.assertEqual(watcher._quit_state.phase, watcher.QuitPhase.IDLE)
        self.assertIn(self.sid, watcher._running_saves)
        self.assertEqual(self.boss.jobs, [])
        self.assertEqual(self.boss.continued, 0)

    def test_last_pane_closed_while_draining_does_not_leave_a_stale_live_save(self) -> None:
        self.boss.tabs = [[self.old]]
        watcher._running_saves.add(self.sid)
        watcher.on_quit(self.boss, self.old, {"confirmed": True})
        watcher.on_close(self.boss, self.old, {})
        self.boss.tabs = []
        self.kitty.include_tab = False
        watcher._finish_autosave(self.sid)
        self.fire_drain()
        self.assertEqual(len(self.boss.jobs), 1)
        self.assertIn("closing_pane", self.boss.jobs[0].payload)
        self.finish_native_save(0)
        self.assertEqual(len(self.boss.jobs), 1, "quit tried to save a nonexistent live layout")
        self.assertEqual(self.boss.continued, 1)

    def test_removed_session_capture_is_preserved_without_recreating_the_session(self) -> None:
        capture = self.queue_unknown_close()
        trash = self.store.move_to_trash(self.sid)
        self.service.save_closing_pane(self.sid, capture)
        self.assertTrue(trash.is_dir())
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(list((self.store.root / "recovered-panes").rglob("*.json"))), 1)
