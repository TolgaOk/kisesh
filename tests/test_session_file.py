from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path

from kitty_workbench.model import (
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    WORKBENCH_UI_VAR,
    SessionManifest,
)
from kitty_workbench.session_file import (
    _cd_working_directory,
    _is_workbench_ui_launch,
    _launch_working_directories,
    _parse_launch,
    _sanitize_blob,
    clean_tab_title,
    read_session,
    rename_snapshot_tab,
    sanitize_launch_line,
    sanitize_session,
    snapshot_summary,
)

RAW_SESSION = (
    "new_os_window\n"
    "os_window_size 140 44\n"
    "os_window_title unsafe title\n"
    "new_tab Agent work\n"
    "layout splits\n"
    "enabled_layouts splits,stack\n"
    'set_layout_state {"opts": {}, "window_groups": []}\n'
    "cd /tmp/project\n"
    "launch --cwd=/tmp/project --env SECRET=value --copy-env --var=ksm=old "
    '\'kitty-unserialize-data={"cmd_at_shell_startup":"claude --continue",'
    '"window_id":11}\' claude --continue\n'
    "focus\n"
    "new_tab Git\n"
    "cd /var/tmp\n"
    "launch --location=vsplit --bias=40 --title 'Git pane' "
    "--var CUSTOM=ok lazygit --filter=tree\n"
    "focus_tab\n"
)


class SessionFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = SessionManifest(
            name="Workbench",
            slug="workbench",
            project_root="/tmp/project",
            id="12345678-1234-4234-8234-123456789abc",
        )

    def test_sanitizer_preserves_layout_but_never_replays_processes(self) -> None:
        safe = sanitize_session(RAW_SESSION, self.manifest)

        self.assertNotIn("new_os_window", safe)
        self.assertNotIn("os_window_", safe)
        self.assertEqual(safe.count("new_tab "), 2)
        self.assertIn("layout splits", safe)
        self.assertIn("set_layout_state", safe)
        self.assertIn("--cwd=/tmp/project", safe)
        self.assertIn("--location=vsplit", safe)
        self.assertIn("CUSTOM=ok", safe)

        for forbidden in ("SECRET", "copy-env", "cmd_at_shell_startup", "claude", "lazygit"):
            self.assertNotIn(forbidden, safe)

        launch_lines = [line for line in safe.splitlines() if line.startswith("launch")]
        self.assertEqual(len(launch_lines), 2)
        for line in launch_lines:
            tokens = shlex.split(line)
            self.assertIn(f"--var={SESSION_ID_VAR}={self.manifest.id}", tokens)
            self.assertIn(f"--var={SESSION_SLUG_VAR}=workbench", tokens)
            self.assertIn(f"--var={SESSION_NAME_VAR}=Workbench", tokens)
        self.assertEqual(sanitize_session(safe, self.manifest), safe)

    def test_unreadable_serialization_blob_is_dropped(self) -> None:
        line = sanitize_launch_line("launch 'kitty-unserialize-data={bad' dangerous", self.manifest)
        self.assertNotIn("unserialize", line)
        self.assertNotIn("dangerous", line)

    def test_behavioral_launch_options_are_not_replayed(self) -> None:
        unsafe = (
            "launch --watcher /tmp/code.py --stdin-source=@screen "
            "--remote-control-password=secret --allow-remote-control shell-command"
        )
        line = sanitize_launch_line(unsafe, self.manifest)
        forbidden_options = (
            "watcher",
            "code.py",
            "stdin",
            "password",
            "remote-control",
            "shell-command",
        )
        for forbidden in forbidden_options:
            self.assertNotIn(forbidden, line)

    def test_unstamped_copied_shell_does_not_claim_a_missing_session(self) -> None:
        safe = sanitize_session(
            "new_tab Scratch\nlaunch yazi\n",
            self.manifest,
            stamp_ownership=False,
        )
        self.assertEqual(safe, "new_tab Scratch\nlaunch\n")

    def test_transient_manager_is_removed_without_inflating_snapshot_panes(self) -> None:
        raw = (
            "new_tab Shell\n"
            "layout stack\n"
            'set_layout_state {"pairs":{"one":1,"two":2}}\n'
            f"launch 'kitty-unserialize-data={{\"id\":1}}' "
            f"--var={SESSION_SCOPE_VAR}=7\n"
            f"launch 'kitty-unserialize-data={{\"id\":2}}' "
            f"--var={WORKBENCH_UI_VAR}=yes --title=Workbench\n"
            "focus\n"
            "new_tab Editor\n"
            "layout splits\n"
            'set_layout_state {"pairs":{"one":3}}\n'
            f"launch 'kitty-unserialize-data={{\"id\":3}}' "
            f"--var {WORKBENCH_UI_VAR}=no --var={SESSION_SCOPE_VAR}=7\n"
            "new_tab Workbench\n"
            "layout splits\n"
            f"launch 'kitty-unserialize-data={{\"id\":4}}' "
            f"--var={WORKBENCH_UI_VAR}=yes --title=Workbench\n"
            "focus_tab\n"
        )

        safe = sanitize_session(raw, self.manifest)

        self.assertNotIn(WORKBENCH_UI_VAR, safe)
        self.assertNotIn(SESSION_SCOPE_VAR, safe)
        self.assertNotIn('{"id":2}', safe)
        self.assertIn('{"id":1}', safe)
        self.assertIn('{"id":3}', safe)
        self.assertNotIn('{"id":4}', safe)
        self.assertNotIn("new_tab Workbench", safe)
        self.assertEqual(safe.count("set_layout_state"), 1)
        summary = snapshot_summary(safe)
        self.assertEqual(summary.tab_count, 2)
        self.assertEqual(summary.pane_count, 2)
        self.assertEqual(sanitize_session(safe, self.manifest), safe)
        self.assertTrue(_is_workbench_ui_launch("launch --var kitty_workbench_ui=YES"))
        self.assertFalse(_is_workbench_ui_launch("launch --var=kitty_workbench_ui=false"))

    def test_missing_structure_gets_a_shell_tab(self) -> None:
        self.manifest.name = "Workbench Session"
        safe = sanitize_session("layout stack\n", self.manifest)
        self.assertTrue(safe.startswith("new_tab Workbench Session\n"))
        self.assertNotIn("'Workbench Session'", safe)
        self.assertIn("launch", safe)

    def test_summary_and_root_cover_multiple_tabs(self) -> None:
        safe = sanitize_session(RAW_SESSION, self.manifest)
        summary = snapshot_summary(safe)
        self.assertEqual(summary.tab_count, 2)
        self.assertEqual(summary.pane_count, 2)
        self.assertEqual(summary.tab_titles, ["Agent work", "Git"])
        self.assertEqual(summary.working_directories, ["/tmp/project", "/var/tmp"])

    def test_tab_rename_changes_only_the_indexed_safe_directive(self) -> None:
        """Preserve layout bytes around a normalized, single-line title change."""
        snapshot = "new_table untouched\nnew_tab First\nlaunch\n  new_tab Second\nlaunch\n"

        renamed = rename_snapshot_tab(snapshot, 1, "  New\n\tTitle  ")

        self.assertEqual(
            renamed,
            "new_table untouched\nnew_tab First\nlaunch\n  new_tab New Title\nlaunch\n",
        )
        self.assertEqual(clean_tab_title("  One   title  "), "One title")
        self.assertEqual(rename_snapshot_tab("new_tab Old", 0, "New"), "new_tab New")
        for index in (-1, 2):
            with self.subTest(index=index), self.assertRaisesRegex(IndexError, "outside"):
                rename_snapshot_tab(snapshot, index, "New")
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            clean_tab_title("\n\t\x00")

    def test_kitty_cd_paths_with_spaces_are_not_truncated(self) -> None:
        session = "new_tab Notes\ncd /Users/me/Library/Mobile Documents/Vault\nlaunch\n"
        summary = snapshot_summary(session)
        self.assertEqual(summary.working_directories, ["/Users/me/Library/Mobile Documents/Vault"])

    def test_launch_sanitizer_covers_inline_flags_values_and_missing_values(self) -> None:
        line = sanitize_launch_line(
            "launch --cwd=/tmp --copy-colors --env=SECRET=yes "
            "--var=CUSTOM=ok --var=kitty_workbench_session=stale --var",
            self.manifest,
        )
        tokens = shlex.split(line)

        self.assertIn("--cwd=/tmp", tokens)
        self.assertIn("--copy-colors", tokens)
        self.assertIn("--var=CUSTOM=ok", tokens)
        self.assertNotIn("SECRET", line)
        self.assertNotIn("stale", line)
        self.assertEqual(tokens.count(f"--var={SESSION_ID_VAR}={self.manifest.id}"), 1)

    def test_malformed_grammar_falls_back_to_an_inert_shell(self) -> None:
        malformed = sanitize_launch_line("launch 'unterminated", self.manifest)
        self.assertNotIn("unterminated", malformed)
        self.assertIn(f"--var={SESSION_ID_VAR}={self.manifest.id}", malformed)
        with self.assertRaisesRegex(ValueError, "expected a Kitty launch line"):
            _parse_launch("title not-a-launch")

    def test_serialized_window_blob_keeps_only_its_inert_id(self) -> None:
        raw = 'kitty-unserialize-data={"id":11,"cmd_at_shell_startup":"top"}'
        safe = _sanitize_blob(raw)

        self.assertEqual(safe, 'kitty-unserialize-data={"id":11}')
        self.assertEqual(_sanitize_blob("ordinary-token"), "ordinary-token")
        self.assertEqual(_sanitize_blob("kitty-unserialize-data=[]"), "")
        self.assertEqual(_sanitize_blob("kitty-unserialize-data={}"), "")

    def test_launch_before_tab_and_blank_edges_normalize_to_one_complete_snapshot(self) -> None:
        safe = sanitize_session("\n\nlaunch --cwd /tmp/project\n\n", self.manifest)

        self.assertTrue(safe.startswith("new_tab Workbench\n"))
        self.assertEqual(safe.count("launch"), 1)
        self.assertTrue(safe.endswith("\n"))
        self.assertFalse(safe.startswith("\n"))
        self.assertFalse(safe.endswith("\n\n"))

    def test_summary_tolerates_malformed_cwd_quoting_and_deduplicates_paths(self) -> None:
        self.assertEqual(_launch_working_directories("launch --cwd 'unterminated"), [])
        self.assertEqual(
            _launch_working_directories("launch --cwd /tmp --cwd=/tmp --cwd"),
            ["/tmp", "/tmp"],
        )
        self.assertEqual(_cd_working_directory("cd 'unterminated"), "'unterminated")
        summary = snapshot_summary("new_tab\nlaunch --cwd /tmp\nlaunch --cwd=/tmp\ncd /tmp\n")
        self.assertEqual(summary.tab_titles, ["untitled"])
        self.assertEqual(summary.working_directories, ["/tmp"])

    def test_session_reader_uses_utf8_without_interpreting_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.kitty-session"
            path.write_text("new_tab Récit\nlaunch\n", encoding="utf-8")
            self.assertEqual(read_session(path), "new_tab Récit\nlaunch\n")


if __name__ == "__main__":
    unittest.main()
