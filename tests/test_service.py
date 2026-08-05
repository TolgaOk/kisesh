from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kitty_workbench.domain import ClosingPaneCapture, CommandEvent, KittyWindow, SessionContext
from kitty_workbench.kitty_client import LiveTab
from kitty_workbench.model import (
    SESSION_ID_VAR,
    SESSION_SLUG_VAR,
)
from kitty_workbench.service import UnownedTabsAction, WorkbenchError, WorkbenchService
from kitty_workbench.session_file import sanitize_session, snapshot_summary
from kitty_workbench.store import SessionNotFound, SessionStore
from tests.fakes import FakeKitty

UNSAFE_CAPTURE = (
    "new_os_window\n"
    "new_tab Main\n"
    "cd /tmp/project\n"
    "launch --env TOKEN=secret "
    '\'kitty-unserialize-data={"cmd_at_shell_startup":"claude --continue","window_id":9}\' '
    "claude --continue\n"
    "new_tab Tests\n"
    "launch --cwd=/tmp/project lazygit\n"
)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SessionStore(root / "data")
        self.kitty = FakeKitty()
        self.kitty.capture_session_text = UNSAFE_CAPTURE
        self.kitty.capture_tab_text = UNSAFE_CAPTURE
        self.service = WorkbenchService(self.store, self.kitty)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_stamps_tab_and_writes_safe_multi_tab_snapshot(self) -> None:
        stored = self.service.create_from_active("My Project")

        self.assertEqual(self.kitty.tab.session_id(), stored.manifest.id)
        self.assertEqual(stored.manifest.summary.tab_count, 2)
        safe = stored.snapshot_path.read_text(encoding="utf-8")
        for forbidden in ("new_os_window", "TOKEN", "cmd_at_shell_startup", "claude", "lazygit"):
            self.assertNotIn(forbidden, safe)
        self.assertEqual(safe.count(f"{SESSION_ID_VAR}={stored.manifest.id}"), 2)
        self.assertTrue(stored.context_path.is_file())

    def test_save_records_completed_history_and_restores_live_agent_context(self) -> None:
        self.kitty.window.update(
            {
                "title": "Claude",
                "at_prompt": False,
                "in_alternate_screen": True,
                "foreground_processes": [
                    {
                        "cmdline": [
                            "claude",
                            "--resume",
                            "session-123",
                            "--dangerously-skip-permissions",
                        ],
                        "cwd": "/tmp/project",
                    }
                ],
                "last_cmd_exit_status": 0,
            }
        )
        stored = self.service.create_from_active("Agent Work")
        stored = self.service.save(
            stored.manifest.id,
            [
                {
                    "window_id": 11,
                    "command": "pytest -q",
                    "completed_at": "2026-08-04T11:30:00Z",
                }
            ],
        )
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context["tabs"][0]["panes"][0]["last_command"], "pytest -q")
        self.assertEqual(
            context["restore_commands"][0]["argv"],
            ["claude", "--resume", "session-123"],
        )
        self.assertNotIn("claude", stored.snapshot_path.read_text(encoding="utf-8"))

        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)

        self.assertIn("restore-shell", self.kitty.opened_contents[-1])
        self.assertNotIn("claude", self.kitty.opened_contents[-1])
        self.assertNotIn("dangerously-skip-permissions", self.kitty.opened_contents[-1])
        self.assertEqual(self.kitty.focused[-1], self.kitty.tab.tab_id)
        self.assertEqual(list(self.store.root.glob(".agent-work.restore.*")), [])

    def test_open_restores_inert_scrollback_before_a_normal_shell(self) -> None:
        self.kitty.window.update(
            {
                "last_reported_cmdline": "touch /tmp/must-not-run",
                "last_cmd_exit_status": 0,
                "at_prompt": True,
            }
        )
        self.kitty.command_outputs[11] = "build completed\n"
        self.kitty.terminal_histories[11] = "old prompt\nbuild completed\nnew prompt\n"
        stored = self.service.create_from_active("Shell Work")
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        pane = context["tabs"][0]["panes"][0]
        self.assertEqual(pane["last_command"], "touch /tmp/must-not-run")
        self.assertEqual(pane["last_command_output"], "build completed\n")
        self.assertEqual(
            pane["terminal_history"],
            "old prompt\nbuild completed\nnew prompt\n",
        )

        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)

        restored = self.kitty.opened_contents[-1]
        self.assertIn("restore-shell", restored)
        self.assertIn("--tab-index 0 --pane-index 0", restored)
        self.assertNotIn("touch /tmp/must-not-run", restored)
        self.assertNotIn("build completed", restored)
        self.assertEqual(list(self.store.root.glob(".shell-work.restore.*")), [])

    def test_cmd_w_after_reopen_persists_new_history_for_the_next_reopen(self) -> None:
        self.kitty.window.update(
            {
                "last_reported_cmdline": "ls",
                "last_cmd_exit_status": 0,
                "at_prompt": True,
            }
        )
        self.kitty.command_outputs[11] = "README.md\n"
        self.kitty.terminal_histories[11] = "ls\nREADME.md\n"
        stored = self.service.create_from_active("Repeated Reopen")

        self.kitty.include_tab = False
        self.kitty.next_open_window_id = 99
        self.service.open(stored.manifest.id)
        first_reopen = self.store.read_context(stored.manifest.id)
        assert first_reopen is not None
        self.assertEqual(first_reopen["tabs"][0]["panes"][0]["window_id"], 99)

        capture: ClosingPaneCapture = {
            "tab_index": 0,
            "pane_index": 0,
            "window": {
                "id": 99,
                "title": "Shell",
                "cwd": "/tmp/project",
                "user_vars": {SESSION_ID_VAR: stored.manifest.id},
                "foreground_processes": [{"cmdline": ["-zsh"]}],
                "at_prompt": True,
                "last_reported_cmdline": "pwd",
                "last_cmd_exit_status": 0,
            },
            "terminal_history": "ls\nREADME.md\npwd\n/tmp/project\n",
            "alternate_screen_text": "",
            "last_command_output": "/tmp/project\n",
            "command_events": [
                {
                    "window_id": 99,
                    "command": "pwd",
                    "completed_at": "2026-08-04T11:31:00Z",
                    "cwd": "/tmp/project",
                    "exit_status": 0,
                }
            ],
        }
        self.service.save_closing_pane(stored.manifest.id, capture)
        self.kitty.include_tab = False

        self.kitty.next_open_window_id = 123
        self.service.open(stored.manifest.id)
        second_reopen = self.store.read_context(stored.manifest.id)
        assert second_reopen is not None
        pane = second_reopen["tabs"][0]["panes"][0]

        self.assertEqual(pane["window_id"], 123)
        self.assertEqual([entry["command"] for entry in pane["command_history"]], ["ls", "pwd"])
        self.assertEqual(pane["terminal_history"], "ls\nREADME.md\npwd\n/tmp/project\n")
        self.assertEqual(pane["last_command_output"], "/tmp/project\n")
        self.assertIn("restore-shell", self.kitty.opened_contents[-1])

    def test_inflight_autosave_merges_a_close_committed_during_remote_capture(self) -> None:
        """Preserve the newer Cmd-W context when an older live save finishes later."""
        self.kitty.window.update(
            {
                "last_reported_cmdline": "ls",
                "last_cmd_exit_status": 0,
                "at_prompt": True,
            }
        )
        self.kitty.terminal_histories[11] = "INITIAL BUFFER\n"
        stored = self.service.create_from_active("Concurrent Close")
        pwd_event: CommandEvent = {
            "window_id": 11,
            "command": "pwd",
            "completed_at": "2026-08-04T11:31:00Z",
            "cwd": "/tmp/project",
        }
        close_capture: ClosingPaneCapture = {
            "tab_index": 0,
            "pane_index": 0,
            "window": {
                "id": 11,
                "title": "top",
                "cwd": "/tmp/project",
                "user_vars": {SESSION_ID_VAR: stored.manifest.id},
                "foreground_processes": [],
                "at_prompt": False,
                "in_alternate_screen": True,
                "last_reported_cmdline": "top",
            },
            "terminal_history": "INITIAL BUFFER\nCLOSE-ONLY BUFFER\n",
            "alternate_screen_text": "TOP AT CLOSE\n",
            "last_command_output": "/tmp/project\n",
            "command_events": [
                pwd_event,
                {
                    "window_id": 11,
                    "command": "echo close-only",
                    "completed_at": "2026-08-04T11:31:01Z",
                    "cwd": "/tmp/project",
                },
            ],
        }

        def commit_close(window_id: int) -> None:
            """Commit the newer close payload during the older save's text read."""
            self.assertEqual(window_id, 11)
            self.kitty.terminal_history_hook = None
            self.service.save_closing_pane(stored.manifest.id, close_capture)

        self.kitty.window.update(close_capture["window"])
        self.kitty.terminal_histories[11] = "ACTIVE TOP FRAME\n"
        self.kitty.terminal_history_hook = commit_close
        self.service.save(stored.manifest.id, [pwd_event])
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        pane = context["tabs"][0]["panes"][0]
        self.assertEqual(
            [entry["command"] for entry in pane["command_history"]],
            ["ls", "pwd", "echo close-only", "top"],
        )
        self.assertEqual(pane["terminal_history"], "INITIAL BUFFER\nCLOSE-ONLY BUFFER\n")
        self.assertEqual(pane["alternate_screen_text"], "ACTIVE TOP FRAME\n")

    def test_xxx_cmd_w_restores_three_apps_through_history_backed_shells(self) -> None:
        """Exercise the reported nvim, htop, and top teardown as one session."""
        commands = (("nvim", "."), ("htop",), ("top",))
        windows: list[KittyWindow] = []
        for index, argv in enumerate(commands):
            windows.append(
                {
                    "id": 31 + index,
                    "title": argv[0],
                    "cwd": "/tmp/project",
                    "user_vars": {},
                    "foreground_processes": ([] if argv == ("top",) else [{"cmdline": list(argv)}]),
                    "last_reported_cmdline": " ".join(argv),
                    "at_prompt": False,
                    "in_alternate_screen": True,
                }
            )
        self.kitty.window = windows[0]
        self.kitty.tab.windows = windows
        self.kitty.capture_session_text = "new_tab xxx\n" + "launch\n" * len(windows)
        self.kitty.terminal_histories = {
            window["id"]: f"{window['title'].upper()} FRAME\n" for window in windows
        }
        stored = self.service.create_from_active("xxx")

        for pane_index, (window, argv) in enumerate(zip(windows, commands, strict=True)):
            self.service.save_closing_pane(
                stored.manifest.id,
                {
                    "tab_index": 0,
                    "pane_index": pane_index,
                    "window": {
                        **window,
                        "foreground_processes": [],
                        "last_reported_cmdline": " ".join(argv),
                    },
                    "terminal_history": f"history before {argv[0]}\n",
                    "alternate_screen_text": f"{argv[0].upper()} AT CLOSE\n",
                    "last_command_output": "",
                    "command_events": [],
                },
            )

        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)
        restored = self.kitty.opened_contents[-1]
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(restored.count("restore-shell"), len(commands))
        self.assertEqual(
            [pane["last_command"] for pane in context["tabs"][0]["panes"]],
            ["nvim .", "htop", "top"],
        )
        self.assertEqual(
            [candidate["argv"] for candidate in context["restore_commands"]],
            [["nvim", "."], ["htop"], ["top"]],
        )

    def test_non_allowlisted_foreground_command_is_prefilled_without_enter(self) -> None:
        self.kitty.window.update(
            {
                "title": "Local server",
                "at_prompt": False,
                "in_alternate_screen": False,
                "foreground_processes": [{"cmdline": ["python", "server.py", "--port", "8080"]}],
            }
        )
        stored = self.service.create_from_active("Server Work")
        context = self.store.read_context(stored.manifest.id)
        assert context is not None
        self.assertFalse(context["restore_commands"][0]["auto_run"])

        self.kitty.include_tab = False
        self.service.open(stored.manifest.id)

        self.assertEqual(
            self.kitty.sent_text,
            [(11, "python server.py --port 8080")],
        )
        # The reminder travels over send-text without a carriage return. It is
        # never embedded as startup argv in Kitty's session file.
        self.assertNotIn("python server.py", self.kitty.opened_contents[-1])

    def test_rename_keeps_uuid_and_updates_live_and_saved_markers(self) -> None:
        created = self.service.create_from_active("Old Name")
        renamed = self.service.rename(created.manifest.id, "New Name")

        self.assertEqual(renamed.manifest.id, created.manifest.id)
        self.assertEqual(renamed.manifest.slug, "new-name")
        self.assertEqual(self.kitty.window["user_vars"][SESSION_SLUG_VAR], "new-name")
        self.assertIn(
            f"{SESSION_SLUG_VAR}=new-name", renamed.snapshot_path.read_text(encoding="utf-8")
        )

    def test_new_pane_is_adopted_before_whole_session_capture(self) -> None:
        created = self.service.create_from_active("Growing Session")
        new_pane: KittyWindow = {"id": 12, "cwd": "/tmp/project", "user_vars": {}}
        self.kitty.tab.windows.append(new_pane)

        self.service.save(created.manifest.id)

        self.assertEqual(new_pane["user_vars"][SESSION_ID_VAR], created.manifest.id)
        self.assertEqual(new_pane["user_vars"][SESSION_SLUG_VAR], "growing-session")

    def test_switching_live_sessions_hides_neither_processes_nor_owned_tabs(self) -> None:
        first = self.service.create_from_active("First Project")
        second = self.store.create("Second Project", "/tmp/second")
        second_tab = LiveTab(
            1,
            8,
            1,
            "Second",
            "splits",
            [{"id": 12, "cwd": "/tmp/second", "user_vars": {}}],
        )
        self.kitty.stamp_tab(second_tab, second.manifest)
        self.kitty.extra_tabs.append(second_tab)

        opened = self.service.open(second.manifest.id)

        self.assertEqual(opened.manifest.id, second.manifest.id)
        self.assertEqual(self.kitty.tab.session_id(), first.manifest.id)
        self.assertEqual(second_tab.session_id(), second.manifest.id)
        self.assertEqual(len(self.kitty.tabs()), 2)
        self.assertEqual(self.kitty.closed_sessions, [])
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (second.manifest.id, second_tab.tab_id),
        )

    def test_save_and_close_preserves_context_before_kill_and_reopens_it(self) -> None:
        self.kitty.window.update(
            {
                "last_reported_cmdline": "pwd",
                "last_cmd_exit_status": 0,
                "at_prompt": True,
            }
        )
        self.kitty.command_outputs[11] = "/tmp/project\n"
        self.kitty.terminal_histories[11] = "pwd\n/tmp/project\n"
        stored = self.service.create_from_active("Recoverable")
        self.kitty.window.update(
            {
                "last_reported_cmdline": "git status",
                "last_cmd_exit_status": 0,
            }
        )
        self.kitty.command_outputs[11] = "working tree clean\n"
        self.kitty.terminal_histories[11] = "pwd\n/tmp/project\ngit status\nworking tree clean\n"

        closed = self.service.save_and_close(stored.manifest.id)
        context_at_close = self.store.read_context(stored.manifest.id)

        self.assertEqual(closed.manifest.id, stored.manifest.id)
        self.assertEqual(self.kitty.closed_sessions, [stored.manifest.id])
        self.assertFalse(self.kitty.include_tab)
        self.assertIsNotNone(context_at_close)
        assert context_at_close is not None
        self.assertEqual(
            context_at_close["tabs"][0]["panes"][0]["terminal_history"],
            "pwd\n/tmp/project\ngit status\nworking tree clean\n",
        )
        self.assertEqual(
            context_at_close["tabs"][0]["panes"][0]["last_command"],
            "git status",
        )

        reopened = self.service.open(stored.manifest.id)

        self.assertEqual(reopened.manifest.id, stored.manifest.id)
        self.assertTrue(self.kitty.include_tab)
        self.assertIn("restore-shell", self.kitty.opened_contents[-1])

    def test_open_requires_a_choice_then_can_attach_all_current_unowned_tabs(self) -> None:
        target = self.store.create("Existing Project", "/tmp/existing")
        target_tab = LiveTab(
            1,
            8,
            1,
            "Existing",
            "splits",
            [{"id": 12, "cwd": "/tmp/existing", "user_vars": {}}],
        )
        scratch_tab = LiveTab(
            1,
            9,
            2,
            "Notes",
            "splits",
            [{"id": 13, "cwd": "/tmp/notes", "user_vars": {}}],
        )
        self.kitty.stamp_tab(target_tab, target.manifest)
        self.kitty.extra_tabs.extend((target_tab, scratch_tab))

        self.assertEqual(self.service.unowned_tab_count(), 2)
        with self.assertRaisesRegex(WorkbenchError, "2 unowned tab"):
            self.service.open(target.manifest.id)

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertIsNone(scratch_tab.session_id())
        self.assertEqual(self.kitty.activated_sessions, [])

        opened = self.service.open(target.manifest.id, UnownedTabsAction.ATTACH)

        self.assertEqual(opened.manifest.id, target.manifest.id)
        self.assertEqual(self.kitty.tab.session_id(), target.manifest.id)
        self.assertEqual(scratch_tab.session_id(), target.manifest.id)
        self.assertEqual(len(self.kitty.tabs_for_session(target.manifest.id)), 3)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (target.manifest.id, self.kitty.tab.tab_id),
        )

    def test_open_can_save_unowned_tabs_as_an_auto_session_without_mixing(self) -> None:
        self.kitty.tab.title = "Shell"
        target = self.store.create("Requested Project", "/tmp/requested")
        target_snapshot = sanitize_session(
            "new_tab Requested\nlaunch --cwd=/tmp/requested\n",
            target.manifest,
        )
        self.store.write_snapshot(
            target.manifest.id,
            target_snapshot,
            snapshot_summary(target_snapshot),
        )
        scratch_tab = LiveTab(
            1,
            9,
            1,
            "Notes",
            "splits",
            [{"id": 13, "cwd": "/tmp/notes", "user_vars": {}}],
        )
        restored_tab = LiveTab(
            1,
            10,
            2,
            "Requested",
            "splits",
            [{"id": 14, "cwd": "/tmp/requested", "user_vars": {}}],
        )
        self.kitty.stamp_tab(restored_tab, target.manifest)
        self.kitty.extra_tabs.append(scratch_tab)
        self.kitty.next_open_tab = restored_tab

        opened = self.service.open(
            target.manifest.id,
            UnownedTabsAction.SAVE_SEPARATELY,
        )

        sessions = self.store.list()
        auto = next(session for session in sessions if session.manifest.id != target.manifest.id)
        self.assertEqual(auto.manifest.name, "project · auto")
        self.assertEqual(self.kitty.tab.session_id(), auto.manifest.id)
        self.assertEqual(scratch_tab.session_id(), auto.manifest.id)
        self.assertEqual(
            [tab.tab_id for tab in self.kitty.tabs_for_session(target.manifest.id)],
            [restored_tab.tab_id],
        )
        self.assertEqual(opened.manifest.id, target.manifest.id)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (target.manifest.id, restored_tab.tab_id),
        )

    def test_add_tab_joins_an_already_live_multi_tab_session(self) -> None:
        target = self.store.create("Live Project", "/tmp/project")
        self.kitty.stamp_tab(self.kitty.tab, target.manifest)
        source_window: KittyWindow = {"id": 12, "cwd": "/tmp/other", "user_vars": {}}
        source = LiveTab(1, 8, 1, "Scratch", "splits", [source_window], is_focused=True)
        self.kitty.extra_tabs.append(source)
        self.kitty.current_tab = source

        updated = self.service.add_current_tab(target.manifest.id)

        self.assertEqual(source.session_id(), target.manifest.id)
        self.assertEqual(updated.manifest.id, target.manifest.id)
        self.assertEqual(len(self.kitty.tabs_for_session(target.manifest.id)), 2)

    def test_add_tab_refuses_to_overwrite_a_saved_session(self) -> None:
        target = self.store.create("Saved Project", "/tmp/saved")
        original = sanitize_session("new_tab Existing\nlaunch --cwd=/tmp/saved\n", target.manifest)
        self.store.write_snapshot(target.manifest.id, original, snapshot_summary(original))

        with self.assertRaisesRegex(WorkbenchError, "open the saved session"):
            self.service.add_current_tab(target.manifest.id)

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual(target.snapshot_path.read_text(encoding="utf-8"), original)

    def test_detach_tab_leaves_it_running_and_saves_remaining_members(self) -> None:
        target = self.service.create_from_active("Multi Tab")
        second_window: KittyWindow = {"id": 12, "cwd": "/tmp/project", "user_vars": {}}
        second = LiveTab(1, 8, 1, "Tests", "splits", [second_window])
        self.kitty.stamp_tab(second, target.manifest)
        self.kitty.extra_tabs.append(second)

        updated = self.service.detach_current_tab(target.manifest.id)

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual(second.session_id(), target.manifest.id)
        self.assertEqual(updated.manifest.id, target.manifest.id)

    def test_detach_tab_refuses_to_orphan_the_sessions_only_live_tab(self) -> None:
        target = self.service.create_from_active("Only Tab")

        with self.assertRaisesRegex(WorkbenchError, "only live tab"):
            self.service.detach_current_tab(target.manifest.id)

        self.assertEqual(self.kitty.tab.session_id(), target.manifest.id)

    def test_copy_tab_appends_only_safe_layout_to_a_saved_target(self) -> None:
        target = self.store.create("Saved Target", "/tmp/target")
        existing = sanitize_session("new_tab Existing\nlaunch --cwd=/tmp/target\n", target.manifest)
        self.store.write_snapshot(target.manifest.id, existing, snapshot_summary(existing))
        source = self.store.create("Live Source", "/tmp/source")
        self.kitty.stamp_tab(self.kitty.tab, source.manifest)
        self.kitty.capture_tab_text = (
            "new_os_window\n"
            "new_tab Copied Agent\n"
            "cd /tmp/source\n"
            "launch --env TOKEN=secret "
            '\'kitty-unserialize-data={"cmd_at_shell_startup":"claude --resume"}\' '
            "claude --resume\n"
        )

        copied = self.service.copy_current_tab(target.manifest.id)
        content = copied.snapshot_path.read_text(encoding="utf-8")

        self.assertEqual(self.kitty.tab.session_id(), source.manifest.id)
        self.assertEqual(copied.manifest.summary.tab_count, 2)
        self.assertEqual(copied.manifest.summary.pane_count, 2)
        self.assertIn("new_tab Existing", content)
        self.assertIn("new_tab Copied Agent", content)
        self.assertIn("/tmp/source", content)
        for forbidden in ("TOKEN", "claude", "resume"):
            self.assertNotIn(forbidden, content)
        self.assertEqual(content.count(f"{SESSION_ID_VAR}={target.manifest.id}"), 2)

    def test_copy_tab_refuses_a_live_target_that_autosave_could_overwrite(self) -> None:
        target = self.store.create("Live Target", "/tmp/target")
        self.kitty.stamp_tab(self.kitty.tab, target.manifest)
        source = LiveTab(1, 8, 1, "Source", "splits", [{"id": 12, "user_vars": {}}])
        self.kitty.extra_tabs.append(source)
        self.kitty.current_tab = source

        with self.assertRaisesRegex(WorkbenchError, "saved target session"):
            self.service.copy_current_tab(target.manifest.id)

    def test_unarchive_returns_a_session_to_the_active_store(self) -> None:
        stored = self.store.create("Dormant Project", "/tmp/dormant")
        archived = self.service.archive(stored.manifest.id)

        restored = self.service.unarchive(archived.manifest.id)

        self.assertEqual(restored.manifest.status, "active")
        self.assertEqual(restored.directory.parent, self.store.sessions_dir)

    def test_live_session_cannot_be_archived(self) -> None:
        stored = self.service.create_from_active("Active Project")

        with self.assertRaisesRegex(WorkbenchError, "^live sessions cannot be archived$"):
            self.service.archive(stored.manifest.id)

        self.assertEqual(self.store.get(stored.manifest.id).manifest.status, "active")

    def test_remove_refuses_live_and_preserves_saved_and_archived_payloads_in_trash(self) -> None:
        live = self.service.create_from_active("Live Project")
        with self.assertRaisesRegex(WorkbenchError, "^live sessions cannot be removed$"):
            self.service.remove(live.manifest.id)
        self.assertEqual(self.store.get(live.manifest.id).manifest.id, live.manifest.id)

        inactive = self.store.create("Finished Project", "/tmp/finished")
        inactive_snapshot = "new_tab Finished\nlaunch --cwd=/tmp/finished\n"
        self.store.write_snapshot(
            inactive.manifest.id,
            inactive_snapshot,
            snapshot_summary(inactive_snapshot),
        )
        destination = self.service.remove(inactive.manifest.id)

        self.assertEqual(destination.parent, self.store.trash_dir)
        self.assertEqual(
            (destination / inactive.manifest.snapshot_file).read_text(encoding="utf-8"),
            inactive_snapshot,
        )
        with self.assertRaisesRegex(SessionNotFound, "unknown session"):
            self.store.get(inactive.manifest.id)

        dormant = self.store.create("Dormant Project", "/tmp/dormant")
        dormant_context: SessionContext = {
            "schema_version": 1,
            "captured_at": "2026-08-04T11:30:00Z",
            "programs": [],
            "agents": [],
            "command_count": 2,
            "restore_commands": [],
            "tabs": [],
        }
        self.store.write_context(dormant.manifest.id, dormant_context)
        archived = self.service.archive(dormant.manifest.id)

        archived_destination = self.service.remove(archived.manifest.id)

        self.assertEqual(archived_destination.parent, self.store.trash_dir)
        self.assertEqual(
            json.loads((archived_destination / "context.json").read_text(encoding="utf-8")),
            dormant_context,
        )
        with self.assertRaisesRegex(SessionNotFound, "unknown session"):
            self.store.get(archived.manifest.id)

    def test_doctor_detects_manual_or_corrupt_startup_commands(self) -> None:
        stored = self.service.create_from_active("Audited")
        stored.snapshot_path.write_text(
            "new_tab Audited\nlaunch dangerous-command\n", encoding="utf-8"
        )

        findings = self.service.doctor()
        self.assertIn("ERROR audited: snapshot checksum mismatch", findings)
        self.assertIn("ERROR audited: snapshot is not safely normalized", findings)

    def test_doctor_reports_corrupt_command_context(self) -> None:
        stored = self.service.create_from_active("Audited Context")
        stored.context_path.write_text("{broken", encoding="utf-8")

        findings = self.service.doctor()

        self.assertTrue(
            any(
                finding.startswith("ERROR audited-context: cannot read context")
                for finding in findings
            ),
            findings,
        )


if __name__ == "__main__":
    unittest.main()
