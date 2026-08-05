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
    def test_session_filter_keeps_other_os_windows_visible_with_real_query_parser(
        self,
    ) -> None:
        """Validate the scoped Boolean filter using Kitty's own search engine."""

        program = """
import json
from kitty.search_query_parser import search

query = (
    "var:kitty_workbench_session=target "
    "or not var:kitty_workbench_scope=1"
)
universal = {"target", "same-window-other", "other-os-window"}

def get_matches(
    location,
    value,
    candidates,
    matches={
        "kitty_workbench_session=target": {"target"},
        "kitty_workbench_scope=1": {"target", "same-window-other"},
    },
):
    assert location == "var"
    return matches.get(value, set()) & candidates

print(json.dumps(sorted(search(query, ("var",), universal, get_matches))))
"""
        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", program],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["other-os-window", "target"])

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
    def test_transient_manager_is_absent_from_real_restored_layout(self) -> None:
        manifest = SessionManifest(
            name="Overlay Scenario",
            slug="overlay-scenario",
            project_root="/tmp/project",
        )
        safe = sanitize_session(
            "new_tab Shell\n"
            "layout stack\n"
            'set_layout_state {"pairs":{"one":1,"two":2}}\n'
            "launch 'kitty-unserialize-data={\"id\":1}' --var=kitty_workbench_scope=4\n"
            "launch 'kitty-unserialize-data={\"id\":2}' "
            "--var=kitty_workbench_ui=yes --title=Workbench\n"
            "new_tab Workbench\n"
            "layout splits\n"
            "launch 'kitty-unserialize-data={\"id\":3}' "
            "--var=kitty_workbench_ui=yes --title=Workbench\n",
            manifest,
        )

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "without-overlay.kitty-session"
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

        self.assertNotIn("kitty_workbench_ui", safe)
        self.assertNotIn("kitty_workbench_scope", safe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"os_windows": 1, "tabs": 1, "panes": [1]})

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
    def test_command_w_resolves_to_safe_close_or_overlay_close_in_real_kitty(self) -> None:
        """Resolve the real Command-W chord through Kitty's conditional key engine."""

        integration = Path(__file__).parents[1] / "integration" / "kitty-workbench.conf"
        program = """
import json
import sys
from kitty.config import load_config
from kitty.keys import Mappings

options = load_config(sys.argv[1])
key, candidates = next(
    (key, definitions)
    for key, definitions in options.keyboard_modes[""].keymap.items()
    if any("safe_close.py" in definition.definition for definition in definitions)
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
    resolved[label] = [
        definition.definition
        for definition in FocusScenario(focused).matching_key_actions(candidates)
    ]
print(json.dumps({"mods": key.mods, "key": key.key, "resolved": resolved}))
"""
        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", program, str(integration)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["mods"], 8)
        self.assertEqual(parsed["key"], ord("w"))
        self.assertEqual(
            parsed["resolved"]["source"],
            ["kitten ~/.local/lib/kitty-workbench/integration/safe_close.py"],
        )
        self.assertEqual(parsed["resolved"]["workbench"], ["close_window"])

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
