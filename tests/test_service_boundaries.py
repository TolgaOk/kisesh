from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kitty_workbench.domain import SessionContext
from kitty_workbench.kitty_client import KittyError, LiveTab
from kitty_workbench.service import (
    UnownedTabsAction,
    UnownedTabsDecision,
    WorkbenchError,
    WorkbenchService,
    _capture_pane_texts,
    _environment_window_id,
)
from kitty_workbench.session_file import sanitize_session, snapshot_summary
from kitty_workbench.store import SessionStore, StoredSession, StoreError
from tests.fakes import FakeKitty


class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root / "data")
        self.kitty = FakeKitty()
        self.service = WorkbenchService(self.store, self.kitty)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot(self, session_id: str, title: str = "Saved") -> None:
        stored = self.store.get(session_id)
        content = sanitize_session(f"new_tab {title}\nlaunch --cwd=/tmp/project\n", stored.manifest)
        self.store.write_snapshot(session_id, content, snapshot_summary(content))

    def test_pane_text_capture_is_independent_across_errors_none_and_text(self) -> None:
        tab = LiveTab(1, 7, 0, "Work", "splits", [{"id": 1}, {"id": 2}, {"id": 3}])

        def reader(window_id: int) -> str | None:
            if window_id == 1:
                raise KittyError("pane disappeared")
            return None if window_id == 2 else "captured"

        self.assertEqual(_capture_pane_texts([tab], reader), {3: "captured"})

    def test_views_group_live_tabs_and_survive_connection_and_context_errors(self) -> None:
        stored = self.store.create("Project", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, stored.manifest)
        self.kitty.extra_tabs.append(
            LiveTab(1, 8, 1, "Scratch", "splits", [{"id": 12, "user_vars": {}}])
        )
        constructions = 0

        def factory() -> FakeKitty:
            nonlocal constructions
            constructions += 1
            return self.kitty

        service = WorkbenchService(self.store, kitty_factory=factory)
        views = service.views()
        self.assertEqual(constructions, 1)
        self.assertTrue(views[0].live)
        self.assertIsNone(views[0].context)

        with mock.patch.object(self.store, "read_context", side_effect=StoreError("broken")):
            self.assertIsNone(service.views()[0].context)

        with mock.patch.object(self.kitty, "tabs", side_effect=KittyError("socket gone")):
            disconnected = service.views()
        self.assertFalse(disconnected[0].live)
        self.assertIsNone(service.kitty)

    def test_create_rejects_blank_names_and_tabs_that_already_have_membership(self) -> None:
        with self.assertRaisesRegex(WorkbenchError, "name cannot be empty"):
            self.service.create_from_active("   ")

        existing = self.store.create("Existing", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, existing.manifest)
        with self.assertRaisesRegex(WorkbenchError, "already belongs"):
            self.service.create_from_active("Second")

    def test_unowned_creation_validates_names_decisions_and_current_window_state(self) -> None:
        with self.assertRaisesRegex(WorkbenchError, "name cannot be empty"):
            self.service.create_from_unowned(
                "   ",
                UnownedTabsDecision(UnownedTabsAction.ATTACH),
            )
        with self.assertRaisesRegex(WorkbenchError, "only save-separately accepts"):
            self.service.create_from_unowned(
                "New",
                UnownedTabsDecision(UnownedTabsAction.ATTACH, "Unexpected"),
            )

        owned = self.store.create("Existing", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, owned.manifest)
        with self.assertRaisesRegex(WorkbenchError, "no unowned tabs"):
            self.service.create_from_unowned(
                "New",
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )
        self.assertEqual(
            [stored.manifest.name for stored in self.store.list()],
            ["Existing"],
        )

    def test_unowned_creation_tolerates_focus_race_and_honors_root_override(self) -> None:
        raced_tab = LiveTab(
            1,
            8,
            1,
            "Raced focus",
            "splits",
            [{"id": 12, "cwd": "/tmp/raced", "user_vars": {}}],
        )
        self.kitty.extra_tabs.append(raced_tab)

        with mock.patch.object(
            self.service,
            "_source_unowned_tabs",
            return_value=[raced_tab],
        ):
            created = self.service.create_from_unowned(
                "Selected",
                UnownedTabsDecision(UnownedTabsAction.ATTACH),
                "/tmp/override",
            )

        self.assertEqual(created.manifest.project_root, "/tmp/override")
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (created.manifest.id, raced_tab.tab_id),
        )
        self.assertIsNone(self.kitty.tab.session_id())

    def test_failed_blank_snapshot_write_removes_the_partial_session(self) -> None:
        with (
            mock.patch.object(
                self.store,
                "write_snapshot",
                side_effect=OSError("disk full"),
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.service.create_from_unowned(
                "No Trace",
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )

        self.assertEqual(self.store.list(), [])
        self.assertTrue(self.kitty.include_tab)
        self.assertEqual(self.kitty.opened, [])

    def test_failed_blank_snapshot_cleanup_does_not_hide_the_write_error(self) -> None:
        with (
            mock.patch.object(
                self.store,
                "write_snapshot",
                side_effect=OSError("disk full"),
            ),
            mock.patch.object(
                self.store,
                "move_to_trash",
                side_effect=StoreError("trash unavailable"),
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.service.create_from_unowned(
                "Recoverable Partial",
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )

        self.assertEqual(
            [stored.manifest.name for stored in self.store.list()],
            ["Recoverable Partial"],
        )

    def test_failed_fresh_shell_open_removes_blank_but_never_discards_source_tabs(self) -> None:
        with (
            mock.patch.object(
                self.service,
                "_open_inactive_snapshot",
                side_effect=WorkbenchError("fresh shell failed"),
            ),
            self.assertRaisesRegex(WorkbenchError, "fresh shell failed"),
        ):
            self.service.create_from_unowned(
                "Failed Fresh Start",
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )

        self.assertEqual(self.store.list(), [])
        self.assertTrue(self.kitty.include_tab)
        self.assertEqual(self.kitty.closed_tabs, [])
        self.assertIsNone(self.kitty.tab.session_id())

    def test_failed_fresh_shell_after_preservation_keeps_the_recoverable_source(self) -> None:
        with (
            mock.patch.object(
                self.service,
                "_open_inactive_snapshot",
                side_effect=WorkbenchError("fresh shell failed"),
            ),
            self.assertRaisesRegex(WorkbenchError, "fresh shell failed"),
        ):
            self.service.create_from_unowned(
                "Failed Target",
                UnownedTabsDecision(
                    UnownedTabsAction.SAVE_SEPARATELY,
                    "Safe Source",
                ),
            )

        sessions = self.store.list()
        self.assertEqual([stored.manifest.name for stored in sessions], ["Safe Source"])
        self.assertEqual(self.kitty.tab.session_id(), sessions[0].manifest.id)
        self.assertTrue(sessions[0].snapshot_path.is_file())
        self.assertEqual(self.kitty.closed_tabs, [])

    def test_failed_open_retains_a_blank_session_if_kitty_reports_it_live(self) -> None:
        fresh_shell = LiveTab(
            1,
            8,
            1,
            "Fresh shell",
            "splits",
            [{"id": 12, "cwd": "/tmp", "user_vars": {}}],
        )

        def fail_after_open(identifier: str, decision: UnownedTabsDecision) -> StoredSession:
            stored = self.store.get(identifier)
            self.kitty.stamp_tab(fresh_shell, stored.manifest)
            self.kitty.extra_tabs.append(fresh_shell)
            raise WorkbenchError(f"activation failed after {decision.action}")

        with (
            mock.patch.object(self.service, "open", side_effect=fail_after_open),
            self.assertRaisesRegex(WorkbenchError, "activation failed"),
        ):
            self.service.create_from_unowned(
                "Still Recoverable",
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )

        sessions = self.store.list()
        self.assertEqual([stored.manifest.name for stored in sessions], ["Still Recoverable"])
        self.assertEqual(fresh_shell.session_id(), sessions[0].manifest.id)
        self.assertTrue(self.kitty.include_tab)

    def test_failed_open_keeps_blank_when_live_state_or_cleanup_is_unavailable(self) -> None:
        for failure in (KittyError("socket gone"), None):
            with self.subTest(failure=failure):
                root = self.root / ("socket" if failure is not None else "trash")
                store = SessionStore(root)
                kitty = FakeKitty()
                service = WorkbenchService(store, kitty)
                patches = [
                    mock.patch.object(
                        service,
                        "open",
                        side_effect=WorkbenchError("open failed"),
                    )
                ]
                if failure is not None:
                    patches.append(
                        mock.patch.object(kitty, "tabs_for_session", side_effect=failure)
                    )
                else:
                    patches.append(
                        mock.patch.object(
                            store,
                            "move_to_trash",
                            side_effect=StoreError("trash unavailable"),
                        )
                    )
                with (
                    patches[0],
                    patches[1],
                    self.assertRaisesRegex(WorkbenchError, "open failed"),
                ):
                    service.create_from_unowned(
                        "Retained Blank",
                        UnownedTabsDecision(UnownedTabsAction.DISCARD),
                    )

                self.assertEqual(
                    [stored.manifest.name for stored in store.list()],
                    ["Retained Blank"],
                )

    def test_stale_native_owner_keeps_a_new_tab_explicitly_unowned(self) -> None:
        stale = self.store.create("Removed", "/tmp")
        native_name = str(stale.snapshot_path)
        self.kitty.window["session_name"] = native_name
        self.kitty.stamp_tab(self.kitty.tab, stale.manifest)
        self.store.move_to_trash(stale.manifest.id)
        new_tab = LiveTab(
            1,
            8,
            1,
            "New shell",
            "splits",
            [{"id": 12, "session_name": native_name, "user_vars": {}}],
        )
        self.kitty.extra_tabs.append(new_tab)
        self.kitty.current_tab = new_tab

        unowned = self.service.unowned_tabs_info()

        self.assertIsNotNone(unowned)
        assert unowned is not None
        self.assertEqual(unowned.count, 1)
        self.assertIsNone(new_tab.session_id())

    def test_failed_inherited_tab_save_rolls_back_automatic_membership(self) -> None:
        stored = self.store.create("Current", "/tmp")
        native_name = str(stored.snapshot_path)
        self.kitty.window["session_name"] = native_name
        self.kitty.stamp_tab(self.kitty.tab, stored.manifest)
        new_tab = LiveTab(
            1,
            8,
            1,
            "New shell",
            "splits",
            [{"id": 12, "session_name": native_name, "user_vars": {}}],
        )
        self.kitty.extra_tabs.append(new_tab)
        self.kitty.current_tab = new_tab

        with (
            mock.patch.object(self.service, "save", side_effect=RuntimeError("disk full")),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            self.service.unowned_tabs_info()

        self.assertIsNone(new_tab.session_id())

    def test_add_tab_rejects_archived_and_foreign_membership_and_is_idempotent(self) -> None:
        archived = self.store.archive(self.store.create("Archived", "/tmp").manifest.id)
        with self.assertRaisesRegex(WorkbenchError, "unarchive"):
            self.service.add_current_tab(archived.manifest.id)

        first = self.store.create("First", "/tmp")
        second = self.store.create("Second", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, first.manifest)
        with self.assertRaisesRegex(WorkbenchError, "another session"):
            self.service.add_current_tab(second.manifest.id)

        saved = self.service.add_current_tab(first.manifest.id)
        self.assertEqual(saved.manifest.id, first.manifest.id)

    def test_add_and_detach_roll_back_membership_when_the_followup_save_fails(self) -> None:
        target = self.store.create("Target", "/tmp")
        owned = LiveTab(1, 8, 1, "Owned", "splits", [{"id": 12, "user_vars": {}}])
        self.kitty.stamp_tab(owned, target.manifest)
        self.kitty.extra_tabs.append(owned)

        with (
            mock.patch.object(self.service, "save", side_effect=RuntimeError("disk full")),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            self.service.add_current_tab(target.manifest.id)
        self.assertIsNone(self.kitty.tab.session_id())

        self.kitty.stamp_tab(self.kitty.tab, target.manifest)
        with (
            mock.patch.object(self.service, "save", side_effect=RuntimeError("disk full")),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            self.service.detach_current_tab(target.manifest.id)
        self.assertEqual(self.kitty.tab.session_id(), target.manifest.id)

        def failed_rollback() -> None:
            raise KittyError("socket gone")

        WorkbenchService._rollback_membership(failed_rollback)

    def test_detach_rejects_a_tab_owned_by_another_session(self) -> None:
        target = self.store.create("Target", "/tmp")
        other = self.store.create("Other", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, other.manifest)
        with self.assertRaisesRegex(WorkbenchError, "does not belong"):
            self.service.detach_current_tab(target.manifest.id)

    def test_copy_rejects_archived_and_same_session_but_supports_an_empty_target(self) -> None:
        archived = self.store.archive(self.store.create("Archived", "/tmp").manifest.id)
        with self.assertRaisesRegex(WorkbenchError, "unarchive"):
            self.service.copy_current_tab(archived.manifest.id)

        target = self.store.create("Target", "/tmp")
        self.kitty.stamp_tab(self.kitty.tab, target.manifest)
        with self.assertRaisesRegex(WorkbenchError, "already belongs"):
            self.service.copy_current_tab(target.manifest.id)

        self.kitty.clear_tab_session(self.kitty.tab)
        copied = self.service.copy_current_tab(target.manifest.id)
        self.assertEqual(copied.manifest.summary.tab_count, 1)
        self.assertTrue(copied.snapshot_path.is_file())

    def test_current_session_save_current_and_not_live_failures_are_explicit(self) -> None:
        with self.assertRaisesRegex(WorkbenchError, "does not belong"):
            self.service.current_session()

        stored = self.service.create_from_active("Current")
        self.assertEqual(self.service.current_session().manifest.id, stored.manifest.id)
        self.assertEqual(self.service.save_current().manifest.id, stored.manifest.id)
        self.assertIsNotNone(self.service.context(stored.manifest.id))

        self.kitty.include_tab = False
        with self.assertRaisesRegex(WorkbenchError, "session is not live"):
            self.service.save(stored.manifest.id)

    def test_open_focuses_live_sessions_unarchives_saved_sessions_and_rejects_missing_snapshots(
        self,
    ) -> None:
        live = self.service.create_from_active("Live")
        reopened = self.service.open(live.manifest.id)
        self.assertEqual(reopened.manifest.id, live.manifest.id)
        self.assertEqual(self.kitty.focused[-1], self.kitty.tab.tab_id)

        self.kitty.clear_tab_session(self.kitty.tab)
        missing = self.store.create("Missing", "/tmp")
        with self.assertRaisesRegex(WorkbenchError, "has no snapshot"):
            self.service.open(missing.manifest.id)

        archived = self.store.archive(self.store.create("Archived", "/tmp").manifest.id)
        self.snapshot(archived.manifest.id, "Archived")
        self.kitty.stamp_tab(self.kitty.tab, archived.manifest)
        self.kitty.include_tab = False
        opened = self.service.open(archived.manifest.id)
        self.assertEqual(opened.manifest.status, "active")

    def test_open_without_context_or_visible_result_handles_bad_reminder_indexes(self) -> None:
        stored = self.store.create("Saved", "/tmp")
        self.snapshot(stored.manifest.id)
        self.kitty.stamp_tab(self.kitty.tab, stored.manifest)
        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)
        self.assertEqual(self.kitty.opened[-1], stored.snapshot_path)

        context: SessionContext = {
            "schema_version": 1,
            "captured_at": "2026-08-04T11:30:00Z",
            "programs": [],
            "agents": [],
            "command_count": 0,
            "restore_commands": [
                {
                    "argv": ["python", "server.py"],
                    "command": "python server.py",
                    "kind": "foreground",
                    "auto_run": False,
                    "tab_index": 9,
                    "pane_index": 0,
                    "tab_title": "Missing",
                    "pane_title": "Missing",
                    "cwd": "/tmp",
                },
                {
                    "argv": ["python", "worker.py"],
                    "command": "python worker.py",
                    "kind": "foreground",
                    "auto_run": False,
                    "tab_index": 0,
                    "pane_index": 9,
                    "tab_title": "Saved",
                    "pane_title": "Missing",
                    "cwd": "/tmp",
                },
            ],
            "tabs": [],
        }
        self.store.write_context(stored.manifest.id, context)
        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)
        self.assertEqual(self.kitty.sent_text, [])

        self.kitty.include_tab = False
        with mock.patch.object(self.kitty, "tabs_for_session", return_value=[]):
            self.service.open(stored.manifest.id)

    def test_failed_save_close_keeps_every_live_tab_running(self) -> None:
        stored = self.service.create_from_active("Still Live")

        with (
            mock.patch.object(self.service, "save", side_effect=StoreError("disk full")),
            self.assertRaisesRegex(StoreError, "disk full"),
        ):
            self.service.save_and_close(stored.manifest.id)

        self.assertTrue(self.kitty.include_tab)
        self.assertEqual(self.kitty.closed_sessions, [])
        self.assertEqual(self.kitty.tab.session_id(), stored.manifest.id)

    def test_failed_auto_session_save_rolls_back_tabs_and_moves_partial_state_to_trash(
        self,
    ) -> None:
        target = self.store.create("Requested", "/tmp/requested")
        self.snapshot(target.manifest.id, "Requested")

        with (
            mock.patch.object(self.service, "save", side_effect=StoreError("disk full")),
            self.assertRaisesRegex(StoreError, "disk full"),
        ):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.SAVE_SEPARATELY),
            )

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual(
            [stored.manifest.id for stored in self.store.list()],
            [target.manifest.id],
        )
        self.assertEqual(len(list(self.store.trash_dir.iterdir())), 1)
        self.assertEqual(self.kitty.opened, [])

    def test_failed_attach_save_restores_unowned_markers_without_closing_target(self) -> None:
        target = self.store.create("Target", "/tmp/target")
        target_tab = LiveTab(
            1,
            8,
            1,
            "Target",
            "splits",
            [{"id": 12, "cwd": "/tmp/target", "user_vars": {}}],
        )
        self.kitty.stamp_tab(target_tab, target.manifest)
        self.kitty.extra_tabs.append(target_tab)

        with (
            mock.patch.object(self.service, "save", side_effect=StoreError("disk full")),
            self.assertRaisesRegex(StoreError, "disk full"),
        ):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.ATTACH),
            )

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual(target_tab.session_id(), target.manifest.id)
        self.assertEqual(self.kitty.closed_sessions, [])

    def test_attach_rejects_a_snapshot_that_opens_no_live_tabs(self) -> None:
        target = self.store.create("Empty Restore", "/tmp/target")
        self.snapshot(target.manifest.id, "Empty Restore")

        with self.assertRaisesRegex(WorkbenchError, "did not create any live tabs"):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.ATTACH),
            )

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual(self.kitty.activated_sessions, [])

    def test_rename_without_snapshot_and_failed_live_restamp_still_persists(self) -> None:
        stored = self.store.create("Old", "/tmp")
        renamed = self.service.rename(stored.manifest.id, "New")
        self.assertEqual(renamed.manifest.slug, "new")

        with mock.patch.object(
            self.kitty,
            "restamp_session",
            side_effect=KittyError("socket gone"),
        ):
            renamed_again = self.service.rename(renamed.manifest.id, "Newest")
        self.assertEqual(renamed_again.manifest.slug, "newest")

    def test_lifecycle_uses_saved_state_when_kitty_fails_and_rejects_active_unarchive(self) -> None:
        stored = self.store.create("Saved", "/tmp")
        with mock.patch.object(
            self.kitty,
            "tabs_for_session",
            side_effect=KittyError("socket gone"),
        ):
            archived = self.service.archive(stored.manifest.id)
        self.assertEqual(archived.manifest.status, "archived")

        active = self.store.create("Active", "/tmp")
        with self.assertRaisesRegex(WorkbenchError, "not archived"):
            self.service.unarchive(active.manifest.id)

    def test_doctor_reports_store_kitty_snapshot_and_context_failures(self) -> None:
        with mock.patch.object(self.store, "list", side_effect=StoreError("root unreadable")):
            self.assertEqual(self.service.doctor(), ["ERROR store: root unreadable"])

        with mock.patch.object(
            self.kitty,
            "list_state",
            side_effect=KittyError("socket gone"),
        ):
            findings = self.service.doctor()
        self.assertIn("WARN kitty: socket gone", findings)

        unsaved = self.store.create("Unsaved", "/tmp")
        self.assertIn(
            "WARN unsaved: no snapshot",
            self.service._session_findings(unsaved),
        )

        malformed = self.store.create("Malformed", "/tmp")
        malformed.snapshot_path.write_text("launcher broken\n", encoding="utf-8")
        unsupported = self.store.create("Unsupported", "/tmp")
        self.snapshot(unsupported.manifest.id)
        context: SessionContext = {
            "schema_version": 999,
            "captured_at": "2026-08-04T11:30:00Z",
            "programs": [],
            "agents": [],
            "command_count": 0,
            "restore_commands": [],
            "tabs": [],
        }
        self.store.write_context(unsupported.manifest.id, context)

        findings = self.service.doctor()

        self.assertIn("ERROR malformed: invalid snapshot", "\n".join(findings))
        self.assertIn("ERROR unsupported: unsupported context schema", findings)

    def test_environment_window_id_requires_a_confirmed_overlay_and_valid_integer(self) -> None:
        scenarios: tuple[tuple[dict[str, str], int | None], ...] = (
            ({}, None),
            ({"KITTY_WORKBENCH_CALLER": "shell", "KITTY_WINDOW_ID": "9"}, None),
            ({"KITTY_WORKBENCH_CALLER": "overlay"}, None),
            ({"KITTY_WORKBENCH_CALLER": "manager", "KITTY_WINDOW_ID": "9"}, 9),
            ({"KITTY_WORKBENCH_CALLER": "overlay", "KITTY_WINDOW_ID": "bad"}, None),
        )
        for environment, expected in scenarios:
            with (
                self.subTest(environment=environment),
                mock.patch.dict("os.environ", environment, clear=True),
            ):
                self.assertEqual(_environment_window_id(), expected)


if __name__ == "__main__":
    unittest.main()
