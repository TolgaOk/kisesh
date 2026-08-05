from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kitty_workbench.context import build_context
from kitty_workbench.domain import SessionContext
from kitty_workbench.kitty_client import LiveTab
from kitty_workbench.model import SnapshotSummary
from kitty_workbench.store import SessionConflict, SessionNotFound, SessionStore, StoreError


def _context() -> SessionContext:
    """Build representative persisted shell context through production logic."""
    return build_context(
        [
            LiveTab(
                1,
                7,
                0,
                "Demo",
                "splits",
                [
                    {
                        "id": 11,
                        "cwd": "/demo",
                        "foreground_processes": [{"cmdline": ["-zsh"]}],
                        "at_prompt": True,
                    }
                ],
            )
        ],
        command_events=[
            {
                "window_id": 11,
                "command": "pytest -q",
                "completed_at": "2026-08-04T11:30:00Z",
            }
        ],
    )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root, history_limit=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_empty_name_cannot_leave_a_partial_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            self.store.create("  ", "/tmp")
        self.assertEqual(self.store.list(), [])

    def test_duplicate_names_and_rename_conflicts_are_rejected(self) -> None:
        first = self.store.create("Main Vault", "/vault")
        self.assertEqual(first.manifest.slug, "main-vault")

        with self.assertRaisesRegex(SessionConflict, "session name already exists"):
            self.store.create("main vault", "/vault/other")

        self.store.archive(first.manifest.id)
        with self.assertRaisesRegex(SessionConflict, "session name already exists"):
            self.store.create("Main Vault!", "/vault/other")

        second = self.store.create("Other", "/vault/other")
        with self.assertRaises(SessionConflict):
            self.store.rename(second.manifest.id, "Main Vault")
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            self.store.rename(first.manifest.id, "")
        self.assertEqual(self.store.get(first.manifest.id).manifest.slug, "main-vault")
        self.assertEqual(
            [stored.manifest.name for stored in self.store.list()],
            ["Other", "Main Vault"],
        )

    def test_snapshots_are_versioned_only_when_content_changes(self) -> None:
        stored = self.store.create("Demo", "/demo")
        summary = SnapshotSummary(tab_count=1, pane_count=1, tab_titles=["Demo"])
        first = self.store.write_snapshot(stored.manifest.id, "new_tab Demo\nlaunch\n", summary)
        duplicate = self.store.write_snapshot(stored.manifest.id, "new_tab Demo\nlaunch\n", summary)
        changed = self.store.write_snapshot(
            stored.manifest.id, "new_tab Demo\nlaunch --cwd=/demo\n", summary
        )

        self.assertEqual(first.manifest.revision, 1)
        self.assertEqual(duplicate.manifest.revision, 1)
        self.assertEqual(changed.manifest.revision, 2)
        history = list((changed.directory / "history").glob("*.kitty-session"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_text(encoding="utf-8"), "new_tab Demo\nlaunch\n")

    def test_archive_restore_and_trash_are_recoverable_moves(self) -> None:
        stored = self.store.create("Demo", "/demo")
        context = _context()
        self.store.write_context(stored.manifest.id, context)
        archived = self.store.archive(stored.manifest.id)
        self.assertEqual(archived.manifest.status, "archived")
        self.assertEqual(archived.directory.parent, self.store.archived_dir)
        self.assertEqual(self.store.read_context(stored.manifest.id), context)

        restored = self.store.restore_archive(stored.manifest.id)
        self.assertEqual(restored.manifest.status, "active")
        self.assertTrue(restored.context_path.is_file())
        destination = self.store.move_to_trash(stored.manifest.id)
        self.assertTrue(destination.is_dir())
        self.assertTrue((destination / "context.json").is_file())
        with self.assertRaises(SessionNotFound):
            self.store.get(stored.manifest.id)

    def test_context_round_trip_rejects_corrupt_or_non_object_data(self) -> None:
        stored = self.store.create("Demo", "/demo")
        context = _context()

        written = self.store.write_context(stored.manifest.id, context)

        self.assertEqual(self.store.read_context(stored.manifest.id), context)
        self.assertEqual(written.context_path.stat().st_mode & 0o777, 0o600)
        written.context_path.write_text("[]\n", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "not an object"):
            self.store.read_context(stored.manifest.id)
        written.context_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "cannot read context"):
            self.store.read_context(stored.manifest.id)


if __name__ == "__main__":
    unittest.main()
