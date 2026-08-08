from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from kisesh.app_profiles import AppProfiles
from kisesh.context import pane_auto_run_argv
from kisesh.kitty_client import KittyError, LiveTab
from kisesh.model import (
    AGENT_SESSION_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    ClosingPaneCapture,
    CommandEvent,
    KittyOsWindowState,
    KittyWindow,
    SessionContext,
)
from kisesh.service import (
    KiSeshError,
    KiSeshService,
    UnownedTabsAction,
    UnownedTabsDecision,
    UnownedTabsInfo,
)
from kisesh.session_file import sanitize_session, snapshot_summary
from kisesh.store import (
    SessionConflict,
    SessionNotFound,
    SessionStore,
    StoredSession,
    StoreError,
)
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


class NativeSessionKitty(FakeKitty):
    """Model Kitty's filename-based native-session reuse during snapshot opens."""

    def __init__(self) -> None:
        """Initialize queued tabs and native-session identities."""
        super().__init__()
        self.queued_native_tabs: list[LiveTab] = []
        self.native_tabs: dict[str, LiveTab] = {}

    def open_snapshot(self, path: Path) -> None:
        """Create a queued tab or focus the tab already using the filename stem."""
        native_name = path.stem
        existing = self.native_tabs.get(native_name)
        if existing is not None:
            self.opened.append(path)
            self.opened_contents.append(path.read_text(encoding="utf-8"))
            self.focus_tab(existing.tab_id)
            return
        if not self.queued_native_tabs:
            raise AssertionError("a native session open requires one queued tab")
        opened = self.queued_native_tabs.pop(0)
        self.next_open_tab = opened
        super().open_snapshot(path)
        for window in opened.windows:
            window["session_name"] = native_name
        self.native_tabs[native_name] = opened

    def focus_tab(self, tab_id: int) -> None:
        """Focus a tab and make it the source for the next session operation."""
        super().focus_tab(tab_id)
        focused = next((tab for tab in self.tabs() if tab.tab_id == tab_id), None)
        if focused is not None:
            self.current_tab = focused


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SessionStore(root / "data")
        self.kitty = FakeKitty()
        self.kitty.capture_session_text = UNSAFE_CAPTURE
        self.kitty.capture_tab_text = UNSAFE_CAPTURE
        self.service = KiSeshService(self.store, self.kitty)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_two_tab_session(self, name: str = "Two Tab Work") -> StoredSession:
        """Create and persist two realistic live tabs through the public service."""
        self.kitty.tab.title = "Editor"
        self.kitty.window["last_reported_cmdline"] = "nvim ."
        tests = LiveTab(
            1,
            8,
            1,
            "Tests",
            "stack",
            [
                {
                    "id": 12,
                    "title": "pytest",
                    "cwd": "/tmp/project",
                    "user_vars": {},
                    "foreground_processes": [{"cmdline": ["pytest", "-q"]}],
                    "last_reported_cmdline": "pytest -q",
                }
            ],
        )
        self.kitty.extra_tabs.append(tests)
        self.kitty.capture_session_text = (
            "new_tab Editor\n"
            "launch --cwd=/tmp/project\n"
            "new_tab Tests\n"
            "layout stack\n"
            "launch --cwd=/tmp/project\n"
        )
        self.kitty.terminal_histories = {
            11: "nvim .\n",
            12: "pytest -q\n2 passed\n",
        }
        return self.service.create_from_unowned(
            name,
            UnownedTabsDecision(UnownedTabsAction.ATTACH),
        )

    def test_agent_hook_updates_only_its_pane_and_survives_a_later_save(self) -> None:
        """Keep same-directory Claude and Codex identities isolated by pane ID."""
        claude_id = "7f676817-c49e-459c-86de-17382e2170ef"
        codex_id = "019fd808-918d-7481-b526-c4da01513c42"
        self.kitty.window.update(
            {
                "title": "Claude",
                "at_prompt": False,
                "foreground_processes": [{"cmdline": ["claude"], "cwd": "/tmp/project"}],
            }
        )
        codex: KittyWindow = {
            "id": 12,
            "title": "Codex",
            "cwd": "/tmp/project",
            "user_vars": {},
            "foreground_processes": [{"cmdline": ["codex"], "cwd": "/tmp/project"}],
            "at_prompt": False,
        }
        self.kitty.tab.windows.append(codex)
        stored = self.service.create_from_active("Agent Work")

        self.service.record_agent_session_id("claude", claude_id, 11)
        first = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(
            [command["argv"] for command in first["restore_commands"]],
            [["claude", "--resume", claude_id], ["codex"]],
        )
        self.assertEqual(first.get("snapshot_revision"), stored.manifest.revision)
        self.assertEqual(self.kitty.window["user_vars"].get(AGENT_SESSION_VAR), claude_id)
        self.assertNotIn(AGENT_SESSION_VAR, codex["user_vars"])

        self.service.record_agent_session_id("codex", codex_id, 12)
        self.service.save(stored.manifest.id)
        saved = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(
            [command["argv"] for command in saved["restore_commands"]],
            [["claude", "--resume", claude_id], ["codex", "resume", codex_id]],
        )
        with self.assertRaisesRegex(KiSeshError, "Kitty pane is unavailable: 404"):
            self.service.record_agent_session_id("claude", claude_id, 404)

    def test_agent_hook_validates_before_mutation_and_carries_an_unowned_marker(self) -> None:
        """Reject bad IDs, then retain a valid pre-session identity through Save As."""
        claude_id = "7f676817-c49e-459c-86de-17382e2170ef"
        self.kitty.window.update(
            {
                "title": "Claude",
                "at_prompt": False,
                "foreground_processes": [{"cmdline": ["claude"], "cwd": "/tmp/project"}],
            }
        )

        with self.assertRaisesRegex(KiSeshError, "invalid claude session ID"):
            self.service.record_agent_session_id("claude", "not-a-session", 11)
        self.assertEqual(self.kitty.user_var_updates, [])

        self.assertIsNone(self.service.record_agent_session_id("claude", claude_id, 11))
        self.assertEqual(self.kitty.window["user_vars"].get(AGENT_SESSION_VAR), claude_id)

        stored = self.service.create_from_active("Later Attached")
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context["restore_commands"][0]["argv"],
            [
                "claude",
                "--resume",
                claude_id,
            ],
        )

    def test_agent_hook_rebuilds_missing_or_unversioned_context(self) -> None:
        """Persist an exact identity even when prior context is absent or legacy-shaped."""
        first_id = "7f676817-c49e-459c-86de-17382e2170ef"
        second_id = "019fd808-918d-7481-b526-c4da01513c42"
        self.kitty.window.update(
            {
                "title": "Claude",
                "at_prompt": False,
                "foreground_processes": [{"cmdline": ["claude"], "cwd": "/tmp/project"}],
            }
        )
        stored = self.service.create_from_active("Agent Work")
        stored.context_path.unlink()

        self.service.record_agent_session_id("claude", first_id, 11)
        rebuilt = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(rebuilt)
        assert rebuilt is not None
        self.assertNotIn("snapshot_revision", rebuilt)
        rebuilt["snapshot_revision"] = True
        self.store.write_context(stored.manifest.id, rebuilt)

        self.service.record_agent_session_id("claude", second_id, 11)
        updated = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertNotIn("snapshot_revision", updated)
        self.assertEqual(
            updated["restore_commands"][0]["argv"],
            ["claude", "--resume", second_id],
        )

    def test_create_stamps_tab_and_writes_safe_multi_tab_snapshot(self) -> None:
        stored = self.service.create_from_active("My Project")

        self.assertEqual(self.kitty.tab.session_id(), stored.manifest.id)
        self.assertEqual(stored.manifest.summary.tab_count, 2)
        safe = stored.snapshot_path.read_text(encoding="utf-8")
        for forbidden in ("new_os_window", "TOKEN", "cmd_at_shell_startup", "claude", "lazygit"):
            self.assertNotIn(forbidden, safe)
        self.assertEqual(safe.count(f"{SESSION_ID_VAR}={stored.manifest.id}"), 2)
        self.assertTrue(stored.context_path.is_file())

    def test_new_session_can_group_every_unowned_tab_in_its_kitty_window(self) -> None:
        scratch = LiveTab(
            1,
            9,
            1,
            "Notes",
            "splits",
            [{"id": 13, "cwd": "/tmp/notes", "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        other_window = LiveTab(
            2,
            20,
            0,
            "Other window",
            "splits",
            [{"id": 21, "cwd": "/tmp/other", "user_vars": {}}],
        )
        self.kitty.extra_tabs.extend((scratch, other_window))
        self.kitty.current_tab = scratch
        self.kitty.capture_session_text = (
            "new_tab Shell\nlaunch --cwd=/tmp/project\nnew_tab Notes\nlaunch --cwd=/tmp/notes\n"
        )

        created = self.service.create_from_unowned(
            "Research",
            UnownedTabsDecision(UnownedTabsAction.ATTACH),
        )

        self.assertEqual(self.kitty.tab.session_id(), created.manifest.id)
        self.assertEqual(scratch.session_id(), created.manifest.id)
        self.assertIsNone(other_window.session_id())
        self.assertEqual(created.manifest.project_root, "/tmp/notes")
        self.assertEqual(created.manifest.summary.tab_count, 2)
        self.assertEqual(self.kitty.opened, [])
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (created.manifest.id, scratch.tab_id),
        )

    def test_new_session_from_tracked_session_opens_fresh_without_reassigning_tabs(self) -> None:
        current = self.service.create_from_active("Current Work")
        self.kitty.window["cwd"] = "/tmp/current-work"
        self.kitty.window["foreground_processes"] = [
            {"cmdline": ["-zsh"], "cwd": "/tmp/current-work"}
        ]
        fresh_shell = LiveTab(
            1,
            9,
            1,
            "Fresh Shell",
            "splits",
            [{"id": 13, "cwd": "/tmp/current-work", "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        self.kitty.next_open_tab = fresh_shell

        created = self.service.create_from_active("New Direction")

        self.assertEqual(self.kitty.tab.session_id(), current.manifest.id)
        self.assertEqual(fresh_shell.session_id(), created.manifest.id)
        self.assertNotEqual(created.manifest.id, current.manifest.id)
        self.assertEqual(created.manifest.project_root, "/tmp/current-work")
        self.assertEqual(created.manifest.summary.tab_count, 1)
        self.assertEqual(created.manifest.summary.pane_count, 1)
        self.assertEqual(self.kitty.closed_tabs, [])
        self.assertEqual(self.kitty.closed_sessions, [])
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (created.manifest.id, fresh_shell.tab_id),
        )
        live_ids = {view.stored.manifest.id for view in self.service.views() if view.live}
        self.assertEqual(live_ids, {current.manifest.id, created.manifest.id})

    def test_consecutive_fresh_sessions_never_reuse_the_native_current_session(self) -> None:
        kitty = NativeSessionKitty()
        service = KiSeshService(self.store, kitty)
        current = service.create_from_active("Current")
        work_tab = LiveTab(
            1,
            9,
            1,
            "Work",
            "splits",
            [{"id": 13, "cwd": "/tmp/work", "user_vars": {}}],
        )
        vault_tab = LiveTab(
            1,
            10,
            2,
            "Vault",
            "splits",
            [{"id": 14, "cwd": "/tmp/vault", "user_vars": {}}],
        )
        kitty.queued_native_tabs.extend((work_tab, vault_tab))

        work = service.create_from_active("Work")
        vault = service.create_from_active("Vault")

        self.assertEqual(kitty.tab.session_id(), current.manifest.id)
        self.assertEqual(work_tab.session_id(), work.manifest.id)
        self.assertEqual(vault_tab.session_id(), vault.manifest.id)
        native_names = [path.stem for path in kitty.opened]
        self.assertEqual(len(set(native_names)), 2)
        self.assertTrue(native_names[0].startswith(f".kisesh-{work.manifest.id}."))
        self.assertTrue(native_names[1].startswith(f".kisesh-{vault.manifest.id}."))
        self.assertNotIn("current", native_names)
        self.assertEqual(kitty.activated_sessions[-1], (vault.manifest.id, vault_tab.tab_id))
        self.assertEqual(kitty.current_tab.tab_id, vault_tab.tab_id)
        self.assertTrue(all(not path.exists() for path in kitty.opened))

    def test_snapshot_open_waits_for_kitty_to_publish_the_matching_tab(self) -> None:
        service = KiSeshService(self.store, self.kitty)
        service.create_from_active("Current")
        target = self.store.create("Delayed", "/tmp/delayed")
        snapshot = sanitize_session("new_tab Delayed\nlaunch\n", target.manifest)
        self.store.write_snapshot(
            target.manifest.id,
            snapshot,
            snapshot_summary(snapshot),
        )
        delayed_tab = LiveTab(
            1,
            9,
            1,
            "Delayed",
            "splits",
            [{"id": 13, "cwd": "/tmp/delayed", "user_vars": {}}],
        )
        checks = 0
        original_tabs_for_session = self.kitty.tabs_for_session

        def delayed_tabs(
            session_id: str,
            state: list[KittyOsWindowState] | None = None,
        ) -> list[LiveTab]:
            """Expose the restored tab only after two post-open state checks."""
            nonlocal checks
            if state is not None or session_id != target.manifest.id:
                return original_tabs_for_session(session_id, state)
            checks += 1
            if checks < 3:
                return []
            if delayed_tab not in self.kitty.extra_tabs:
                self.kitty.stamp_tab(delayed_tab, target.manifest)
                self.kitty.extra_tabs.append(delayed_tab)
            return [delayed_tab]

        with (
            mock.patch.object(self.kitty, "tabs_for_session", side_effect=delayed_tabs),
            mock.patch("kisesh.service.time.sleep") as pause,
        ):
            opened = service.open(target.manifest.id)

        self.assertEqual(opened.manifest.id, target.manifest.id)
        self.assertEqual(checks, 3)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (target.manifest.id, delayed_tab.tab_id),
        )

    def test_new_session_can_preserve_current_tabs_then_open_one_fresh_shell(self) -> None:
        self.kitty.window["cwd"] = "/tmp/Current Project"
        self.kitty.window["foreground_processes"] = [
            {"cmdline": ["-zsh"], "cwd": "/tmp/Current Project"}
        ]
        scratch = LiveTab(
            1,
            9,
            1,
            "Notes",
            "splits",
            [{"id": 13, "cwd": "/tmp/notes", "user_vars": {}}],
        )
        fresh_shell = LiveTab(
            1,
            10,
            2,
            "Fresh Shell",
            "splits",
            [{"id": 14, "cwd": "/tmp/Current Project", "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        self.kitty.extra_tabs.append(scratch)
        self.kitty.next_open_tab = fresh_shell
        self.kitty.capture_session_text = (
            "new_tab Project\nlaunch --cwd='/tmp/Current Project'\n"
            "new_tab Notes\nlaunch --cwd=/tmp/notes\n"
        )

        created = self.service.create_from_unowned(
            "Clean Room",
            UnownedTabsDecision(
                UnownedTabsAction.SAVE_SEPARATELY,
                "Earlier Work",
            ),
        )

        sessions = {stored.manifest.name: stored for stored in self.store.list()}
        preserved = sessions["Earlier Work"]
        self.assertEqual(created.manifest.name, "Clean Room")
        self.assertEqual(created.manifest.summary.tab_count, 1)
        self.assertEqual(created.manifest.summary.pane_count, 1)
        self.assertEqual(
            created.manifest.summary.working_directories,
            ["/tmp/Current Project"],
        )
        self.assertEqual(created.manifest.project_root, "/tmp/Current Project")
        self.assertEqual(self.kitty.tab.session_id(), preserved.manifest.id)
        self.assertEqual(scratch.session_id(), preserved.manifest.id)
        self.assertEqual(fresh_shell.session_id(), created.manifest.id)
        self.assertEqual(preserved.manifest.summary.tab_count, 2)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (created.manifest.id, fresh_shell.tab_id),
        )

    def test_new_session_discards_exact_source_tabs_only_after_fresh_shell_opens(self) -> None:
        scratch = LiveTab(
            1,
            9,
            1,
            "Throwaway",
            "splits",
            [{"id": 13, "cwd": "/tmp/scratch", "user_vars": {}}],
        )
        fresh_shell = LiveTab(
            1,
            10,
            2,
            "Fresh Shell",
            "splits",
            [{"id": 14, "cwd": "/tmp/project", "user_vars": {}}],
        )
        self.kitty.extra_tabs.append(scratch)
        self.kitty.next_open_tab = fresh_shell

        created = self.service.create_from_unowned(
            "Fresh Start",
            UnownedTabsDecision(UnownedTabsAction.DISCARD),
        )

        self.assertEqual(self.kitty.closed_tabs, [self.kitty.tab.tab_id, scratch.tab_id])
        self.assertEqual([tab.tab_id for tab in self.kitty.tabs()], [fresh_shell.tab_id])
        self.assertEqual(fresh_shell.session_id(), created.manifest.id)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (created.manifest.id, fresh_shell.tab_id),
        )
        self.assertEqual(
            [stored.manifest.name for stored in self.store.list()],
            ["Fresh Start"],
        )

    def test_autosave_after_cmd_w_removes_closed_tab_from_snapshot_and_context(self) -> None:
        closing = self.kitty.tab
        closing.title = "Temporary"
        remaining = LiveTab(
            1,
            9,
            1,
            "Keep",
            "splits",
            [{"id": 13, "cwd": "/tmp/keep", "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        self.kitty.extra_tabs.append(remaining)
        self.kitty.capture_session_text = (
            "new_tab Temporary\nlaunch --cwd=/tmp/project\nnew_tab Keep\nlaunch --cwd=/tmp/keep\n"
        )
        self.kitty.terminal_histories = {
            11: "temporary history\n",
            13: "keep history\n",
        }
        stored = self.service.create_from_unowned(
            "Two Tabs",
            UnownedTabsDecision(UnownedTabsAction.ATTACH),
        )

        self.kitty.include_tab = False
        self.kitty.current_tab = remaining
        self.kitty.capture_session_text = "new_tab Keep\nlaunch --cwd=/tmp/keep\n"
        saved = self.service.save(stored.manifest.id)
        context = self.store.read_context(stored.manifest.id)

        self.assertEqual(saved.manifest.summary.tab_count, 1)
        self.assertEqual(saved.manifest.summary.tab_titles, ["Keep"])
        self.assertNotIn("Temporary", saved.snapshot_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(len(context["tabs"]), 1)
        self.assertEqual(context["tabs"][0]["title"], "Keep")
        self.assertEqual(context["tabs"][0]["panes"][0]["window_id"], 13)
        self.assertEqual(
            context["tabs"][0]["panes"][0]["terminal_history"],
            "keep history\n",
        )
        self.assertNotIn("temporary history", str(context))

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

    def test_x_revives_the_exact_claude_and_codex_sessions_after_teardown(self) -> None:
        """Exercise save, destructive close callbacks, remap, and pane-specific revive."""
        codex_id = "019fd808-918d-7481-b526-c4da01513c42"
        claude_id = "7f676817-c49e-459c-86de-17382e2170ef"
        codex: KittyWindow = {
            "id": 11,
            "title": "Codex",
            "cwd": "/tmp/project",
            "user_vars": {},
            "foreground_processes": [{"cmdline": ["codex"], "pid": 101}],
            "at_prompt": False,
        }
        claude: KittyWindow = {
            "id": 12,
            "title": "Claude",
            "cwd": "/tmp/project",
            "user_vars": {},
            "foreground_processes": [{"cmdline": ["claude"], "pid": 202}],
            "at_prompt": False,
        }
        self.kitty.tab.windows = [codex, claude]
        self.kitty.window = codex
        self.kitty.capture_session_text = (
            "new_tab Agents\nlaunch --cwd=/tmp/project\nlaunch --cwd=/tmp/project\n"
        )

        def exact_resumes(
            tabs: Sequence[LiveTab],
            _profiles: AppProfiles,
        ) -> dict[int, list[str]]:
            """Return the identities observed for this two-agent live session."""
            self.assertEqual([window["id"] for tab in tabs for window in tab.windows], [11, 12])
            return {
                11: ["codex", "resume", codex_id],
                12: ["claude", "--resume", claude_id],
            }

        service = KiSeshService(self.store, self.kitty, resume_resolver=exact_resumes)
        stored = service.create_from_active("Exact Agents")
        service.save_and_close(stored.manifest.id)

        for pane_index, window in enumerate((codex, claude)):
            capture: ClosingPaneCapture = {
                "tab_index": 0,
                "pane_index": pane_index,
                "window": {
                    **window,
                    "foreground_processes": [],
                    "at_prompt": False,
                },
                "terminal_history": f"saved pane {pane_index}\n",
                "alternate_screen_text": "",
                "last_command_output": "",
                "command_events": [],
            }
            service.save_closing_pane(stored.manifest.id, capture)

        reopened_windows: list[KittyWindow] = [
            {
                "id": 91,
                "title": "Codex",
                "cwd": "/tmp/project",
                "user_vars": {},
                "foreground_processes": [{"cmdline": ["-zsh"]}],
                "at_prompt": True,
            },
            {
                "id": 92,
                "title": "Claude",
                "cwd": "/tmp/project",
                "user_vars": {},
                "foreground_processes": [{"cmdline": ["-zsh"]}],
                "at_prompt": True,
            },
        ]
        self.kitty.next_open_tab = LiveTab(
            1,
            27,
            0,
            "Agents",
            "splits",
            reopened_windows,
            is_focused=True,
            is_active=True,
        )
        service.open(stored.manifest.id)
        context = self.store.read_context(stored.manifest.id)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            [pane["window_id"] for pane in context["tabs"][0]["panes"]],
            [91, 92],
        )
        self.assertEqual(pane_auto_run_argv(context, 0, 0), ["codex", "resume", codex_id])
        self.assertEqual(
            pane_auto_run_argv(context, 0, 1),
            ["claude", "--resume", claude_id],
        )

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

    def test_open_rewrites_stale_ownership_markers_without_altering_layout(self) -> None:
        stored = self.store.create("Renamed Product", "/tmp/project")
        previous_snapshot = (
            "new_tab Editor\n"
            "layout splits\n"
            "launch --cwd=/tmp/project "
            "--var=kisesh_session=stale-id "
            "--var=kisesh_slug=stale-name "
            "--var=kisesh_name='Stale Name' "
            "--var=user_choice=keep\n"
        )
        self.store.write_snapshot(
            stored.manifest.id,
            previous_snapshot,
            snapshot_summary(previous_snapshot),
        )
        self.kitty.next_open_tab = LiveTab(
            1,
            8,
            1,
            "Editor",
            "splits",
            [{"id": 12, "cwd": "/tmp/project", "user_vars": {}}],
        )
        self.kitty.include_tab = False

        self.service.open(stored.manifest.id)

        restored = self.kitty.opened_contents[-1]
        self.assertNotIn("stale-id", restored)
        self.assertIn(f"{SESSION_ID_VAR}={stored.manifest.id}", restored)
        self.assertIn(f"{SESSION_SLUG_VAR}=renamed-product", restored)
        self.assertIn(f"{SESSION_NAME_VAR}=Renamed Product", restored)
        self.assertIn("layout splits", restored)
        self.assertIn("--var=user_choice=keep", restored)

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
        view = next(
            item for item in self.service.views() if item.stored.manifest.id == created.manifest.id
        )
        opened = self.service.open(created.manifest.id)

        self.assertEqual(renamed.manifest.id, created.manifest.id)
        self.assertEqual(renamed.manifest.slug, "new-name")
        self.assertTrue(view.live)
        self.assertEqual(opened.manifest.id, created.manifest.id)
        self.assertEqual(self.kitty.opened, [])
        self.assertEqual(self.kitty.window["user_vars"][SESSION_SLUG_VAR], "new-name")
        self.assertIn(
            f"{SESSION_SLUG_VAR}=new-name", renamed.snapshot_path.read_text(encoding="utf-8")
        )

    def test_live_tab_rename_updates_kitty_and_autosaves_complete_context(self) -> None:
        stored = self._create_two_tab_session()
        revision = stored.manifest.revision

        renamed = self.service.rename_tab(stored.manifest.id, 1, "Build results")
        context = self.store.read_context(stored.manifest.id)

        self.assertEqual(self.kitty.renamed_tabs, [(8, "Build results")])
        self.assertEqual(self.kitty.extra_tabs[0].title, "Build results")
        self.assertGreater(renamed.manifest.revision, revision)
        self.assertEqual(renamed.manifest.summary.tab_titles, ["Editor", "Build results"])
        self.assertIn("new_tab Build results", renamed.snapshot_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context["tabs"][1]["title"], "Build results")
        self.assertEqual(
            context["tabs"][1]["panes"][0]["terminal_history"],
            "pytest -q\n2 passed\n",
        )
        self.assertEqual(context["snapshot_revision"], renamed.manifest.revision)

    def test_saved_archived_tab_rename_survives_a_real_restore_materialization(self) -> None:
        stored = self._create_two_tab_session("Dormant Work")
        self.kitty.close_session_tabs(stored.manifest.id)
        archived = self.service.archive(stored.manifest.id)

        with mock.patch.object(
            self.kitty,
            "tabs_for_session",
            side_effect=KittyError("Kitty socket unavailable"),
        ):
            renamed = self.service.rename_tab(archived.manifest.id, 0, "Review")

        context = self.store.read_context(renamed.manifest.id)
        self.assertEqual(renamed.manifest.status, "archived")
        self.assertEqual(renamed.manifest.summary.tab_titles, ["Review", "Tests"])
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual([tab["title"] for tab in context["tabs"]], ["Review", "Tests"])
        self.assertEqual(context["snapshot_revision"], renamed.manifest.revision)

        reopened = self.service.open(renamed.manifest.id)

        self.assertEqual(reopened.manifest.status, "active")
        self.assertIn("new_tab Review", self.kitty.opened_contents[-1])
        self.assertIn("new_tab Tests", self.kitty.opened_contents[-1])

    def test_summary_only_saved_tab_can_be_renamed_without_context(self) -> None:
        stored = self.store.create("Layout Only", "/tmp/layout")
        snapshot = sanitize_session(
            "new_tab Shell\nlaunch --cwd=/tmp/layout\n",
            stored.manifest,
        )
        self.store.write_snapshot(stored.manifest.id, snapshot, snapshot_summary(snapshot))

        renamed = self.service.rename_tab(stored.manifest.id, 0, "Logs")

        self.assertIsNone(self.store.read_context(renamed.manifest.id))
        self.assertEqual(renamed.manifest.summary.tab_titles, ["Logs"])
        self.assertIn("new_tab Logs", renamed.snapshot_path.read_text(encoding="utf-8"))

    def test_tab_rename_rejects_missing_or_incoherent_saved_material(self) -> None:
        empty = self.store.create("Empty", "/tmp/empty")
        with self.assertRaisesRegex(KiSeshError, "no saved tab layout"):
            self.service.rename_tab(empty.manifest.id, 0, "Missing")

        stored = self._create_two_tab_session("Stale Context")
        with self.assertRaisesRegex(KiSeshError, "live session"):
            self.service.rename_tab(stored.manifest.id, 4, "Unavailable")
        self.kitty.close_session_tabs(stored.manifest.id)
        context = self.store.read_context(stored.manifest.id)
        self.assertIsNotNone(context)
        assert context is not None
        context["tabs"] = context["tabs"][:1]
        self.store.write_context(stored.manifest.id, context)
        original = stored.snapshot_path.read_text(encoding="utf-8")

        with self.assertRaisesRegex(KiSeshError, "saved context"):
            self.service.rename_tab(stored.manifest.id, 1, "Unavailable")
        self.assertEqual(stored.snapshot_path.read_text(encoding="utf-8"), original)

        with self.assertRaisesRegex(KiSeshError, "saved snapshot"):
            self.service.rename_tab(stored.manifest.id, 4, "Unavailable")

    def test_failed_live_tab_autosave_restores_the_native_title(self) -> None:
        stored = self._create_two_tab_session()
        original = stored.snapshot_path.read_text(encoding="utf-8")

        with (
            mock.patch.object(
                self.service,
                "save",
                side_effect=KiSeshError("capture failed"),
            ),
            self.assertRaisesRegex(KiSeshError, "capture failed"),
        ):
            self.service.rename_tab(stored.manifest.id, 1, "Temporary title")

        self.assertEqual(
            self.kitty.renamed_tabs,
            [(8, "Temporary title"), (8, "Tests")],
        )
        self.assertEqual(self.kitty.extra_tabs[0].title, "Tests")
        self.assertEqual(stored.snapshot_path.read_text(encoding="utf-8"), original)

    def test_failed_saved_context_write_rolls_the_snapshot_title_back(self) -> None:
        stored = self._create_two_tab_session()
        self.kitty.close_session_tabs(stored.manifest.id)
        original_snapshot = stored.snapshot_path.read_text(encoding="utf-8")
        original_context = self.store.read_context(stored.manifest.id)
        self.assertIsNotNone(original_context)
        assert original_context is not None
        write_context = self.store.write_context
        attempts = 0

        def fail_once(
            slug_or_id: str,
            context: SessionContext,
            *,
            now: str | None = None,
        ) -> StoredSession:
            """Fail the requested rename write, then permit the rollback write."""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise StoreError("context disk unavailable")
            return write_context(slug_or_id, context, now=now)

        with (
            mock.patch.object(self.store, "write_context", side_effect=fail_once),
            self.assertRaisesRegex(StoreError, "context disk unavailable"),
        ):
            self.service.rename_tab(stored.manifest.id, 1, "Should roll back")

        rolled_back = self.store.get(stored.manifest.id)
        context = self.store.read_context(stored.manifest.id)
        self.assertEqual(attempts, 2)
        self.assertEqual(rolled_back.snapshot_path.read_text(encoding="utf-8"), original_snapshot)
        self.assertEqual(rolled_back.manifest.summary.tab_titles, ["Editor", "Tests"])
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual([tab["title"] for tab in context["tabs"]], ["Editor", "Tests"])
        self.assertEqual(context["snapshot_revision"], rolled_back.manifest.revision)

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

    def test_switching_back_to_a_live_session_restores_its_last_focused_tab(self) -> None:
        first = self.store.create("First Project", "/tmp/first")
        second = self.store.create("Second Project", "/tmp/second")
        self.kitty.window["last_focused_at"] = 10.0
        first_last = LiveTab(
            1,
            8,
            1,
            "First tests",
            "splits",
            [
                {"id": 12, "last_focused_at": 35.0, "user_vars": {}},
                {"id": 13, "last_focused_at": 40.0, "user_vars": {}},
            ],
        )
        second_first = LiveTab(
            1,
            9,
            2,
            "Second editor",
            "splits",
            [{"id": 14, "last_focused_at": 20.0, "user_vars": {}}],
        )
        second_last = LiveTab(
            1,
            10,
            3,
            "Second tests",
            "splits",
            [{"id": 15, "last_focused_at": 30.0, "user_vars": {}}],
        )
        self.kitty.tab.is_focused = False
        self.kitty.tab.is_active = False
        self.kitty.stamp_tab(self.kitty.tab, first.manifest)
        self.kitty.stamp_tab(first_last, first.manifest)
        self.kitty.stamp_tab(second_first, second.manifest)
        self.kitty.stamp_tab(second_last, second.manifest)
        self.kitty.extra_tabs.extend((first_last, second_first, second_last))

        self.kitty.current_tab = second_first
        self.service.open(first.manifest.id)
        self.kitty.current_tab = first_last
        self.service.open(second.manifest.id)
        self.kitty.current_tab = second_last
        self.service.open(first.manifest.id)

        self.assertEqual(
            self.kitty.activated_sessions[-3:],
            [
                (first.manifest.id, first_last.tab_id),
                (second.manifest.id, second_last.tab_id),
                (first.manifest.id, first_last.tab_id),
            ],
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

    def test_final_tab_close_saves_then_promotes_the_next_live_session(self) -> None:
        self.kitty.window.update(
            {
                "last_reported_cmdline": "git status",
                "last_cmd_exit_status": 0,
                "at_prompt": True,
            }
        )
        self.kitty.terminal_histories[11] = "pwd\ngit status\nclean\n"
        closing = self.service.create_from_active("Closing")
        next_session = self.store.create("Next", "/tmp/next")
        next_tab = LiveTab(
            1,
            8,
            1,
            "Next",
            "splits",
            [{"id": 12, "cwd": "/tmp/next", "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        self.kitty.stamp_tab(next_tab, next_session.manifest)
        self.kitty.extra_tabs.append(next_tab)

        closed = self.service.save_and_close(closing.manifest.id, 1)
        context = self.store.read_context(closing.manifest.id)

        self.assertEqual(closed.manifest.id, closing.manifest.id)
        self.assertEqual(self.kitty.closed_sessions, [closing.manifest.id])
        self.assertEqual([tab.tab_id for tab in self.kitty.tabs()], [next_tab.tab_id])
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (next_session.manifest.id, next_tab.tab_id),
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context["tabs"][0]["panes"][0]["terminal_history"],
            "pwd\ngit status\nclean\n",
        )
        self.assertEqual(context["tabs"][0]["panes"][0]["last_command"], "git status")

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

        with mock.patch("kisesh.service.secrets.randbelow", return_value=0):
            self.assertEqual(
                self.service.unowned_tabs_info(),
                UnownedTabsInfo(2, "Amber Badger"),
            )
        with self.assertRaisesRegex(KiSeshError, "2 unowned tab"):
            self.service.open(target.manifest.id)

        self.assertIsNone(self.kitty.tab.session_id())
        self.assertIsNone(scratch_tab.session_id())
        self.assertEqual(self.kitty.activated_sessions, [])

        opened = self.service.open(
            target.manifest.id,
            UnownedTabsDecision(UnownedTabsAction.ATTACH),
        )

        self.assertEqual(opened.manifest.id, target.manifest.id)
        self.assertEqual(self.kitty.tab.session_id(), target.manifest.id)
        self.assertEqual(scratch_tab.session_id(), target.manifest.id)
        self.assertEqual(len(self.kitty.tabs_for_session(target.manifest.id)), 3)
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (target.manifest.id, self.kitty.tab.tab_id),
        )

    def test_new_native_tab_inherits_its_multi_tab_session_without_a_prompt(self) -> None:
        stored = self.store.create("Current Project", "/tmp/project")
        native_name = str(stored.snapshot_path)
        self.kitty.window["session_name"] = native_name
        self.kitty.window["last_focused_at"] = 10.0
        self.kitty.stamp_tab(self.kitty.tab, stored.manifest)
        new_tab = LiveTab(
            1,
            8,
            1,
            "New shell",
            "splits",
            [
                {
                    "id": 12,
                    "cwd": "/tmp/project",
                    "session_name": native_name,
                    "last_focused_at": 20.0,
                    "user_vars": {},
                }
            ],
            is_focused=True,
            is_active=True,
        )
        self.kitty.extra_tabs.append(new_tab)
        self.kitty.current_tab = new_tab
        self.kitty.capture_session_text = (
            "new_tab Existing\nlaunch --cwd=/tmp/project\n"
            "new_tab New shell\nlaunch --cwd=/tmp/project\n"
        )

        self.assertIsNone(self.service.unowned_tabs_info())

        self.assertEqual(new_tab.session_id(), stored.manifest.id)
        saved = self.store.get(stored.manifest.id)
        self.assertEqual(saved.manifest.summary.tab_count, 2)
        self.assertEqual(saved.manifest.summary.pane_count, 2)

    def test_new_tab_in_a_custom_live_session_uses_the_last_scoped_session(self) -> None:
        older = self.store.create("Older", "/tmp/older")
        current = self.store.create("Current", "/tmp/current")
        self.kitty.window["last_focused_at"] = 10.0
        self.kitty.window.setdefault("user_vars", {})[SESSION_SCOPE_VAR] = "1"
        self.kitty.stamp_tab(self.kitty.tab, older.manifest)
        current_tab = LiveTab(
            1,
            8,
            1,
            "Current",
            "splits",
            [
                {
                    "id": 12,
                    "last_focused_at": 20.0,
                    "user_vars": {SESSION_SCOPE_VAR: "1"},
                }
            ],
        )
        new_tab = LiveTab(
            1,
            9,
            2,
            "New shell",
            "splits",
            [{"id": 13, "user_vars": {}}],
            is_focused=True,
            is_active=True,
        )
        self.kitty.stamp_tab(current_tab, current.manifest)
        self.kitty.extra_tabs.extend((current_tab, new_tab))
        self.kitty.current_tab = new_tab

        self.assertIsNone(self.service.unowned_tabs_info())

        self.assertEqual(new_tab.session_id(), current.manifest.id)
        self.assertEqual(self.kitty.tab.session_id(), older.manifest.id)

    def test_random_unowned_name_skips_collisions_and_ignores_owned_tabs(self) -> None:
        owned = self.store.create("Amber Badger", "/existing")
        self.store.create("Amber Badger 2", "/another")

        with mock.patch("kisesh.service.secrets.randbelow", return_value=0):
            self.assertEqual(
                self.service.unowned_tabs_info(),
                UnownedTabsInfo(1, "Amber Badger 3"),
            )

        self.kitty.stamp_tab(self.kitty.tab, owned.manifest)
        self.assertIsNone(self.service.unowned_tabs_info())

    def test_open_can_name_and_save_unowned_tabs_without_mixing(self) -> None:
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
            UnownedTabsDecision(
                UnownedTabsAction.SAVE_SEPARATELY,
                "Focused research",
            ),
        )

        sessions = self.store.list()
        auto = next(session for session in sessions if session.manifest.id != target.manifest.id)
        self.assertEqual(auto.manifest.name, "Focused research")
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

    def test_open_can_discard_only_the_confirmed_unowned_tabs_after_activation(self) -> None:
        target = self.store.create("Live target", "/tmp/target")
        target_tab = LiveTab(
            1,
            8,
            2,
            "Target",
            "splits",
            [{"id": 12, "cwd": "/tmp/target", "user_vars": {}}],
        )
        scratch_tab = LiveTab(
            1,
            9,
            1,
            "Scratch",
            "splits",
            [{"id": 13, "cwd": "/tmp/scratch", "user_vars": {}}],
        )
        self.kitty.stamp_tab(target_tab, target.manifest)
        self.kitty.extra_tabs.extend((scratch_tab, target_tab))

        opened = self.service.open(
            target.manifest.id,
            UnownedTabsDecision(UnownedTabsAction.DISCARD),
        )

        self.assertEqual(opened.manifest.id, target.manifest.id)
        self.assertEqual(self.kitty.closed_tabs, [self.kitty.tab.tab_id, scratch_tab.tab_id])
        self.assertEqual(
            self.kitty.activated_sessions[-1],
            (target.manifest.id, target_tab.tab_id),
        )
        self.assertEqual([tab.tab_id for tab in self.kitty.tabs()], [target_tab.tab_id])
        self.assertEqual(
            [session.manifest.id for session in self.store.list()], [target.manifest.id]
        )

    def test_unowned_decision_validation_and_failed_open_never_discard_tabs(self) -> None:
        target = self.store.create("Broken target", "/tmp/target")
        snapshot = sanitize_session("new_tab Broken\nlaunch\n", target.manifest)
        self.store.write_snapshot(
            target.manifest.id,
            snapshot,
            snapshot_summary(snapshot),
        )

        with self.assertRaisesRegex(KiSeshError, "only save-separately accepts"):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.ATTACH, "invalid"),
            )
        with self.assertRaisesRegex(KiSeshError, "name cannot be empty"):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.SAVE_SEPARATELY, " "),
            )
        with self.assertRaisesRegex(SessionConflict, "session name already exists"):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(
                    UnownedTabsAction.SAVE_SEPARATELY,
                    "broken TARGET!",
                ),
            )
        with (
            mock.patch.object(
                self.service,
                "_open_inactive_snapshot",
                side_effect=KiSeshError("restore failed"),
            ),
            self.assertRaisesRegex(KiSeshError, "restore failed"),
        ):
            self.service.open(
                target.manifest.id,
                UnownedTabsDecision(UnownedTabsAction.DISCARD),
            )

        self.assertEqual(self.kitty.closed_tabs, [])
        self.assertTrue(self.kitty.include_tab)
        self.assertIsNone(self.kitty.tab.session_id())
        self.assertEqual([stored.manifest.id for stored in self.store.list()], [target.manifest.id])

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

        with self.assertRaisesRegex(KiSeshError, "open the saved session"):
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

        with self.assertRaisesRegex(KiSeshError, "only live tab"):
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

        with self.assertRaisesRegex(KiSeshError, "saved target session"):
            self.service.copy_current_tab(target.manifest.id)

    def test_unarchive_returns_a_session_to_the_active_store(self) -> None:
        stored = self.store.create("Dormant Project", "/tmp/dormant")
        archived = self.service.archive(stored.manifest.id)

        restored = self.service.unarchive(archived.manifest.id)

        self.assertEqual(restored.manifest.status, "active")
        self.assertEqual(restored.directory.parent, self.store.sessions_dir)

    def test_live_session_cannot_be_archived(self) -> None:
        stored = self.service.create_from_active("Active Project")

        with self.assertRaisesRegex(KiSeshError, "^live sessions cannot be archived$"):
            self.service.archive(stored.manifest.id)

        self.assertEqual(self.store.get(stored.manifest.id).manifest.status, "active")

    def test_remove_refuses_live_and_preserves_saved_and_archived_payloads_in_trash(self) -> None:
        live = self.service.create_from_active("Live Project")
        with self.assertRaisesRegex(KiSeshError, "^live sessions cannot be removed$"):
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
