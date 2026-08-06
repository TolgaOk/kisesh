from __future__ import annotations

import unittest
from pathlib import Path

from kisesh.domain import (
    KittyWindow,
    PaneContext,
    RestoreSpec,
    SessionContext,
    TabContext,
)
from kisesh.kitty_client import LiveTab
from kisesh.model import SessionManifest, SnapshotSummary
from kisesh.preview import build_session_preview, is_shell_program
from kisesh.service import SessionView
from kisesh.store import StoredSession


def _context(tabs: list[TabContext]) -> SessionContext:
    return {
        "schema_version": 1,
        "captured_at": "2026-08-05T12:00:00Z",
        "programs": [],
        "agents": [],
        "command_count": 0,
        "restore_commands": [],
        "tabs": tabs,
    }


def _pane(
    window_id: int,
    *,
    program: str | None,
    agent: str | None = None,
    argv: list[str] | None = None,
    title: str = "",
    last_command: str | None = None,
    focused_at: float | None = None,
    restore: bool = False,
) -> PaneContext:
    restore_spec: RestoreSpec | None = None
    if restore:
        restore_spec = {
            "argv": [program or "command"],
            "command": program or "command",
            "kind": "agent" if agent else "foreground",
            "auto_run": True,
        }
    pane: PaneContext = {
        "window_id": window_id,
        "title": title,
        "cwd": "/tmp/project",
        "program": program,
        "agent": agent,
        "foreground_argv": argv or [],
        "foreground_command": None,
        "restore": restore_spec,
        "at_prompt": False,
        "alternate_screen": False,
        "last_exit_status": None,
        "needs_attention": False,
        "had_activity": False,
        "command_history": [],
        "last_command": last_command,
        "last_command_output": None,
        "last_command_output_truncated": False,
        "last_output_command": None,
        "terminal_history": None,
        "terminal_history_truncated": False,
        "alternate_screen_text": None,
        "alternate_screen_text_truncated": False,
    }
    if focused_at is not None:
        pane["last_focused_at"] = focused_at
    return pane


def _view(
    *,
    live_tabs: list[LiveTab] | None = None,
    context: SessionContext | None = None,
    tab_count: int = 1,
    tab_titles: list[str] | None = None,
) -> SessionView:
    manifest = SessionManifest(
        name="Preview",
        slug="preview",
        project_root="/tmp/project",
        summary=SnapshotSummary(
            tab_count=tab_count,
            pane_count=0,
            tab_titles=tab_titles or [],
        ),
    )
    return SessionView(
        StoredSession(manifest, Path("/tmp/preview")),
        live_tabs or [],
        context,
    )


class SessionPreviewTests(unittest.TestCase):
    def test_live_kitty_state_wins_over_stale_saved_agent_context(self) -> None:
        stale = _context(
            [
                {
                    "title": "stale",
                    "layout": "stack",
                    "focused": False,
                    "panes": [_pane(99, program="nvim")],
                }
            ]
        )
        windows: list[KittyWindow] = [
            {
                "id": 1,
                "title": "codex",
                "foreground_processes": [
                    {"cmdline": ["/opt/bin/codex-nightly"]},
                    {"cmdline": []},
                ],
                "is_active": True,
                "needs_attention": True,
            },
            {
                "id": 2,
                "title": "claude",
                "foreground_processes": [],
                "last_reported_cmdline": "claude --resume abc",
                "at_prompt": False,
                "is_focused": True,
            },
            {
                "id": 3,
                "title": "custom",
                "foreground_processes": [],
                "last_reported_cmdline": "'",
                "at_prompt": False,
            },
            {
                "id": 4,
                "title": "",
                "foreground_processes": [],
                "last_reported_cmdline": "",
                "at_prompt": True,
            },
        ]
        live = LiveTab(1, 2, 0, " ", " ", windows, is_focused=True)

        preview = build_session_preview(_view(live_tabs=[live], context=stale))

        self.assertEqual(preview.source, "live")
        self.assertEqual(preview.tabs[0].title, "Tab 1")
        self.assertEqual(preview.tabs[0].layout, "")
        self.assertTrue(preview.tabs[0].focused)
        self.assertEqual(
            [pane.program for pane in preview.tabs[0].panes],
            ["codex-nightly", "claude", "custom", "shell"],
        )
        self.assertEqual(
            [pane.agent for pane in preview.tabs[0].panes],
            ["codex", "claude", None, None],
        )
        self.assertEqual(
            [pane.active for pane in preview.tabs[0].panes],
            [True, True, False, False],
        )
        self.assertTrue(preview.tabs[0].panes[0].needs_attention)
        self.assertFalse(any(pane.restore_available for pane in preview.tabs[0].panes))
        self.assertNotIn("stale", [tab.title for tab in preview.tabs])

    def test_saved_context_preserves_order_agents_restore_and_one_active_pane(self) -> None:
        tabs: list[TabContext] = [
            {
                "title": " ",
                "layout": "splits",
                "focused": True,
                "panes": [
                    _pane(
                        1,
                        program="claude",
                        agent="claude",
                        last_command="claude   --continue",
                        focused_at=2.0,
                        restore=True,
                    ),
                    _pane(
                        2,
                        program=None,
                        argv=["/opt/bin/codex-beta"],
                        focused_at=3.0,
                    ),
                    _pane(3, program=None, title="custom pane"),
                ],
            },
            {"title": "empty", "layout": "", "focused": False, "panes": []},
        ]

        preview = build_session_preview(_view(context=_context(tabs)))

        self.assertEqual(preview.source, "saved")
        self.assertEqual([tab.title for tab in preview.tabs], ["Tab 1", "empty"])
        self.assertEqual(
            [pane.program for pane in preview.tabs[0].panes],
            ["claude", "codex-beta", "custom pane"],
        )
        self.assertEqual(
            [pane.agent for pane in preview.tabs[0].panes],
            ["claude", "codex", None],
        )
        self.assertEqual(
            [pane.active for pane in preview.tabs[0].panes],
            [False, True, False],
        )
        self.assertEqual(preview.tabs[0].panes[0].last_command, "claude --continue")
        self.assertTrue(preview.tabs[0].panes[0].restore_available)
        self.assertFalse(preview.tabs[1].panes)

    def test_manifest_summary_fills_missing_names_without_inventing_pane_details(self) -> None:
        preview = build_session_preview(
            _view(context=_context([]), tab_count=3, tab_titles=["named", ""])
        )

        self.assertEqual(preview.source, "summary")
        self.assertEqual([tab.title for tab in preview.tabs], ["named", "Tab 2", "Tab 3"])
        self.assertTrue(all(not tab.details_available for tab in preview.tabs))
        self.assertTrue(all(not tab.panes for tab in preview.tabs))

        empty = build_session_preview(_view(context=None, tab_count=0))
        self.assertEqual(empty.tabs, ())
        self.assertTrue(is_shell_program("zsh"))
        self.assertFalse(is_shell_program("nvim"))


if __name__ == "__main__":
    unittest.main()
