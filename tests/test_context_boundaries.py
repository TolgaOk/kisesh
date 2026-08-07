from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from unittest import mock

from kisesh.app_profiles import DEFAULT_APP_PROFILES
from kisesh.context import (
    ARGUMENT_COUNT_LIMIT,
    ARGUMENT_LENGTH_LIMIT,
    TERMINAL_HISTORY_CHARACTER_LIMIT,
    _append_history,
    _bounded_terminal_history,
    _claude_resume,
    _codex_resume,
    _command_argv,
    _command_name,
    _event_time,
    _history,
    _restore_command,
    build_context,
    merge_context,
    normalize_command_event,
    pane_alternate_screen_text,
    pane_command_history,
    pane_last_command_output,
    pane_terminal_history,
    pending_restore_commands,
    remap_context_windows,
    rename_context_tab,
    restore_session,
    update_context_for_closing_pane,
)
from kisesh.domain import ClosingPaneCapture, CommandRecord, KittyWindow
from kisesh.kitty_client import LiveTab


def _tab(*windows: Mapping[str, object], tab_id: int = 7) -> LiveTab:
    return LiveTab(
        1,
        tab_id,
        0,
        "Work",
        "splits",
        [cast(KittyWindow, dict(window)) for window in windows],
        is_focused=True,
        is_active=True,
    )


def _shell(window_id: int = 11) -> dict[str, object]:
    return {
        "id": window_id,
        "title": "Shell",
        "cwd": "/tmp/project",
        "foreground_processes": [{"cmdline": ["-zsh"]}],
        "at_prompt": True,
    }


