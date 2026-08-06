from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from kisesh import legacy
from kisesh.domain import KittyOsWindowState, KittyWindow
from kisesh.kitty_client import KittyClient, LiveTab
from kisesh.model import (
    KISESH_UI_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    SessionManifest,
)
from tests.fakes import RecordingCommandRunner


class KittyClientTests(unittest.TestCase):
    def test_remote_command_uses_explicit_unix_socket(self) -> None:
        runner = RecordingCommandRunner(stdout="[]")

        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)
        self.assertEqual(client.list_state(), [])
        self.assertEqual(
            runner.commands,
            [["/kitty", "@", "--to", "unix:/tmp/test.sock", "ls"]],
        )

    def test_last_command_output_requests_only_plain_last_shell_output(self) -> None:
        runner = RecordingCommandRunner(stdout="last output\n")

        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)

        self.assertEqual(client.last_command_output(42), "last output\n")
        self.assertEqual(
            runner.commands,
            [
                [
                    "/kitty",
                    "@",
                    "--to",
                    "unix:/tmp/test.sock",
                    "get-text",
                    "--match",
                    "id:42",
                    "--extent",
                    "last_cmd_output",
                ]
            ],
        )
        self.assertNotIn("--ansi", runner.commands[0])

    def test_terminal_history_requests_styled_screen_and_scrollback(self) -> None:
        runner = RecordingCommandRunner(stdout="scrollback\nscreen\n")

        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)

        self.assertEqual(client.terminal_history(42), "scrollback\nscreen\n")
        self.assertEqual(
            runner.commands[0][-6:],
            ["get-text", "--match", "id:42", "--extent", "all", "--ansi"],
        )

    def test_send_text_prefills_without_an_enter_key(self) -> None:
        runner = RecordingCommandRunner()

        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)
        client.send_text(42, "python server.py --port 8080")

        command, sent = runner.commands[0], runner.inputs[0]
        self.assertEqual(
            command[-5:], ["send-text", "--match", "id:42", "--bracketed-paste=auto", "--stdin"]
        )
        self.assertEqual(sent, "python server.py --port 8080")
        self.assertNotIn("\n", str(sent))
        self.assertNotIn("\r", str(sent))

    def test_tab_rename_targets_one_stable_id_without_changing_focus(self) -> None:
        runner = RecordingCommandRunner()
        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)

        client.rename_tab(42, "Editor and tests")

        self.assertEqual(
            runner.commands[0][-4:],
            ["set-tab-title", "--match", "id:42", "Editor and tests"],
        )

    def test_tab_layout_targets_one_stable_id_without_changing_focus(self) -> None:
        runner = RecordingCommandRunner()
        client = KittyClient(executable="/kitty", socket="/tmp/test.sock", runner=runner)

        client.set_tab_layout(42, "stack")

        self.assertEqual(
            runner.commands[0][-4:],
            ["goto-layout", "--match", "id:42", "stack"],
        )

    def test_popup_targets_main_kitty_instead_of_its_own_panel_socket(self) -> None:
        runner = RecordingCommandRunner(stdout="[]")

        with patch.dict(
            "os.environ",
            {
                "KISESH_TARGET_SOCKET": "unix:/tmp/main-kitty.sock",
                "KITTY_LISTEN_ON": "unix:/tmp/popup-kitty.sock",
            },
            clear=False,
        ):
            KittyClient(executable="/kitty", runner=runner).list_state()

        self.assertEqual(
            runner.commands,
            [["/kitty", "@", "--to", "unix:/tmp/main-kitty.sock", "ls"]],
        )

    def test_tab_parser_finds_user_variable_and_cwd(self) -> None:
        state: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "title": "Demo",
                        "layout": "splits",
                        "windows": [
                            {
                                "id": 3,
                                "session_name": "/tmp/demo.kitty-session",
                                "user_vars": {"kisesh_session": "session-id"},
                                "cwd": "/old",
                                "foreground_processes": [{"cwd": "/new"}],
                            }
                        ],
                    }
                ],
            }
        ]
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock")
        tab = client.tabs(state)[0]
        self.assertEqual(tab.session_id(), "session-id")
        self.assertEqual(tab.native_session_name(), "/tmp/demo.kitty-session")
        self.assertEqual(tab.suggested_root(), "/new")

    def test_previous_live_variables_are_recognized_then_replaced_in_place(self) -> None:
        state: list[KittyOsWindowState] = [
            {
                "id": 1,
                "tabs": [
                    {
                        "id": 2,
                        "title": "Shell",
                        "windows": [
                            {
                                "id": 3,
                                "user_vars": {
                                    legacy.SESSION_ID_VARIABLE: "session-id",
                                    legacy.SESSION_SLUG_VARIABLE: "silver-seal",
                                    legacy.SESSION_NAME_VARIABLE: "Silver Seal",
                                    legacy.SESSION_SCOPE_VARIABLE: "1",
                                },
                            }
                        ],
                    },
                    {
                        "id": 4,
                        "title": "Old manager overlay",
                        "windows": [{"id": 5, "user_vars": {legacy.UI_VARIABLE: "yes"}}],
                    },
                ],
            }
        ]
        runner = RecordingCommandRunner(stdout=json.dumps(state))
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock", runner=runner)

        tabs = client.tabs(state)
        self.assertEqual([tab.tab_id for tab in tabs], [2])
        self.assertEqual(tabs[0].session_id(), "session-id")
        self.assertEqual(tabs[0].session_scope(), "1")

        manifest = SessionManifest(
            name="Silver Seal",
            slug="silver-seal",
            project_root="/tmp",
            id="session-id",
        )
        client.stamp_tab(tabs[0], manifest)
        client.activate_session(manifest.id, tabs[0])

        stamp_command = runner.commands[0]
        self.assertIn(f"{SESSION_ID_VAR}=session-id", stamp_command)
        self.assertIn(f"{SESSION_NAME_VAR}=Silver Seal", stamp_command)
        self.assertIn(legacy.SESSION_ID_VARIABLE, stamp_command)
        self.assertIn(legacy.SESSION_SLUG_VARIABLE, stamp_command)
        self.assertIn(legacy.SESSION_NAME_VARIABLE, stamp_command)
        scope_command = next(
            command for command in runner.commands if f"{SESSION_SCOPE_VAR}=1" in command
        )
        self.assertIn(legacy.SESSION_SCOPE_VARIABLE, scope_command)
        self.assertEqual(
            runner.commands[-1][-1],
            f"var:{SESSION_ID_VAR}=session-id or "
            f"var:{legacy.SESSION_ID_VARIABLE}=session-id or "
            f"not var:{SESSION_SCOPE_VAR}=1",
        )

    def test_focused_tab_skips_a_standalone_manager_os_window(self) -> None:
        state: list[KittyOsWindowState] = [
            {
                "id": 10,
                "is_focused": True,
                "tabs": [
                    {
                        "id": 11,
                        "is_focused": True,
                        "windows": [{"id": 99, "cwd": "/manager"}],
                    }
                ],
            },
            {
                "id": 20,
                "is_active": True,
                "tabs": [
                    {
                        "id": 21,
                        "is_active": True,
                        "title": "Actual project",
                        "windows": [{"id": 22, "cwd": "/project"}],
                    }
                ],
            },
        ]
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock")

        tab = client.focused_tab(state, exclude_window_id=99)

        self.assertEqual(tab.os_window_id, 20)
        self.assertEqual(tab.tab_id, 21)
        self.assertEqual(tab.suggested_root(), "/project")

    def test_transient_ui_surface_is_never_treated_as_session_content(self) -> None:
        state: list[KittyOsWindowState] = [
            {
                "id": 10,
                "is_focused": True,
                "tabs": [
                    {
                        "id": 11,
                        "is_focused": True,
                        "is_active": True,
                        "title": "Project with manager",
                        "layout": "splits",
                        "windows": [
                            {
                                "id": 12,
                                "cwd": "/project",
                                "user_vars": {SESSION_ID_VAR: "session-id"},
                            },
                            {
                                "id": 13,
                                "is_active": True,
                                "cwd": "/manager",
                                "user_vars": {KISESH_UI_VAR: "yes"},
                            },
                        ],
                    }
                ],
            }
        ]
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock")

        parsed = client.tabs(state)
        focused = client.focused_tab(state)

        self.assertEqual(len(parsed), 1)
        self.assertEqual([window["id"] for window in parsed[0].windows], [12])
        self.assertEqual([window["id"] for window in focused.windows], [12])
        self.assertEqual(focused.suggested_root(), "/project")
        self.assertEqual(focused.session_id(), "session-id")

    def test_stamp_only_touches_new_panes_to_avoid_autosave_feedback(self) -> None:
        runner = RecordingCommandRunner()

        manifest = SessionManifest(name="Demo", slug="demo", project_root="/tmp")
        desired = {
            SESSION_ID_VAR: manifest.id,
            SESSION_SLUG_VAR: manifest.slug,
            SESSION_NAME_VAR: manifest.name,
        }
        existing: KittyWindow = {"id": 3, "user_vars": dict(desired)}
        new: KittyWindow = {"id": 4, "user_vars": {}}
        tab = LiveTab(1, 2, 0, "Demo", "splits", [existing])
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock", runner=runner)

        client.stamp_tab(tab, manifest)
        self.assertEqual(runner.commands, [])
        tab.windows.append(new)
        client.stamp_tab(tab, manifest)
        self.assertEqual(len(runner.commands), 1)
        self.assertIn("id:4", runner.commands[0])

    def test_clear_tab_session_removes_all_membership_markers_from_every_pane(self) -> None:
        runner = RecordingCommandRunner()

        tab = LiveTab(
            1,
            2,
            0,
            "Demo",
            "splits",
            [{"id": 3}, {"id": 4}],
        )
        client = KittyClient(executable="/kitty", socket="unix:/tmp/test.sock", runner=runner)

        client.clear_tab_session(tab)

        self.assertEqual(len(runner.commands), 1)
        command = runner.commands[0]
        self.assertIn("id:3 or id:4", command)
        self.assertIn(SESSION_ID_VAR, command)
        self.assertIn(SESSION_NAME_VAR, command)
        self.assertIn(SESSION_SLUG_VAR, command)
        self.assertNotIn(f"{SESSION_ID_VAR}=", command)


if __name__ == "__main__":
    unittest.main()
