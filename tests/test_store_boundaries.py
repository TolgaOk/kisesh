from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kisesh.context import build_context
from kisesh.kitty_client import LiveTab
from kisesh.model import SnapshotSummary
from kisesh.store import (
    SessionConflict,
    SessionStore,
    StoreError,
    _filename_timestamp,
)


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root / "data", history_limit=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_listing_ignores_junk_and_reports_corrupt_manifests(self) -> None:
        stored = self.store.create("Valid", "/tmp")
        junk = self.store.sessions_dir / "plain-file"
        junk.write_text("junk", encoding="utf-8")
        empty = self.store.sessions_dir / "empty-directory"
        empty.mkdir()
        self.assertEqual([item.manifest.slug for item in self.store.list()], ["valid"])

        stored.directory.joinpath("manifest.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "manifest.*not an object"):
            self.store.list()
        stored.directory.joinpath("manifest.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "cannot read manifest"):
            self.store.list()

    def test_create_detects_a_directory_created_during_slug_selection(self) -> None:
        self.store.ensure()
        collision = self.store.sessions_dir / "raced"
        collision.mkdir()
        with self.assertRaisesRegex(SessionConflict, "directory already exists"):
            self.store.create("Raced", "/tmp")

    def test_snapshot_and_context_history_are_bounded_after_real_changes(self) -> None:
        stored = self.store.create("History", "/tmp")
        summary = SnapshotSummary(tab_count=1, pane_count=1, tab_titles=["History"])
        for revision in range(5):
            self.store.write_snapshot(
                stored.manifest.id,
                f"new_tab History\nlaunch --cwd=/tmp/{revision}\n",
                summary,
                now=f"2026-08-04T11:30:0{revision}Z",
            )
            context = build_context(
                [
                    LiveTab(
                        1,
                        7,
                        0,
                        "History",
                        "splits",
                        [{"id": 11, "cwd": f"/tmp/{revision}"}],
                    )
                ]
            )
            self.store.write_context(
                stored.manifest.id,
                context,
                now=f"2026-08-04T11:31:0{revision}Z",
            )

        self.assertEqual(len(list(stored.snapshot_history_dir.glob("*.kitty-session"))), 2)
        self.assertEqual(len(list(stored.context_history_dir.glob("*.json"))), 2)
        before = sorted(stored.context_history_dir.glob("*.json"))
        self.store.write_context(stored.manifest.id, context)
        self.assertEqual(sorted(stored.context_history_dir.glob("*.json")), before)

    def test_rename_archive_and_restore_handle_idempotence_and_destination_races(self) -> None:
        stored = self.store.create("Project", "/tmp")
        same_slug = self.store.rename(stored.manifest.id, "Project!")
        self.assertEqual(same_slug.directory, stored.directory)

        blocked_rename = self.store.sessions_dir / "renamed"
        blocked_rename.mkdir()
        with self.assertRaisesRegex(SessionConflict, "directory already exists"):
            self.store.rename(stored.manifest.id, "Renamed")
        blocked_rename.rmdir()

        blocked_archive = self.store.archived_dir / stored.manifest.slug
        blocked_archive.mkdir()
        with self.assertRaisesRegex(SessionConflict, "archive destination exists"):
            self.store.archive(stored.manifest.id)
        blocked_archive.rmdir()

        archived = self.store.archive(stored.manifest.id)
        self.assertEqual(self.store.archive(archived.manifest.id).directory, archived.directory)
        blocked_restore = self.store.sessions_dir / archived.manifest.slug
        blocked_restore.mkdir()
        with self.assertRaisesRegex(SessionConflict, "session destination exists"):
            self.store.restore_archive(archived.manifest.id)
        blocked_restore.rmdir()
        active = self.store.restore_archive(archived.manifest.id)
        self.assertEqual(self.store.restore_archive(active.manifest.id).directory, active.directory)

    def test_trash_and_context_history_names_remain_unique_on_timestamp_collision(self) -> None:
        now = "2026-08-04T11:30:00Z"
        first = self.store.create("Project", "/tmp")
        first_destination = self.store.move_to_trash(first.manifest.id, now=now)
        second = self.store.create("Project", "/tmp")
        second_destination = self.store.move_to_trash(second.manifest.id, now=now)

        self.assertNotEqual(first_destination, second_destination)
        self.assertTrue(second_destination.name.endswith("-2"))
        self.assertRegex(_filename_timestamp("not-a-time"), r"^\d{8}T\d{6}\.\d{6}Z$")


if __name__ == "__main__":
    unittest.main()