class ContextBoundaryTests(unittest.TestCase):
    def test_untrusted_command_arguments_are_bounded_and_never_raise(self) -> None:
        oversized = "x" * (ARGUMENT_LENGTH_LIMIT + 10)
        sequence = ["command", "", oversized, *[str(index) for index in range(100)]]

        parsed = _command_argv(sequence)

        self.assertEqual(parsed[0], "command")
        self.assertEqual(len(parsed[1]), ARGUMENT_LENGTH_LIMIT)
        self.assertLessEqual(len(parsed), ARGUMENT_COUNT_LIMIT - 1)
        self.assertEqual(_command_argv("command 'two words'"), ["command", "two words"])
        self.assertEqual(_command_argv("unterminated '"), [])
        self.assertEqual(_command_argv(7), [])
        self.assertIsNone(_command_name([]))
        self.assertIsNone(_command_name(["/"]))

    def test_event_times_accept_epoch_aware_and_naive_iso_with_safe_fallback(self) -> None:
        epoch = 1_725_190_200.25
        expected_epoch = (
            datetime.fromtimestamp(epoch, UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        with mock.patch("kisesh.context.utc_now", return_value="fallback"):
            self.assertEqual(_event_time(epoch), expected_epoch)
            self.assertEqual(
                _event_time("2026-08-04T14:30:00+03:00"),
                "2026-08-04T11:30:00.000000Z",
            )
            self.assertEqual(
                _event_time("2026-08-04T11:30:00"),
                "2026-08-04T11:30:00.000000Z",
            )
            self.assertEqual(_event_time("not-a-time"), "fallback")
            self.assertEqual(_event_time(True), "fallback")
            self.assertEqual(_event_time(10**1000), "fallback")

    def test_event_and_history_validation_drop_corrupt_records_but_keep_metadata(self) -> None:
        self.assertIsNone(normalize_command_event({"window_id": "not-an-id", "command": "pwd"}))
        self.assertIsNone(normalize_command_event({"window_id": 11, "command": ""}))
        event = normalize_command_event(
            {
                "window_id": "11",
                "cmdline": ["git", "status"],
                "time": "2026-08-04T11:30:00Z",
                "cwd": "/tmp/project\x00",
                "exit_status": "0",
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["command"], "git status")
        self.assertEqual(event["cwd"], "/tmp/project")
        self.assertEqual(event["exit_status"], 0)

        history = _history(
            [
                "bad",
                {"command": ""},
                {"command": "pwd", "completed_at": "bad", "exit_status": True},
                {
                    "command": ["git", "status"],
                    "completed_at": "2026-08-04T11:31:00Z",
                    "cwd": "/tmp/project",
                    "exit_status": "1",
                },
            ]
        )
        self.assertEqual([entry["command"] for entry in history], ["pwd", "git status"])
        self.assertNotIn("cwd", history[0])
        self.assertNotIn("exit_status", history[0])
        self.assertEqual(history[1]["cwd"], "/tmp/project")
        self.assertEqual(history[1]["exit_status"], 1)

    def test_history_append_ignores_empty_and_duplicate_events(self) -> None:
        history: list[CommandRecord] = []
        _append_history(history, {"command": ""}, "")
        self.assertEqual(history, [])
        event = {"command": "pwd", "completed_at": "2026-08-04T11:30:00Z"}
        _append_history(history, event, "")
        _append_history(
            history,
            {"command": "ls", "completed_at": "2026-08-04T11:31:00Z"},
            "",
        )
        _append_history(history, event, "")
        self.assertEqual([entry["command"] for entry in history], ["pwd", "ls"])
        self.assertNotIn("cwd", history[0])

    def test_agent_resume_variants_reduce_to_stable_safe_commands(self) -> None:
        claude = DEFAULT_APP_PROFILES.match("claude-nightly")
        self.assertIsNotNone(claude)
        self.assertTrue(claude.agent if claude is not None else False)
        self.assertIsNone(DEFAULT_APP_PROFILES.match("python"))
        self.assertEqual(_claude_resume(["claude", "--resume=abc"]), ["claude", "--resume", "abc"])
        self.assertEqual(_claude_resume(["claude", "-r", "abc"]), ["claude", "--resume", "abc"])
        self.assertEqual(
            _claude_resume(["claude", "--session-id", "abc"]),
            ["claude", "--resume", "abc"],
        )
        self.assertEqual(_claude_resume(["claude", "-r", "--flag"]), ["claude", "--resume"])
        self.assertEqual(_claude_resume(["claude", "-c"]), ["claude", "--continue"])
        self.assertEqual(_claude_resume(["claude"]), ["claude"])
        self.assertEqual(_codex_resume(["codex"]), ["codex"])
        self.assertEqual(
            _codex_resume(["codex", "resume", "--last"]),
            ["codex", "resume", "--last"],
        )
        self.assertEqual(
            _codex_resume(["codex", "resume", "session-id"]),
            ["codex", "resume", "session-id"],
        )
        self.assertEqual(
            _codex_resume(["codex", "resume", "--sandbox"]),
            ["codex", "resume"],
        )
        self.assertIsNone(
            _restore_command(
                ["-zsh"],
                profile=None,
                default_restore=DEFAULT_APP_PROFILES.defaults.restore,
            )
        )
        unknown = _restore_command(
            ["python", "server.py"],
            profile=None,
            default_restore=DEFAULT_APP_PROFILES.defaults.restore,
        )
        self.assertIsNotNone(unknown)
        assert unknown is not None
        self.assertFalse(unknown["auto_run"])

    def test_terminal_capture_enforces_character_limit_with_and_without_newline(self) -> None:
        with_newline = "x" * 600_000 + "\n" + "y" * 600_000
        without_newline = "z" * (TERMINAL_HISTORY_CHARACTER_LIMIT + 10)

        bounded = _bounded_terminal_history(with_newline)
        single_line = _bounded_terminal_history(without_newline)

        self.assertTrue(bounded.truncated)
        self.assertLess(len(bounded.text), TERMINAL_HISTORY_CHARACTER_LIMIT)
        self.assertTrue(bounded.text.startswith("y"))
        self.assertTrue(single_line.truncated)
        self.assertEqual(len(single_line.text), TERMINAL_HISTORY_CHARACTER_LIMIT)
        self.assertEqual(_bounded_terminal_history(7).text, "")

    def test_terminal_capture_keeps_spaceship_colors_but_drops_active_controls(self) -> None:
        """Retain SGR prompt styling without replaying cursor or clipboard commands."""
        orange = "\x1b[38;2;245;130;65m"
        blue_background = "\x1b[48:2::90:170:215m"
        reset = "\x1b[0m"
        styled_prompt = f"{orange} ~/dotfiles{blue_background}  main {reset}\u276f"
        hyperlink_open = "\x1b]8;;file:///Users/tok/dotfiles\x1b\\"
        hyperlink_close = "\x1b]8;;\x1b\\"
        captured = (
            f"{hyperlink_open}{styled_prompt}{hyperlink_close}"
            "\x1b[2J\x1b]52;c;must-not-reach-the-clipboard\x07\x01 ls\n"
        )

        bounded = _bounded_terminal_history(captured)

        self.assertEqual(bounded.text, f"{styled_prompt} ls\n")
        self.assertIn(orange, bounded.text)
        self.assertIn(blue_background, bounded.text)
        self.assertNotIn("must-not-reach-the-clipboard", bounded.text)
        self.assertNotIn("\x1b[2J", bounded.text)

    def test_terminal_capture_drops_only_unused_trailing_screen_rows(self) -> None:
        """Keep meaningful spacing while removing blank rows below the final content."""
        green = "\x1b[32m"
        reset = "\x1b[0m"
        captured = f"{green}prompt{reset}\n\ncommand output\n" + "\n" * 12 + f"{reset}   \t"

        bounded = _bounded_terminal_history(captured)

        self.assertEqual(
            bounded.text,
            f"{green}prompt{reset}\n\ncommand output\n",
        )
        self.assertFalse(bounded.truncated)
        self.assertEqual(_bounded_terminal_history(f"{reset}\n\n").text, "")

    def test_corrupt_prior_panes_are_ignored_while_valid_position_state_survives(self) -> None:
        prior: dict[str, object] = {
            "tabs": [
                "bad-tab",
                {
                    "title": "Work",
                    "layout": "splits",
                    "focused": True,
                    "panes": [
                        "bad-pane",
                        {
                            "window_id": "bad-id",
                            "command_history": [
                                {
                                    "command": "pwd",
                                    "completed_at": "2026-08-04T11:30:00Z",
                                }
                            ],
                        },
                    ],
                },
            ]
        }
        shell = _shell(99)
        shell["last_focused_at"] = 12.5
        context = build_context([_tab(shell)], prior, command_events=[{"bad": "event"}])

        pane = context["tabs"][0]["panes"][0]
        self.assertEqual(pane["last_focused_at"], 12.5)
        self.assertEqual(pane["command_history"], [])

        layered = _shell(100)
        layered["foreground_processes"] = [
            {"cmdline": ["-zsh"]},
            {"cmdline": []},
        ]
        layered_context = build_context([_tab(layered)])
        self.assertEqual(layered_context["tabs"][0]["panes"][0]["program"], "zsh")

        empty_prompt = _shell(101)
        empty_prompt["foreground_processes"] = []
        empty_prompt_context = build_context([_tab(empty_prompt)])
        self.assertIsNone(empty_prompt_context["tabs"][0]["panes"][0]["program"])

    def test_remap_and_close_use_identity_then_position_and_reject_missing_panes(self) -> None:
        original = build_context([_tab(_shell(11))])
        extra_live_tab = _tab(_shell(99), tab_id=8)
        remapped = remap_context_windows(original, [_tab(_shell(55)), extra_live_tab])
        self.assertEqual(remapped["tabs"][0]["panes"][0]["window_id"], 55)

        fallback_capture = cast(
            ClosingPaneCapture,
            {
                "tab_index": 0,
                "pane_index": 0,
                "window": {**_shell(77), "last_reported_cmdline": "pwd"},
                "terminal_history": "pwd\n/tmp/project\n",
                "alternate_screen_text": "",
                "last_command_output": "/tmp/project\n",
                "command_events": [],
            },
        )
        closed = update_context_for_closing_pane(original, fallback_capture)
        self.assertEqual(closed["tabs"][0]["panes"][0]["window_id"], 77)

        missing_capture = cast(
            ClosingPaneCapture,
            {**fallback_capture, "tab_index": 8, "pane_index": 9},
        )
        with self.assertRaisesRegex(ValueError, "absent from the saved session"):
            update_context_for_closing_pane(None, missing_capture)

        original["snapshot_revision"] = 4
        renamed = rename_context_tab(original, 0, "Renamed tab")
        self.assertIsNotNone(renamed)
        assert renamed is not None
        self.assertEqual(renamed["tabs"][0]["title"], "Renamed tab")
        self.assertEqual(renamed["snapshot_revision"], 4)
        self.assertNotEqual(original["tabs"][0]["title"], "Renamed tab")
        self.assertIsNone(rename_context_tab(None, 0, "Unused"))
        with self.assertRaisesRegex(IndexError, "saved context"):
            rename_context_tab(original, 3, "Missing")

    def test_public_context_readers_tolerate_missing_indexes_and_legacy_output(self) -> None:
        legacy: dict[str, object] = {
            "tabs": [{"panes": [{"last_command_output": "legacy output\n"}]}]
        }
        self.assertEqual(pane_terminal_history(legacy, 0, 0), "legacy output\n")
        self.assertEqual(pane_last_command_output(None, 0, 0), "")
        self.assertEqual(pane_terminal_history(legacy, 4, 0), "")
        self.assertEqual(pane_terminal_history(legacy, 0, 4), "")
        self.assertEqual(pane_alternate_screen_text(None, 0, 0), "")
        self.assertEqual(pane_command_history(None, 0, 0), [])
        self.assertEqual(merge_context(None, None)["tabs"], [])

    def test_pending_reminders_reject_bad_locations_controls_and_empty_commands(self) -> None:
        context: dict[str, object] = {
            "restore_commands": [
                "bad",
                {"auto_run": True, "tab_index": 0, "pane_index": 0, "command": "top"},
                {"tab_index": "bad", "pane_index": 0, "command": "pwd"},
                {"tab_index": 0, "pane_index": 1, "command": ""},
                {"tab_index": 0, "pane_index": 2, "argv": ["python", "server.py"]},
                {"tab_index": 0, "pane_index": 3, "command": "\x01\x7f"},
            ]
        }

        self.assertEqual(pending_restore_commands(context), {(0, 2): "python server.py"})

    def test_restore_ignores_malformed_candidates_and_launch_lines(self) -> None:
        context: dict[str, object] = {
            "restore_commands": [
                "bad",
                {"auto_run": False, "tab_index": 0, "pane_index": 0, "argv": ["top"]},
                {"auto_run": True, "tab_index": "bad", "pane_index": 0, "argv": ["top"]},
                {"auto_run": True, "tab_index": 0, "pane_index": 0, "argv": []},
                {"auto_run": True, "tab_index": 0, "pane_index": 0, "argv": ["top"]},
                {"auto_run": True, "tab_index": 0, "pane_index": 1, "argv": ["top"]},
                {"auto_run": True, "tab_index": 0, "pane_index": 2, "argv": ["top"]},
            ]
        }
        malformed_snapshot = (
            "new_tab Work\nlauncher --not-a-launch\nlaunch 'unterminated\nlaunch --cwd=/tmp\n"
        )

        restored = restore_session(malformed_snapshot, context)

        self.assertIn("launch 'unterminated", restored)
        self.assertIn("launcher --not-a-launch", restored)
        self.assertIn("launch --cwd=/tmp", restored)
        self.assertEqual(restore_session("launch --cwd=/tmp\n", None), "launch --cwd=/tmp\n")
        self.assertEqual(restore_session("title Work\n", context), "title Work\n")


if __name__ == "__main__":
    unittest.main()
