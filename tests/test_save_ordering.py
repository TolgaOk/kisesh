"""Capture ordering across independent session-service callers."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from kisesh.service import KiSeshService
from kisesh.store import SessionStore
from tests.fakes import FakeKitty


class SaveOrderingTests(unittest.TestCase):
    def test_final_save_waits_for_an_older_capture_and_writes_the_latest_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary))
            kitty = FakeKitty()
            service = KiSeshService(store, kitty)
            stored = service.create_from_active("Work")
            captured = threading.Event()
            release = threading.Event()
            final_started = threading.Event()
            native_capture = kitty.capture_session

            def older_capture(session_id: str, destination: Path) -> None:
                native_capture(session_id, destination)
                captured.set()
                if not release.wait(5):
                    raise TimeoutError("capture was not released")

            def final_capture(session_id: str, destination: Path) -> None:
                final_started.set()
                native_capture(session_id, destination)
                destination.write_text("new_tab Work\nlayout tall\nlaunch\n", encoding="utf-8")

            with (
                ThreadPoolExecutor(max_workers=2) as pool,
                mock.patch.object(kitty, "capture_session", side_effect=older_capture),
            ):
                older = pool.submit(service.save, stored.manifest.id)
                self.assertTrue(captured.wait(5))
                with mock.patch.object(kitty, "capture_session", side_effect=final_capture):
                    latest = pool.submit(service.save, stored.manifest.id)
                    try:
                        self.assertFalse(final_started.wait(0.1))
                    finally:
                        release.set()
                    older.result(timeout=5)
                    latest.result(timeout=5)

            self.assertIn("layout tall", stored.snapshot_path.read_text(encoding="utf-8"))
            context = store.read_context(stored.manifest.id)
            assert context is not None
            self.assertEqual(
                context["snapshot_revision"], store.get(stored.manifest.id).manifest.revision
            )

    def test_failed_capture_releases_the_session_for_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary))
            kitty = FakeKitty()
            service = KiSeshService(store, kitty)
            stored = service.create_from_active("Work")
            with (
                mock.patch.object(kitty, "capture_session", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                service.save(stored.manifest.id)
            service.save(stored.manifest.id)
            self.assertTrue(stored.snapshot_path.is_file())
