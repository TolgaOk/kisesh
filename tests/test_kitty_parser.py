from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kitty_workbench.context import restore_session
from kitty_workbench.model import SessionManifest
from kitty_workbench.session_file import sanitize_session

PARSE_SESSION_PROGRAM = (
    "import json,sys; "
    "from pathlib import Path; "
    "from kitty.config import load_config; "
    "from kitty.session import parse_session; "
    "p=Path(sys.argv[1]); "
    "sessions=list(parse_session("
    "p.read_text(),load_config('/dev/null'),session_path=str(p))); "
    "print(json.dumps({'os_windows':len(sessions),"
    "'tabs':sum(len(s.tabs) for s in sessions),"
    "'panes':[len(t.windows) for s in sessions for t in s.tabs]}))"
)


class RealKittyParserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("kitty"), "Kitty is not installed")
    def test_safe_multi_tab_snapshot_is_accepted_by_installed_kitty(self) -> None:
        """Exercise Kitty's real parser, not a workbench imitation of it."""

        raw = (
            "new_os_window\n"
            "new_tab Main Agent\n"
            "layout splits\n"
            "cd /Users/example/Project With Spaces\n"
            "launch --location=vsplit "
            '\'kitty-unserialize-data={"id":1,'
            '"cmd_at_shell_startup":"claude --continue"}\' '
            "claude --continue\n"
            "focus\n"
            "new_tab Git\n"
            "layout stack\n"
            "launch --cwd=/tmp/project lazygit\n"
            "focus_tab\n"
        )
        manifest = SessionManifest(
            name="Parser Scenario",
            slug="parser-scenario",
            project_root="/Users/example/Project With Spaces",
        )
        safe = sanitize_session(raw, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "scenario.kitty-session"
            snapshot.write_text(safe, encoding="utf-8")
            result = subprocess.run(
                [
                    shutil.which("kitty") or "kitty",
                    "+runpy",
                    PARSE_SESSION_PROGRAM,
                    str(snapshot),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"os_windows": 1, "tabs": 2, "panes": [1, 1]})

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is not installed")
    def test_opt_in_integration_is_accepted_by_installed_kitty(self) -> None:
        integration = Path(__file__).parents[1] / "integration" / "kitty-workbench.conf"
        program = (
            "import json,sys; "
            "from kitty.config import load_config; "
            "bad=[]; load_config(sys.argv[1],accumulate_bad_lines=bad); "
            "print(json.dumps([str(x) for x in bad]))"
        )
        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", program, str(integration)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is not installed")
    def test_manager_shortcut_resolves_to_open_or_close_from_real_focus_state(self) -> None:
        """Exercise Kitty's conditional-key resolver, not only config parsing."""

        integration = Path(__file__).parents[1] / "integration" / "kitty-workbench.conf"
        program = """
import json
import sys
from kitty.config import load_config
from kitty.keys import Mappings

options = load_config(sys.argv[1])
candidates = next(
    definitions
    for definitions in options.keyboard_modes[""].keymap.values()
    if any("kitty-workbench manager" in definition.definition for definition in definitions)
)

class FocusScenario(Mappings):
    def __init__(self, workbench_has_focus):
        self.window = object()
        self.workbench_has_focus = workbench_has_focus

    def get_active_window(self):
        return self.window

    def match_windows(self, expression):
        assert expression == "var:kitty_workbench_ui"
        return iter((self.window,)) if self.workbench_has_focus else iter(())

resolved = {}
for label, focused in (("source", False), ("workbench", True)):
    matches = FocusScenario(focused).matching_key_actions(candidates)
    resolved[label] = [definition.definition for definition in matches]
print(json.dumps(resolved))
"""
        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", program, str(integration)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = json.loads(result.stdout)
        self.assertEqual(len(resolved["source"]), 1)
        self.assertIn("launch --type=overlay", resolved["source"][0])
        self.assertEqual(resolved["workbench"], ["close_window"])

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is not installed")
    def test_generated_agent_resume_snapshot_is_accepted_by_installed_kitty(self) -> None:
        manifest = SessionManifest(
            name="Agent Scenario",
            slug="agent-scenario",
            project_root="/tmp/project",
        )
        safe = sanitize_session(
            "new_tab Agent\nlayout splits\nlaunch --cwd=/tmp/project\n",
            manifest,
        )
        resumable = restore_session(
            safe,
            {
                "restore_commands": [
                    {
                        "tab_index": 0,
                        "pane_index": 0,
                        "argv": ["claude", "--resume", "session-123"],
                        "auto_run": True,
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "agent.kitty-session"
            snapshot.write_text(resumable, encoding="utf-8")
            result = subprocess.run(
                [
                    shutil.which("kitty") or "kitty",
                    "+runpy",
                    PARSE_SESSION_PROGRAM,
                    str(snapshot),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertIn("claude --resume session-123", resumable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"os_windows": 1, "tabs": 1, "panes": [1]})

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is not installed")
    def test_generated_last_output_shell_snapshot_is_accepted_by_installed_kitty(self) -> None:
        manifest = SessionManifest(
            name="Shell Output Scenario",
            slug="shell-output-scenario",
            project_root="/tmp/project",
        )
        safe = sanitize_session(
            "new_tab Shell\nlayout splits\nlaunch --cwd=/tmp/project\n",
            manifest,
        )
        restored = restore_session(
            safe,
            {
                "tabs": [
                    {
                        "panes": [
                            {
                                "last_command": "touch /tmp/must-not-run",
                                "last_command_output": "tests passed\n",
                            }
                        ]
                    }
                ],
                "restore_commands": [],
            },
            shell_restore_argv=["/workbench", "restore-shell", manifest.id],
        )

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "shell-output.kitty-session"
            snapshot.write_text(restored, encoding="utf-8")
            result = subprocess.run(
                [
                    shutil.which("kitty") or "kitty",
                    "+runpy",
                    PARSE_SESSION_PROGRAM,
                    str(snapshot),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertIn("/workbench restore-shell", restored)
        self.assertNotIn("touch /tmp/must-not-run", restored)
        self.assertNotIn("tests passed", restored)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"os_windows": 1, "tabs": 1, "panes": [1]})


if __name__ == "__main__":
    unittest.main()
