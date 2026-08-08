"""Tests for native Claude and Codex session-start hook payloads."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kisesh.agent_hooks import (
    CLAUDE_HOOK_COMMAND,
    CODEX_HOOK_COMMAND,
    INVALID_SESSION_START_MESSAGE,
    AgentHookPaths,
    claude_hook_enabled,
    codex_hook_enabled,
    configure_user_agent_hooks,
    disable_claude_hook,
    disable_codex_hook,
    enable_claude_hook,
    enable_codex_hook,
    read_session_start,
    user_agent_hook_paths,
)
from kisesh.app_profiles import ResumeAdapter


class AgentHookTests(unittest.TestCase):
    """Validate the untrusted JSON and Kitty environment boundary."""

    def test_both_agents_resolve_the_exact_originating_kitty_pane(self) -> None:
        """Ignore unrelated native fields while retaining provider, ID, and pane."""
        cases: tuple[tuple[ResumeAdapter, str, int], ...] = (
            ("claude", "7f676817-c49e-459c-86de-17382e2170ef", 11),
            ("codex", "019fd808-918d-7481-b526-c4da01513c42", 12),
        )
        for adapter, session_id, window_id in cases:
            payload = io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": session_id,
                        "cwd": "/tmp/shared-project",
                        "source": "startup",
                    }
                )
            )

            with self.subTest(adapter=adapter):
                event = read_session_start(adapter, payload, {"KITTY_WINDOW_ID": str(window_id)})

            self.assertEqual(event.adapter, adapter)
            self.assertEqual(event.external_session_id, session_id)
            self.assertEqual(event.window_id, window_id)

    def test_user_hook_paths_are_explicit_and_require_home(self) -> None:
        """Target only the two documented user-level hook files."""
        paths = user_agent_hook_paths({"HOME": "/Users/example"})

        self.assertEqual(paths.claude, Path("/Users/example/.claude/settings.json"))
        self.assertEqual(paths.codex, Path("/Users/example/.codex/hooks.json"))
        with self.assertRaisesRegex(ValueError, "HOME is unavailable"):
            user_agent_hook_paths({})
        with self.assertRaisesRegex(ValueError, "HOME must be an absolute path"):
            user_agent_hook_paths({"HOME": "relative/home"})

    def test_malformed_or_unrelated_events_never_select_a_pane(self) -> None:
        """Reject every missing identity boundary before live Kitty is mutated."""
        cases: tuple[tuple[str, dict[str, str]], ...] = (
            ("{", {"KITTY_WINDOW_ID": "11"}),
            ("[]", {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"PreToolUse","session_id":"id"}', {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"SessionStart"}', {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"SessionStart","session_id":""}', {"KITTY_WINDOW_ID": "11"}),
            (
                '{"hook_event_name":"SessionStart","session_id":"id"}',
                {},
            ),
            (
                '{"hook_event_name":"SessionStart","session_id":"id"}',
                {"KITTY_WINDOW_ID": "not-a-pane"},
            ),
            (
                '{"hook_event_name":"SessionStart","session_id":"id"}',
                {"KITTY_WINDOW_ID": "0"},
            ),
        )
        for payload, environment in cases:
            with (
                self.subTest(payload=payload, environment=environment),
                self.assertRaisesRegex(ValueError, INVALID_SESSION_START_MESSAGE),
            ):
                read_session_start("claude", io.StringIO(payload), environment)

    def test_claude_enable_is_reversible_idempotent_and_preserves_other_hooks(self) -> None:
        """Merge one user hook without replacing settings or neighboring handlers."""
        session_hook: dict[str, object] = {
            "matcher": "startup",
            "hooks": [{"type": "command", "command": "prepare-project"}],
        }
        post_tool_hook: dict[str, object] = {
            "matcher": "Edit",
            "hooks": [{"type": "command", "command": "format-project"}],
        }
        original: dict[str, object] = {
            "model": "sonnet",
            "hooks": {
                "SessionStart": [session_hook],
                "PostToolUse": [post_tool_hook],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / ".claude" / "settings.json"
            settings.parent.mkdir()
            encoded_original = json.dumps(original, indent=2) + "\n"
            settings.write_text(encoded_original, encoding="utf-8")

            self.assertTrue(enable_claude_hook(settings))
            enabled_text = settings.read_text(encoding="utf-8")
            enabled = json.loads(enabled_text)

            self.assertTrue(claude_hook_enabled(settings))
            self.assertEqual(
                enabled,
                {
                    "model": "sonnet",
                    "hooks": {
                        "SessionStart": [
                            session_hook,
                            {"hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]},
                        ],
                        "PostToolUse": [post_tool_hook],
                    },
                },
            )
            self.assertFalse(enable_claude_hook(settings))
            self.assertEqual(settings.read_text(encoding="utf-8"), enabled_text)
            self.assertTrue(disable_claude_hook(settings))
            self.assertFalse(claude_hook_enabled(settings))
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)
            self.assertFalse(disable_claude_hook(settings))

    def test_claude_first_use_is_private_and_symlinked_settings_stay_symlinked(self) -> None:
        """Create secure settings and edit a linked target without replacing its link."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh = root / "fresh" / "settings.json"

            self.assertFalse(claude_hook_enabled(fresh))
            self.assertFalse(disable_claude_hook(fresh))
            self.assertTrue(enable_claude_hook(fresh))
            self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)
            self.assertTrue(disable_claude_hook(fresh))
            self.assertEqual(json.loads(fresh.read_text(encoding="utf-8")), {})

            target = root / "managed" / "settings.json"
            target.parent.mkdir()
            original = '{"theme":"dark"}\n'
            target.write_text(original, encoding="utf-8")
            linked = root / "linked" / "settings.json"
            linked.parent.mkdir()
            linked.symlink_to(target)

            self.assertTrue(enable_claude_hook(linked))

            self.assertTrue(linked.is_symlink())
            self.assertEqual(
                target.with_name("settings.json.kisesh.bak").read_text(encoding="utf-8"),
                original,
            )
            self.assertTrue(claude_hook_enabled(linked))

    def test_claude_rejects_malformed_settings_and_preserves_failed_writes(self) -> None:
        """Refuse ambiguous JSON shapes and keep the source intact on write failure."""
        invalid_payloads: tuple[object, ...] = (
            [],
            {"hooks": []},
            {"hooks": {"SessionStart": {}}},
            {"hooks": {"SessionStart": ["not-a-group"]}},
            {"hooks": {"SessionStart": [{}]}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "settings.json"
            for payload in invalid_payloads:
                encoded = json.dumps(payload) + "\n"
                settings.write_text(encoded, encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    enable_claude_hook(settings)
                self.assertEqual(settings.read_text(encoding="utf-8"), encoded)

            malformed = "{"
            settings.write_text(malformed, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot read agent hook configuration"):
                enable_claude_hook(settings)
            self.assertEqual(settings.read_text(encoding="utf-8"), malformed)

            unrelated: dict[str, object] = {"hooks": {"PostToolUse": []}}
            settings.write_text(json.dumps(unrelated), encoding="utf-8")
            self.assertFalse(claude_hook_enabled(settings))
            self.assertFalse(disable_claude_hook(settings))

            broken = Path(temporary) / "broken.json"
            broken.symlink_to(Path(temporary) / "missing.json")
            with self.assertRaisesRegex(ValueError, "cannot resolve agent hook configuration"):
                claude_hook_enabled(broken)

            original = '{"theme":"light"}\n'
            settings.write_text(original, encoding="utf-8")
            with (
                mock.patch(
                    "kisesh.agent_hooks.atomic_write_text",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                enable_claude_hook(settings)
            self.assertEqual(settings.read_text(encoding="utf-8"), original)

    def test_codex_enable_is_reversible_idempotent_and_preserves_other_hooks(self) -> None:
        """Merge the user-level Codex hook without disturbing its lifecycle config."""
        session_end_hooks: list[dict[str, object]] = [
            {"hooks": [{"type": "command", "command": "archive-notes", "timeout": 3}]}
        ]
        original: dict[str, object] = {
            "description": "Personal lifecycle hooks",
            "hooks": {"SessionEnd": session_end_hooks},
        }
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / ".codex" / "hooks.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

            self.assertTrue(enable_codex_hook(settings))
            enabled_text = settings.read_text(encoding="utf-8")
            enabled = json.loads(enabled_text)

            self.assertTrue(codex_hook_enabled(settings))
            self.assertEqual(
                enabled,
                {
                    **original,
                    "hooks": {
                        "SessionEnd": session_end_hooks,
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}
                        ],
                    },
                },
            )
            self.assertFalse(enable_codex_hook(settings))
            self.assertEqual(settings.read_text(encoding="utf-8"), enabled_text)
            self.assertTrue(disable_codex_hook(settings))
            self.assertFalse(codex_hook_enabled(settings))
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)
            self.assertFalse(disable_codex_hook(settings))

    def test_transaction_reports_when_automatic_rollback_itself_fails(self) -> None:
        """Do not hide a failed recovery behind the original provider error."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = AgentHookPaths(root / "claude.json", root / "codex.json")
            with (
                mock.patch(
                    "kisesh.agent_hooks.enable_codex_hook",
                    side_effect=OSError("provider write failed"),
                ),
                mock.patch(
                    "kisesh.agent_hooks._restore_json",
                    side_effect=OSError("recovery disk full"),
                ),
                self.assertRaisesRegex(OSError, "cannot roll back agent hook configuration"),
            ):
                configure_user_agent_hooks(paths, enabled=True)


if __name__ == "__main__":
    unittest.main()
