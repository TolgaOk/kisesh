"""Behavioral and boundary tests for native agent session hooks."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from kisesh.agent_hooks import (
    INVALID_SESSION_START_MESSAGE,
    AgentHookSpec,
    AgentHookState,
    JsonAgentHook,
    PiExtensionHook,
    agent_hook_state,
    agent_session_start,
    configure_user_agent_hooks,
    disable_agent_hook,
    enable_agent_hook,
    read_session_start,
    user_agent_hooks,
)
from kisesh.app_profiles import ResumeAdapter


def _hooks(home: Path) -> dict[ResumeAdapter, AgentHookSpec]:
    """Index isolated native hook specifications by adapter name."""
    return {hook.adapter: hook for hook in user_agent_hooks({"HOME": str(home)})}


class AgentHookTests(unittest.TestCase):
    """Exercise native configuration and untrusted event boundaries."""

    def test_unknown_adapter_hook_and_state_variants_fail_closed(self) -> None:
        """Reject runtime values outside every closed hook integration union."""
        unknown_adapter = cast(ResumeAdapter, "unknown")
        with self.assertRaises(AssertionError):
            agent_session_start(unknown_adapter, "session", {"KITTY_WINDOW_ID": "11"})

        unknown_hook = cast(AgentHookSpec, object())
        for operation in (agent_hook_state, enable_agent_hook, disable_agent_hook):
            with self.subTest(operation=operation.__name__), self.assertRaises(AssertionError):
                operation(unknown_hook)
        with self.assertRaises(AssertionError):
            configure_user_agent_hooks((unknown_hook,), enabled=True)

        with tempfile.TemporaryDirectory() as temporary:
            pi = _hooks(Path(temporary))["pi"]
            assert isinstance(pi, PiExtensionHook)
            invalid_state = cast(AgentHookState, "invalid")
            with mock.patch("kisesh.agent_hooks._pi_hook_state", return_value=invalid_state):
                for operation in (enable_agent_hook, disable_agent_hook):
                    with (
                        self.subTest(operation=operation.__name__),
                        self.assertRaises(AssertionError),
                    ):
                        operation(pi)

    def test_native_and_extension_events_resolve_the_originating_kitty_pane(self) -> None:
        """Accept provider payloads and Pi's direct UUID without coupling them."""
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
            self.assertEqual(
                (event.adapter, event.external_session_id, event.window_id),
                (adapter, session_id, window_id),
            )

        pi = agent_session_start(
            "pi",
            "b624c385-95da-4626-9aeb-8b4f54e31dc2",
            {"KITTY_WINDOW_ID": "13"},
        )
        self.assertEqual(pi.adapter, "pi")
        self.assertEqual(pi.external_session_id, "b624c385-95da-4626-9aeb-8b4f54e31dc2")
        self.assertEqual(pi.window_id, 13)

    def test_user_hook_specs_cover_three_product_owned_integration_types(self) -> None:
        """Resolve two JSON hooks and one dedicated Pi extension from HOME."""
        hooks = user_agent_hooks({"HOME": "/Users/example"})
        claude, codex, pi = hooks

        self.assertIsInstance(claude, JsonAgentHook)
        self.assertIsInstance(codex, JsonAgentHook)
        self.assertIsInstance(pi, PiExtensionHook)
        assert isinstance(pi, PiExtensionHook)
        self.assertEqual(claude.path, Path("/Users/example/.claude/settings.json"))
        self.assertEqual(codex.path, Path("/Users/example/.codex/hooks.json"))
        self.assertEqual(pi.path, Path("/Users/example/.pi/agent/extensions/kisesh.ts"))
        self.assertIn('pi.on("session_start"', pi.source)
        self.assertIn("context.sessionManager.getSessionId()", pi.source)
        self.assertIn('["agent-hook", "pi", "--session-id"', pi.source)
        self.assertEqual(codex.status_suffix, " (review with /hooks)")
        with self.assertRaisesRegex(ValueError, "HOME is unavailable"):
            user_agent_hooks({})
        with self.assertRaisesRegex(ValueError, "HOME must be an absolute path"):
            user_agent_hooks({"HOME": "relative/home"})

    def test_malformed_or_unrelated_events_never_select_a_pane(self) -> None:
        """Reject every missing identity boundary before live Kitty is mutated."""
        cases: tuple[tuple[str, dict[str, str]], ...] = (
            ("{", {"KITTY_WINDOW_ID": "11"}),
            ("[]", {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"PreToolUse","session_id":"id"}', {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"SessionStart"}', {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"SessionStart","session_id":""}', {"KITTY_WINDOW_ID": "11"}),
            ('{"hook_event_name":"SessionStart","session_id":"id"}', {}),
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
        for session_id in (None, "", 7):
            with (
                self.subTest(session_id=session_id),
                self.assertRaisesRegex(ValueError, INVALID_SESSION_START_MESSAGE),
            ):
                agent_session_start("pi", session_id, {"KITTY_WINDOW_ID": "11"})

    def test_claude_hook_is_reversible_and_preserves_neighboring_handlers(self) -> None:
        """Merge one JSON hook without replacing settings or other lifecycle handlers."""
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
            hook = _hooks(Path(temporary))["claude"]
            assert isinstance(hook, JsonAgentHook)
            hook.path.parent.mkdir()
            encoded_original = json.dumps(original, indent=2) + "\n"
            hook.path.write_text(encoded_original, encoding="utf-8")

            self.assertTrue(enable_agent_hook(hook))
            enabled_text = hook.path.read_text(encoding="utf-8")
            enabled = json.loads(enabled_text)
            self.assertEqual(agent_hook_state(hook), AgentHookState.CONFIGURED)
            self.assertEqual(
                enabled,
                {
                    "model": "sonnet",
                    "hooks": {
                        "SessionStart": [
                            session_hook,
                            {"hooks": [{"type": "command", "command": hook.command}]},
                        ],
                        "PostToolUse": [post_tool_hook],
                    },
                },
            )
            self.assertFalse(enable_agent_hook(hook))
            self.assertEqual(hook.path.read_text(encoding="utf-8"), enabled_text)
            self.assertTrue(disable_agent_hook(hook))
            self.assertEqual(agent_hook_state(hook), AgentHookState.NOT_CONFIGURED)
            self.assertEqual(json.loads(hook.path.read_text(encoding="utf-8")), original)
            self.assertFalse(disable_agent_hook(hook))

    def test_json_hook_first_use_is_private_and_symlink_targets_are_preserved(self) -> None:
        """Create secure JSON and atomically edit a target without replacing its link."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = _hooks(root)["claude"]
            assert isinstance(template, JsonAgentHook)
            fresh = replace(template, path=root / "fresh" / "settings.json")

            self.assertEqual(agent_hook_state(fresh), AgentHookState.NOT_CONFIGURED)
            self.assertFalse(disable_agent_hook(fresh))
            self.assertTrue(enable_agent_hook(fresh))
            self.assertEqual(stat.S_IMODE(fresh.path.stat().st_mode), 0o600)
            self.assertTrue(disable_agent_hook(fresh))
            self.assertEqual(json.loads(fresh.path.read_text(encoding="utf-8")), {})

            target = root / "managed" / "settings.json"
            target.parent.mkdir()
            original = '{"theme":"dark"}\n'
            target.write_text(original, encoding="utf-8")
            linked = root / "linked" / "settings.json"
            linked.parent.mkdir()
            linked.symlink_to(target)
            hook = replace(template, path=linked)

            self.assertTrue(enable_agent_hook(hook))
            self.assertTrue(linked.is_symlink())
            self.assertEqual(
                target.with_name("settings.json.kisesh.bak").read_text(encoding="utf-8"),
                original,
            )
            self.assertEqual(agent_hook_state(hook), AgentHookState.CONFIGURED)

    def test_json_hooks_reject_malformed_settings_and_preserve_failed_writes(self) -> None:
        """Refuse ambiguous JSON shapes and keep source content on write failure."""
        invalid_payloads: tuple[object, ...] = (
            [],
            {"hooks": []},
            {"hooks": {"SessionStart": {}}},
            {"hooks": {"SessionStart": ["not-a-group"]}},
            {"hooks": {"SessionStart": [{}]}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = _hooks(root)["claude"]
            assert isinstance(template, JsonAgentHook)
            settings = root / "settings.json"
            hook = replace(template, path=settings)
            for payload in invalid_payloads:
                encoded = json.dumps(payload) + "\n"
                settings.write_text(encoded, encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    enable_agent_hook(hook)
                self.assertEqual(settings.read_text(encoding="utf-8"), encoded)

            malformed = "{"
            settings.write_text(malformed, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot read agent hook configuration"):
                enable_agent_hook(hook)
            self.assertEqual(settings.read_text(encoding="utf-8"), malformed)

            unrelated: dict[str, object] = {"hooks": {"PostToolUse": []}}
            settings.write_text(json.dumps(unrelated), encoding="utf-8")
            self.assertEqual(agent_hook_state(hook), AgentHookState.NOT_CONFIGURED)
            self.assertFalse(disable_agent_hook(hook))

            broken = root / "broken.json"
            broken.symlink_to(root / "missing.json")
            with self.assertRaisesRegex(ValueError, "cannot resolve agent hook configuration"):
                agent_hook_state(replace(template, path=broken))

            original = '{"theme":"light"}\n'
            settings.write_text(original, encoding="utf-8")
            with (
                mock.patch(
                    "kisesh.agent_hooks.atomic_write_text",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                enable_agent_hook(hook)
            self.assertEqual(settings.read_text(encoding="utf-8"), original)

    def test_codex_hook_is_idempotent_and_retains_other_lifecycle_groups(self) -> None:
        """Preserve neighboring Codex lifecycle data across enable and disable."""
        session_end_hooks: list[dict[str, object]] = [
            {"hooks": [{"type": "command", "command": "archive-notes", "timeout": 3}]}
        ]
        original: dict[str, object] = {
            "description": "Personal lifecycle hooks",
            "hooks": {"SessionEnd": session_end_hooks},
        }
        with tempfile.TemporaryDirectory() as temporary:
            hook = _hooks(Path(temporary))["codex"]
            assert isinstance(hook, JsonAgentHook)
            hook.path.parent.mkdir()
            hook.path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

            self.assertTrue(enable_agent_hook(hook))
            enabled_text = hook.path.read_text(encoding="utf-8")
            enabled = json.loads(enabled_text)
            self.assertEqual(
                enabled,
                {
                    **original,
                    "hooks": {
                        "SessionEnd": session_end_hooks,
                        "SessionStart": [{"hooks": [{"type": "command", "command": hook.command}]}],
                    },
                },
            )
            self.assertFalse(enable_agent_hook(hook))
            self.assertEqual(hook.path.read_text(encoding="utf-8"), enabled_text)
            self.assertTrue(disable_agent_hook(hook))
            self.assertEqual(json.loads(hook.path.read_text(encoding="utf-8")), original)

    def test_pi_extension_is_private_reversible_and_never_overwrites_foreign_content(self) -> None:
        """Own only the dedicated extension file and reject content or symlink conflicts."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hook = _hooks(root)["pi"]
            assert isinstance(hook, PiExtensionHook)

            self.assertEqual(agent_hook_state(hook), AgentHookState.NOT_CONFIGURED)
            self.assertFalse(disable_agent_hook(hook))
            self.assertTrue(enable_agent_hook(hook))
            self.assertEqual(agent_hook_state(hook), AgentHookState.CONFIGURED)
            self.assertEqual(hook.path.read_text(encoding="utf-8"), hook.source)
            self.assertEqual(stat.S_IMODE(hook.path.stat().st_mode), 0o600)
            self.assertFalse(enable_agent_hook(hook))
            self.assertTrue(disable_agent_hook(hook))
            self.assertFalse(hook.path.exists())

            foreign = "export default function custom() {}\n"
            hook.path.write_text(foreign, encoding="utf-8")
            self.assertEqual(agent_hook_state(hook), AgentHookState.CONFLICT)
            for operation in (enable_agent_hook, disable_agent_hook):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(ValueError, "not managed"),
                ):
                    operation(hook)
            self.assertEqual(hook.path.read_text(encoding="utf-8"), foreign)

            hook.path.unlink()
            target = root / "foreign.ts"
            target.write_text(foreign, encoding="utf-8")
            hook.path.symlink_to(target)
            self.assertEqual(agent_hook_state(hook), AgentHookState.CONFLICT)

            hook.path.unlink()
            hook.path.write_text(hook.source, encoding="utf-8")
            with (
                mock.patch.object(Path, "read_text", side_effect=OSError("denied")),
                self.assertRaisesRegex(ValueError, "cannot read Pi extension"),
            ):
                agent_hook_state(hook)

    def test_three_hook_transaction_rolls_back_before_reporting_provider_conflicts(self) -> None:
        """Restore both JSON files when the final Pi integration cannot be installed."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks = user_agent_hooks({"HOME": str(root)})
            claude, codex, pi = hooks
            originals = ('{"theme":"dark"}\n', '{"features":{}}\n')
            for hook, content in zip((claude, codex), originals, strict=True):
                hook.path.parent.mkdir(parents=True)
                hook.path.write_text(content, encoding="utf-8")
            pi.path.parent.mkdir(parents=True)
            pi.path.write_text("foreign\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not managed"):
                configure_user_agent_hooks(hooks, enabled=True)

            self.assertEqual(claude.path.read_text(encoding="utf-8"), originals[0])
            self.assertEqual(codex.path.read_text(encoding="utf-8"), originals[1])
            self.assertEqual(pi.path.read_text(encoding="utf-8"), "foreign\n")
            self.assertFalse(claude.path.with_name("settings.json.kisesh.bak").exists())
            self.assertFalse(codex.path.with_name("hooks.json.kisesh.bak").exists())

    def test_transaction_reports_when_automatic_rollback_itself_fails(self) -> None:
        """Do not hide a failed recovery behind the original provider error."""
        with tempfile.TemporaryDirectory() as temporary:
            hooks = user_agent_hooks({"HOME": temporary})
            with (
                mock.patch(
                    "kisesh.agent_hooks.enable_agent_hook",
                    side_effect=OSError("provider write failed"),
                ),
                mock.patch(
                    "kisesh.agent_hooks._restore_file",
                    side_effect=OSError("recovery disk full"),
                ),
                self.assertRaisesRegex(OSError, "cannot roll back agent hook configuration"),
            ):
                configure_user_agent_hooks(hooks, enabled=True)


if __name__ == "__main__":
    unittest.main()
