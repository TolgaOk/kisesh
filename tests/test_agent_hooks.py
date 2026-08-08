"""Tests for native Claude and Codex session-start hook payloads."""

from __future__ import annotations

import io
import json
import unittest

from kisesh.agent_hooks import INVALID_SESSION_START_MESSAGE, read_session_start
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


if __name__ == "__main__":
    unittest.main()
