"""Cross-layer review regressions using real storage and no native Kitty connections."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from kisesh import watcher
from kisesh.model import ClosingPaneCapture
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

    def test_removed_session_capture_is_preserved_without_recreating_the_session(self) -> None:
        capture = self.queue_unknown_close()
        trash = self.store.move_to_trash(self.sid)
        self.service.save_closing_pane(self.sid, capture)
        self.assertTrue(trash.is_dir())
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(list((self.store.root / "recovered-panes").rglob("*.json"))), 1)
