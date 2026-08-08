from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import cast

from kisesh.context import (
    COMMAND_HISTORY_LIMIT,
    LAST_COMMAND_OUTPUT_LIMIT,
    TERMINAL_HISTORY_LINE_LIMIT,
    build_context,
    merge_context,
    pane_alternate_screen_text,
    pane_auto_run_argv,
    pane_last_command_output,
    pane_terminal_history,
    pending_restore_commands,
    remap_context_windows,
    restore_session,
    update_context_for_closing_pane,
)
from kisesh.kitty_client import LiveTab
from kisesh.model import AGENT_SESSION_VAR, ClosingPaneCapture, KittyWindow


def _tab(*windows: Mapping[str, object], title: str = "Work") -> LiveTab:
    return LiveTab(
        os_window_id=1,
        tab_id=7,
        index=0,
        title=title,
        layout="splits",
        windows=[cast(KittyWindow, dict(window)) for window in windows],
        is_focused=True,
        is_active=True,
    )


class ContextTests(unittest.TestCase):
    def test_hook_session_marker_replaces_and_survives_later_agent_autosaves(self) -> None:
        first_id = "7f676817-c49e-459c-86de-17382e2170ef"
        second_id = "a76d8108-9f50-449c-b30f-e3cdb5eac4a4"
        window = {
            "id": 11,
            "title": "Claude",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["claude"], "pid": 991}],
            "user_vars": {AGENT_SESSION_VAR: first_id},
            "at_prompt": False,
        }

        first = build_context([_tab(window)])
        window["user_vars"] = {AGENT_SESSION_VAR: second_id}
        resumed = build_context([_tab(window)], first)
        window["user_vars"] = {}
        later_autosave = build_context([_tab(window)], resumed)

        self.assertEqual(
            first["restore_commands"][0]["argv"],
            ["claude", "--resume", first_id],
        )
        self.assertEqual(
            resumed["restore_commands"][0]["argv"],
            ["claude", "--resume", second_id],
        )
        self.assertEqual(
            later_autosave["restore_commands"][0]["argv"],
            ["claude", "--resume", second_id],
        )

    def test_capture_keeps_completed_history_and_builds_agent_resume_command(self) -> None:
        claude = {
            "id": 11,
            "title": "Claude review",
            "cwd": "/tmp/project",
            "foreground_processes": [
                {
                    "cmdline": [
                        "/usr/local/bin/claude",
                        "--resume",
                        "session-123",
                        "--dangerously-skip-permissions",
                    ],
                    "cwd": "/tmp/project",
                    "pid": 991,
                }
            ],
            "env": {"ANTHROPIC_API_KEY": "must-not-copy-environment"},
            "screen_text": "must-not-copy-terminal-output",
            "scrollback": ["must-not-copy-scrollback"],
            "in_alternate_screen": True,
            "at_prompt": False,
        }
        shell = {
            "id": 12,
            "title": "Tests",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"], "pid": 992}],
            "last_reported_cmdline": "pytest -q",
            "last_cmd_exit_status": 0,
            "at_prompt": True,
        }
        context = build_context(
            [_tab(claude, shell)],
            command_events=[
                {
                    "window_id": 12,
                    "command": "pytest -q",
                    "completed_at": "2026-08-04T11:30:00Z",
                }
            ],
        )

        self.assertEqual(context["command_count"], 1)
        shell_context = context["tabs"][0]["panes"][1]
        self.assertEqual(shell_context["last_command"], "pytest -q")
        self.assertEqual(shell_context["command_history"][0]["exit_status"], 0)
        restore = context["restore_commands"][0]
        self.assertEqual(restore["argv"], ["claude", "--resume", "session-123"])
        self.assertTrue(restore["auto_run"])

        encoded = json.dumps(context)
        self.assertNotIn("ANTHROPIC_API_KEY", encoded)
        self.assertNotIn("must-not-copy-environment", encoded)
        self.assertNotIn("must-not-copy-terminal-output", encoded)
        self.assertNotIn("must-not-copy-scrollback", encoded)
        self.assertNotIn('"pid"', encoded)

    def test_completed_one_shot_command_is_history_never_startup_code(self) -> None:
        window = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "deploy --production",
            "last_cmd_exit_status": 0,
            "at_prompt": True,
        }
        context = build_context([_tab(window)])
        snapshot = "new_tab Work\nlaunch --cwd=/tmp/project\n"

        self.assertEqual(context["tabs"][0]["panes"][0]["last_command"], "deploy --production")
        self.assertEqual(context["restore_commands"], [])
        self.assertEqual(restore_session(snapshot, context), snapshot)

    def test_restore_replays_current_apps_directly_in_their_matching_panes(self) -> None:
        codex = {
            "id": 11,
            "title": "Codex",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["codex"]}],
            "at_prompt": False,
        }
        editor = {
            "id": 12,
            "title": "Editor",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["nvim", "."]}],
            "in_alternate_screen": True,
            "at_prompt": False,
        }
        context = build_context([_tab(codex, editor)])
        snapshot = (
            "new_tab Work\n"
            "layout splits\n"
            "launch --cwd=/tmp/project --var=example=yes\n"
            "launch --cwd=/tmp/project\n"
        )

        restored = restore_session(snapshot, context)

        self.assertIn("--var=example=yes codex", restored)
        self.assertNotIn("resume --last", restored)
        self.assertIn("nvim .", restored)
        self.assertEqual(restored.count("launch "), 2)

    def test_history_is_bounded_and_survives_a_new_kitty_window_id(self) -> None:
        first = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "at_prompt": True,
        }
        events = [
            {
                "window_id": 11,
                "command": f"command-{index}",
                "completed_at": f"2026-08-04T11:30:{index % 60:02d}Z",
            }
            for index in range(COMMAND_HISTORY_LIMIT + 10)
        ]
        original = build_context([_tab(first)], command_events=events)
        reopened = {**first, "id": 99}

        updated = build_context([_tab(reopened)], original)
        history = updated["tabs"][0]["panes"][0]["command_history"]

        self.assertEqual(len(history), COMMAND_HISTORY_LIMIT)
        self.assertEqual(history[0]["command"], "command-10")
        self.assertEqual(history[-1]["command"], f"command-{COMMAND_HISTORY_LIMIT + 9}")

    def test_last_completed_output_is_plain_bounded_and_stays_with_its_pane(self) -> None:
        first = {
            "id": 11,
            "title": "Build",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "make test",
            "at_prompt": True,
        }
        second = {
            "id": 12,
            "title": "Git",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "git status",
            "at_prompt": True,
        }
        oversized = "discarded-prefix\n" + "x" * (LAST_COMMAND_OUTPUT_LIMIT + 20)
        context = build_context(
            [_tab(first, second)],
            command_outputs={
                11: oversized,
                12: "clean\x1b]52;c;clipboard-attack\x07\nworking tree clean\n",
            },
        )

        first_pane, second_pane = context["tabs"][0]["panes"]
        self.assertEqual(len(first_pane["last_command_output"] or ""), LAST_COMMAND_OUTPUT_LIMIT)
        self.assertTrue(first_pane["last_command_output_truncated"])
        self.assertNotIn("discarded-prefix", first_pane["last_command_output"] or "")
        self.assertEqual(second_pane["last_output_command"], "git status")
        self.assertEqual(
            pane_last_command_output(context, 0, 1),
            "clean]52;c;clipboard-attack\nworking tree clean\n",
        )
        self.assertNotIn("\x1b", json.dumps(context))
        self.assertNotIn("\x07", json.dumps(context))

    def test_shell_state_uses_an_inert_restorer_and_never_replays_history(self) -> None:
        first = {
            "id": 11,
            "title": "One",
            "cwd": "/tmp/one",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "touch /tmp/must-not-run",
            "at_prompt": True,
        }
        second = {
            "id": 12,
            "title": "Two",
            "cwd": "/tmp/two",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "printf second",
            "at_prompt": True,
        }
        context = build_context(
            [_tab(first, second)],
            command_outputs={11: "first output\n", 12: "second output\n"},
        )
        snapshot = "new_tab Work\nlayout splits\nlaunch --cwd=/tmp/one\nlaunch --cwd=/tmp/two\n"

        restored = restore_session(
            snapshot,
            context,
            shell_restore_argv=["/kisesh", "restore-shell", "session-id"],
        )

        launch_lines = [line for line in restored.splitlines() if line.startswith("launch")]
        self.assertEqual(len(launch_lines), 2)
        self.assertIn("/kisesh restore-shell session-id", launch_lines[0])
        self.assertIn("--tab-index 0 --pane-index 0", launch_lines[0])
        self.assertIn("--tab-index 0 --pane-index 1", launch_lines[1])
        self.assertNotIn("touch /tmp/must-not-run", restored)
        self.assertNotIn("printf second", restored)
        self.assertNotIn("first output", restored)
        self.assertNotIn("second output", restored)

    def test_scrollback_and_last_output_survive_an_empty_second_save(self) -> None:
        first = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "make test",
            "at_prompt": True,
        }
        scrollback = (
            "".join(f"line-{index:04d}\n" for index in range(TERMINAL_HISTORY_LINE_LIMIT + 10))
            + "unsafe\x1b]52;c;payload\x07\n"
        )
        original = build_context(
            [_tab(first)],
            command_outputs={11: "tests passed\n"},
            terminal_histories={11: scrollback},
        )
        saved_history = pane_terminal_history(original, 0, 0)

        self.assertEqual(len(saved_history.splitlines()), TERMINAL_HISTORY_LINE_LIMIT)
        self.assertNotIn("line-0000", saved_history)
        self.assertIn("line-0011", saved_history)
        self.assertNotIn("\x1b", saved_history)
        self.assertNotIn("\x07", saved_history)

        reopened = {**first, "id": 99, "last_reported_cmdline": ""}
        second_save = build_context(
            [_tab(reopened)],
            original,
            command_outputs={99: ""},
            terminal_histories={99: ""},
        )

        self.assertEqual(pane_terminal_history(second_save, 0, 0), saved_history)
        pane = second_save["tabs"][0]["panes"][0]
        self.assertEqual(pane["last_command_output"], "tests passed\n")
        self.assertEqual(
            pane["command_history"], original["tabs"][0]["panes"][0]["command_history"]
        )

    def test_unknown_foreground_command_becomes_single_line_unexecuted_reminder(self) -> None:
        window = {
            "id": 11,
            "title": "Server",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["python", "server.py", "line one\nline two"]}],
            "at_prompt": False,
            "in_alternate_screen": False,
        }
        context = build_context([_tab(window)])

        self.assertFalse(context["restore_commands"][0]["auto_run"])
        pending = pending_restore_commands(context)
        self.assertEqual(pending, {(0, 0): "python server.py 'line one line two'"})
        self.assertNotIn("\n", pending[(0, 0)])

    def test_copy_context_appends_tabs_and_rebuilds_command_totals(self) -> None:
        shell_a = {
            "id": 11,
            "cwd": "/tmp/a",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "make test",
            "at_prompt": True,
        }
        shell_b = {
            "id": 12,
            "cwd": "/tmp/b",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "last_reported_cmdline": "git status",
            "at_prompt": True,
        }

        merged = merge_context(
            build_context([_tab(shell_a, title="A")]),
            build_context([_tab(shell_b, title="B")]),
        )

        self.assertEqual([tab["title"] for tab in merged["tabs"]], ["A", "B"])
        self.assertEqual(merged["command_count"], 2)

    def test_reopened_pane_close_keeps_new_commands_and_two_thousand_lines(self) -> None:
        first = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "at_prompt": True,
        }
        initial_lines = "".join(f"initial-{index:04d}\n" for index in range(1999))
        original = build_context(
            [_tab(first)],
            command_events=[
                {
                    "window_id": 11,
                    "command": "ls",
                    "completed_at": "2026-08-04T11:30:00Z",
                }
            ],
            terminal_histories={11: initial_lines},
        )
        original["snapshot_revision"] = 7
        reopened_window = {**first, "id": 99}
        reopened = remap_context_windows(original, [_tab(reopened_window)])
        capture: ClosingPaneCapture = {
            "tab_index": 0,
            "pane_index": 0,
            "window": cast(
                KittyWindow,
                {
                    **reopened_window,
                    "last_reported_cmdline": "pwd",
                    "last_cmd_exit_status": 0,
                },
            ),
            "terminal_history": f"{initial_lines}pwd\n/tmp/project\n",
            "alternate_screen_text": "",
            "last_command_output": "/tmp/project\n",
            "command_events": [
                {
                    "window_id": 99,
                    "command": "pwd",
                    "completed_at": "2026-08-04T11:31:00Z",
                    "cwd": "/tmp/project",
                }
            ],
        }

        closed = update_context_for_closing_pane(reopened, capture)
        pane = closed["tabs"][0]["panes"][0]

        self.assertEqual(pane["window_id"], 99)
        self.assertEqual([entry["command"] for entry in pane["command_history"]], ["ls", "pwd"])
        self.assertEqual(pane["last_command_output"], "/tmp/project\n")
        self.assertEqual(len(pane_terminal_history(closed, 0, 0).splitlines()), 2000)
        self.assertNotIn("initial-0000", pane_terminal_history(closed, 0, 0))
        self.assertIn("/tmp/project", pane_terminal_history(closed, 0, 0))
        self.assertEqual(closed["snapshot_revision"], 7)

        reopened_again = remap_context_windows(closed, [_tab({**first, "id": 123})])
        second_pane = reopened_again["tabs"][0]["panes"][0]
        self.assertEqual(second_pane["window_id"], 123)
        self.assertEqual(second_pane["command_history"], pane["command_history"])
        self.assertEqual(
            pane_terminal_history(reopened_again, 0, 0), pane_terminal_history(closed, 0, 0)
        )

    def test_top_close_keeps_normal_scrollback_separate_and_restores_top(self) -> None:
        shell = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "foreground_processes": [{"cmdline": ["-zsh"]}],
            "at_prompt": True,
        }
        original = build_context(
            [_tab(shell)],
            terminal_histories={11: "ls\nREADME.md\npwd\n/tmp/project\n"},
        )
        reopened = remap_context_windows(original, [_tab({**shell, "id": 99})])
        capture: ClosingPaneCapture = {
            "tab_index": 0,
            "pane_index": 0,
            "window": cast(
                KittyWindow,
                {
                    **shell,
                    "id": 99,
                    "title": "top",
                    "foreground_processes": [{"cmdline": ["top"]}],
                    "at_prompt": False,
                    "in_alternate_screen": True,
                },
            ),
            "terminal_history": "ls\nREADME.md\npwd\n/tmp/project\n",
            "alternate_screen_text": "Processes: 412 total\nCPU usage: 8.4%\n",
            "last_command_output": "",
            "command_events": [],
        }

        closed = update_context_for_closing_pane(reopened, capture)
        restore = closed["restore_commands"][0]

        self.assertEqual(pane_terminal_history(closed, 0, 0), "ls\nREADME.md\npwd\n/tmp/project\n")
        self.assertEqual(
            pane_alternate_screen_text(closed, 0, 0),
            "Processes: 412 total\nCPU usage: 8.4%\n",
        )
        self.assertEqual(restore["argv"], ["top"])
        self.assertTrue(restore["auto_run"])

    def test_cmd_w_keeps_running_commands_and_both_terminal_buffers(self) -> None:
        """Preserve three running apps through teardown and the first reopen."""
        commands = (("nvim", "."), ("htop",), ("top",))
        windows = [
            {
                "id": 51 + index,
                "title": argv[0],
                "cwd": "/tmp/project",
                "foreground_processes": ([] if argv == ("top",) else [{"cmdline": list(argv)}]),
                "last_reported_cmdline": " ".join(argv),
                "at_prompt": False,
                "in_alternate_screen": True,
            }
            for index, argv in enumerate(commands)
        ]
        context = build_context(
            [_tab(*windows)],
            terminal_histories={
                51: "NVIM FRAME\n",
                52: "HTOP FRAME\n",
                53: "TOP FRAME\n",
            },
        )
        context["snapshot_revision"] = 4

        for pane_index, (window, argv) in enumerate(zip(windows, commands, strict=True)):
            capture: ClosingPaneCapture = {
                "tab_index": 0,
                "pane_index": pane_index,
                "window": cast(
                    KittyWindow,
                    {
                        **window,
                        "foreground_processes": [],
                        "last_reported_cmdline": " ".join(argv),
                    },
                ),
                "terminal_history": f"shell history for {argv[0]}\n",
                "alternate_screen_text": f"{argv[0].upper()} FRAME AT CLOSE\n",
                "last_command_output": "",
                "command_events": [],
            }
            context = update_context_for_closing_pane(context, capture)

        panes = context["tabs"][0]["panes"]
        self.assertEqual([pane["last_command"] for pane in panes], ["nvim .", "htop", "top"])
        self.assertEqual(
            [candidate["argv"] for candidate in context["restore_commands"]],
            [["nvim", "."], ["htop"], ["top"]],
        )
        for pane_index, argv in enumerate(commands):
            self.assertEqual(
                pane_terminal_history(context, 0, pane_index),
                f"shell history for {argv[0]}\n",
            )
            self.assertEqual(
                pane_alternate_screen_text(context, 0, pane_index),
                f"{argv[0].upper()} FRAME AT CLOSE\n",
            )
        self.assertEqual(context["snapshot_revision"], 4)

        snapshot = "new_tab Multi App\n" + "launch\n" * len(commands)
        restored = restore_session(snapshot, context)
        for argv in commands:
            self.assertIn(" ".join(argv), restored)

        restored_shells = restore_session(
            snapshot,
            context,
            shell_restore_argv=["/kisesh", "restore-shell", "session-id"],
        )
        self.assertEqual(restored_shells.count("/kisesh restore-shell"), len(commands))
        self.assertNotIn("nvim .", restored_shells)
        self.assertNotIn(" htop", restored_shells)
        self.assertNotIn(" top", restored_shells)
        self.assertEqual(
            [pane_auto_run_argv(context, 0, index) for index in range(len(commands))],
            [list(argv) for argv in commands],
        )

    def test_x_teardown_cannot_erase_exact_claude_and_codex_resumes(self) -> None:
        """Model a good save followed by process metadata disappearing during tab kill."""
        codex_id = "019fd808-918d-7481-b526-c4da01513c42"
        claude_id = "7f676817-c49e-459c-86de-17382e2170ef"
        windows = [
            {
                "id": 71,
                "title": "Codex",
                "cwd": "/tmp/project",
                "foreground_processes": [
                    {"cmdline": ["codex"], "pid": 101},
                    {"cmdline": ["rg", "resume bug"], "pid": 102},
                ],
                "at_prompt": False,
            },
            {
                "id": 72,
                "title": "Claude",
                "cwd": "/tmp/project",
                "foreground_processes": [{"cmdline": ["claude"], "pid": 202}],
                "at_prompt": False,
            },
        ]
        context = build_context(
            [_tab(*windows)],
            agent_resumes={
                71: ["codex", "resume", codex_id],
                72: ["claude", "--resume", claude_id],
            },
        )

        for pane_index, window in enumerate(windows):
            capture: ClosingPaneCapture = {
                "tab_index": 0,
                "pane_index": pane_index,
                "window": cast(
                    KittyWindow,
                    {
                        **window,
                        "foreground_processes": [],
                        "at_prompt": False,
                    },
                ),
                "terminal_history": f"pane {pane_index} history\n",
                "alternate_screen_text": "",
                "last_command_output": "",
                "command_events": [],
            }
            context = update_context_for_closing_pane(context, capture)

        self.assertEqual(
            [candidate["argv"] for candidate in context["restore_commands"]],
            [
                ["codex", "resume", codex_id],
                ["claude", "--resume", claude_id],
            ],
        )
        restored = restore_session("new_tab work\nlaunch\nlaunch\n", context)
        self.assertIn(f"codex resume {codex_id}", restored)
        self.assertIn(f"claude --resume {claude_id}", restored)

        prompt_capture: ClosingPaneCapture = {
            "tab_index": 0,
            "pane_index": 0,
            "window": cast(
                KittyWindow,
                {
                    **windows[0],
                    "foreground_processes": [{"cmdline": ["-zsh"]}],
                    "at_prompt": True,
                },
            ),
            "terminal_history": "agent exited normally\n",
            "alternate_screen_text": "",
            "last_command_output": "",
            "command_events": [],
        }
        exited = update_context_for_closing_pane(context, prompt_capture)
        self.assertEqual(
            [candidate["argv"] for candidate in exited["restore_commands"]],
            [["claude", "--resume", claude_id]],
        )


if __name__ == "__main__":
    unittest.main()
