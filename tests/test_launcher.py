from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def kitty_runtime_environment(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["KISESH_INSTALL_ROOT"] = str(project)
    return environment


class LauncherTests(unittest.TestCase):
    def test_gui_style_reduced_path_still_finds_modern_homebrew_python(self) -> None:
        """Reproduce Kitty's GUI PATH instead of relying on the test shell's PATH."""

        project = Path(__file__).parents[1]
        launcher = project / "bin/kisesh"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            homebrew_bin = home / "homebrew/bin"
            homebrew_bin.mkdir(parents=True)
            (homebrew_bin / "python3").symlink_to(Path(sys.executable).resolve())
            environment = os.environ.copy()
            environment.update({"HOME": str(home), "PATH": "/usr/bin:/bin"})
            result = subprocess.run(
                [str(launcher), "--help"],
                cwd=home,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kisesh", result.stdout)
        self.assertIn("manager", result.stdout)
        self.assertIn("add-tab", result.stdout)
        self.assertIn("detach-tab", result.stdout)
        self.assertIn("copy-tab", result.stdout)
        self.assertIn("context", result.stdout)

    def test_kitty_mappings_use_launcher_instead_of_ambient_python(self) -> None:
        project = Path(__file__).parents[1]
        integration = (project / "integration/kisesh.conf").read_text(encoding="utf-8")
        mappings = [line for line in integration.splitlines() if line.startswith("map ")]

        launch_mappings = [line for line in mappings if " launch " in line]
        self.assertEqual(len(mappings), 6)
        for mapping in launch_mappings:
            self.assertIn("~/.local/lib/kisesh/bin/kisesh", mapping)
            self.assertNotIn(" python3 ", mapping)

        manager_mappings = [line for line in mappings if line.startswith("map alt+s ")]
        self.assertEqual(len(manager_mappings), 1)
        self.assertTrue(all("launch --type=overlay" in line for line in manager_mappings))
        self.assertTrue(all("--location=" not in line for line in manager_mappings))
        self.assertTrue(all("--bias=" not in line for line in manager_mappings))
        self.assertTrue(all("--var=kisesh_ui=yes" in line for line in manager_mappings))
        self.assertTrue(all("--env=KISESH_CALLER=overlay" in line for line in manager_mappings))
        self.assertTrue(all("bin/kisesh manager" in line for line in manager_mappings))

        toggle_mappings = [
            line for line in mappings if line.startswith("map --when-focus-on var:kisesh_ui ")
        ]
        self.assertEqual(
            toggle_mappings,
            [
                "map --when-focus-on var:kisesh_ui alt+s combine : last_used_layout : close_window",
                "map --when-focus-on var:kisesh_ui cmd+w combine : last_used_layout : close_window",
            ],
        )
        launch_index = mappings.index(manager_mappings[0])
        manager_close = (
            "map --when-focus-on var:kisesh_ui alt+s combine : last_used_layout : close_window"
        )
        close_index = mappings.index(manager_close)
        self.assertLess(launch_index, close_index)
        close_mapping = "map cmd+w kitten ~/.local/lib/kisesh/integration/safe_close.py"
        self.assertIn(close_mapping, mappings)
        overlay_close = (
            "map --when-focus-on var:kisesh_ui cmd+w combine : last_used_layout : close_window"
        )
        self.assertLess(
            mappings.index(close_mapping),
            mappings.index(overlay_close),
        )
        self.assertTrue((project / "integration/safe_close.py").is_file())
        layout_mapping = (
            "map --when-focus-on var:kisesh_session alt+z kitten "
            "~/.local/lib/kisesh/integration/layout_toggle.py"
        )
        self.assertIn(layout_mapping, mappings)
        self.assertTrue((project / "integration/layout_toggle.py").is_file())
        self.assertNotIn(" undo", integration)
        self.assertNotIn(" park", integration)

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_close_kitten_loads_without_file_inside_kitty_runtime(self) -> None:
        project = Path(__file__).parents[1]
        script = project / "integration/safe_close.py"
        expression = (
            "from kittens.runner import import_kitten_main_module; "
            f"loaded = import_kitten_main_module('', {str(script)!r}); "
            "print(callable(loaded['end']))"
        )

        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", expression],
            cwd=project,
            env=kitty_runtime_environment(project),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_tab_bar_reload_kitten_loads_inside_kitty_runtime(self) -> None:
        project = Path(__file__).parents[1]
        script = project / "integration/reload_tab_bar.py"
        expression = (
            "from kittens.runner import import_kitten_main_module; "
            f"loaded = import_kitten_main_module('', {str(script)!r}); "
            "print(callable(loaded['end']))"
        )

        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", expression],
            cwd=project,
            env=kitty_runtime_environment(project),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_session_filter_kitten_loads_inside_kitty_runtime(self) -> None:
        project = Path(__file__).parents[1]
        script = project / "integration/session_filter.py"
        expression = (
            "from kittens.runner import import_kitten_main_module; "
            f"loaded = import_kitten_main_module('', {str(script)!r}); "
            "print(callable(loaded['end']))"
        )

        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", expression],
            cwd=project,
            env=kitty_runtime_environment(project),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_layout_toggle_kitten_loads_inside_kitty_runtime(self) -> None:
        project = Path(__file__).parents[1]
        script = project / "integration/layout_toggle.py"
        expression = (
            "from kittens.runner import import_kitten_main_module; "
            f"loaded = import_kitten_main_module('', {str(script)!r}); "
            "print(callable(loaded['end']))"
        )

        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", expression],
            cwd=project,
            env=kitty_runtime_environment(project),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    def test_panel_launcher_builds_cold_and_prewarmed_toggle_commands(self) -> None:
        project = Path(__file__).parents[1]
        launcher = project / "bin/kisesh-panel"
        self.assertTrue(os.access(launcher, os.X_OK))

        with tempfile.TemporaryDirectory() as temporary:
            fake_kitten = Path(temporary) / "kitten"
            command_log = Path(temporary) / "commands.log"
            fake_kitten.write_text(
                '#!/bin/sh\nprintf \'<%s>\\n\' "$@" >> "$KISESH_FAKE_LOG"\n',
                encoding="utf-8",
            )
            fake_kitten.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "KISESH_KITTEN": str(fake_kitten),
                    "KISESH_TARGET_SOCKET": "unix:/tmp/main-kitty.sock",
                    "KISESH_PANEL_GROUP": "test-kisesh",
                    "KISESH_PANEL_SOCKET": "unix:/tmp/test-panel.sock",
                    "KISESH_FAKE_LOG": str(command_log),
                }
            )

            for prewarm, expected in ((False, "start_as_hidden=no"), (True, "start_as_hidden=yes")):
                command_log.unlink(missing_ok=True)
                command = [str(launcher)]
                if prewarm:
                    command.append("--prewarm")
                command.extend(("--data-dir", "/tmp/test-data", "manager"))
                result = subprocess.run(
                    command,
                    cwd=project,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                logged = command_log.read_text(encoding="utf-8")

                with self.subTest(prewarm=prewarm):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"<--override={expected}>", logged)
                    self.assertIn("<KISESH_CALLER=panel>", logged)
                    self.assertIn("<KISESH_PANEL_CONFIG=", logged)
                    self.assertIn(
                        "<KISESH_TARGET_SOCKET=unix:/tmp/main-kitty.sock>",
                        logged,
                    )
                    self.assertIn("<--data-dir>", logged)
                    self.assertIn("</tmp/test-data>", logged)
                    self.assertIn("<@>", logged)
                    self.assertIn("<unix:/tmp/test-panel.sock>", logged)
                    self.assertIn("<ctrl+g>", logged)

    def test_optional_quick_access_profile_remains_centered_and_theme_aware(self) -> None:
        project = Path(__file__).parents[1]
        profile = (project / "integration/quick-access-terminal.conf").read_text(encoding="utf-8")

        for setting in (
            "edge center-sized",
            "columns 105",
            "lines 28",
            "hide_on_focus_loss yes",
            "start_as_hidden no",
            "allow_remote_control=socket-only",
            "listen_on=unix:/tmp/kisesh-panel",
        ):
            self.assertIn(setting, profile)
        active_profile = "\n".join(
            line for line in profile.splitlines() if not line.startswith("#")
        )
        self.assertNotIn("kitty_conf", active_profile)


if __name__ == "__main__":
    unittest.main()
