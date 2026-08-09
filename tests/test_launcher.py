from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kisesh import panel_launcher


def kitty_runtime_environment(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["KISESH_INSTALL_ROOT"] = str(project)
    return environment


class LauncherTests(unittest.TestCase):
    def test_gui_style_reduced_path_runs_installed_console_command(self) -> None:
        """Run the installed entry point without relying on an interactive PATH."""

        launcher = Path(sys.executable).with_name("kisesh")
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
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

    def test_package_exposes_paired_console_commands_without_source_wrappers(self) -> None:
        project = Path(__file__).parents[1]
        commands = (
            Path(sys.executable).with_name("kisesh"),
            Path(sys.executable).with_name("kisesh-panel"),
        )

        self.assertTrue(all(os.access(command, os.X_OK) for command in commands))
        self.assertFalse((project / "bin").exists())

    def test_kitty_mappings_use_launcher_instead_of_ambient_python(self) -> None:
        project = Path(__file__).parents[1]
        packaged = project / "kisesh" / "integration"
        integration = (packaged / "kisesh.conf").read_text(encoding="utf-8")
        mappings = [line for line in integration.splitlines() if line.startswith("map ")]

        launch_mappings = [line for line in mappings if " launch " in line]
        self.assertEqual(len(mappings), 7)
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
        close_mapping = "map cmd+w kitten ~/.local/lib/kisesh/integration/actions.py safe-close"
        self.assertIn(close_mapping, mappings)
        reload_mappings = [
            line for line in mappings if line.startswith(("map ctrl+cmd+, ", "map ctrl+shift+f5 "))
        ]
        self.assertEqual(
            reload_mappings,
            [
                "map ctrl+cmd+, kitten ~/.local/lib/kisesh/integration/actions.py reload-config",
                "map ctrl+shift+f5 kitten ~/.local/lib/kisesh/integration/actions.py reload-config",
            ],
        )
        overlay_close = (
            "map --when-focus-on var:kisesh_ui cmd+w combine : last_used_layout : close_window"
        )
        self.assertLess(
            mappings.index(close_mapping),
            mappings.index(overlay_close),
        )
        self.assertTrue((packaged / "actions.py").is_file())
        layout_mapping = (
            "map --when-focus-on var:kisesh_session alt+z kitten "
            "~/.local/lib/kisesh/integration/actions.py layout-toggle"
        )
        self.assertIn(layout_mapping, mappings)
        self.assertNotIn(" undo", integration)
        self.assertNotIn(" park", integration)

    def test_kitty_resources_have_one_packaged_source_of_truth(self) -> None:
        project = Path(__file__).parents[1]
        packaged = project / "kisesh" / "integration"

        self.assertTrue((packaged / "kisesh.conf").is_file())
        self.assertTrue((packaged / "quick-access-terminal.conf").is_file())
        self.assertFalse((project / "integration").exists())
        self.assertFalse((project / "typings").exists())

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_actions_kitten_loads_without_file_inside_kitty_runtime(self) -> None:
        project = Path(__file__).parents[1]
        script = project / "kisesh/integration/actions.py"
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
        launcher = Path(sys.executable).with_name("kisesh-panel")
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

    def test_panel_launcher_resolves_installed_inputs_and_rejects_unsafe_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            invoked = runtime / "bin" / "kisesh-panel"
            (runtime / "integration").mkdir(parents=True)
            (runtime / "kisesh").mkdir()
            invoked.parent.mkdir(exist_ok=True)
            invoked.touch()
            kitten = root / "kitten"
            cli = root / "kisesh"
            invalid = root / "not-executable"
            for executable in (kitten, cli):
                executable.touch()
                executable.chmod(0o755)
            invalid.touch()

            self.assertEqual(
                panel_launcher._kitten_executable({"KISESH_KITTEN": str(kitten)}),
                kitten,
            )
            self.assertEqual(
                panel_launcher._cli_executable({"KISESH_CLI": str(cli)}, invoked),
                cli,
            )
            self.assertEqual(
                panel_launcher._runtime_root({"KISESH_INSTALL_ROOT": str(root)}, invoked),
                root,
            )
            self.assertEqual(panel_launcher._runtime_root({}, invoked), runtime)
            self.assertEqual(
                panel_launcher._target_socket({"KITTY_LISTEN_ON": "unix:/tmp/main"}),
                "unix:/tmp/main",
            )

            with self.assertRaisesRegex(panel_launcher.PanelLaunchError, "not executable"):
                panel_launcher._configured_executable(
                    {"KISESH_KITTEN": str(invalid)}, "KISESH_KITTEN"
                )
            for environment, message in (
                ({}, "unavailable"),
                ({"KISESH_TARGET_SOCKET": "fd:3"}, "persistent unix"),
            ):
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(panel_launcher.PanelLaunchError, message),
                ):
                    panel_launcher._target_socket(environment)

            fallback_kitten = root / "path" / "kitten"
            fallback_kitten.parent.mkdir()
            fallback_kitten.touch()
            fallback_kitten.chmod(0o755)
            with (
                mock.patch("kisesh.panel_launcher.shutil.which", return_value=str(fallback_kitten)),
                mock.patch(
                    "kisesh.panel_launcher._is_executable",
                    side_effect=lambda path: path == fallback_kitten,
                ),
            ):
                self.assertEqual(
                    panel_launcher._kitten_executable({"PATH": str(fallback_kitten.parent)}),
                    fallback_kitten,
                )

            with (
                mock.patch("kisesh.panel_launcher.shutil.which", return_value=None),
                mock.patch("kisesh.panel_launcher._is_executable", return_value=False),
            ):
                with self.assertRaisesRegex(
                    panel_launcher.PanelLaunchError, "kitten was not found"
                ):
                    panel_launcher._kitten_executable({})
                with self.assertRaisesRegex(
                    panel_launcher.PanelLaunchError, "command was not found"
                ):
                    panel_launcher._cli_executable({}, root / "missing" / "kisesh-panel")

    def test_panel_launcher_discovers_socket_and_propagates_toggle_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary) / "panel"
            with mock.patch.object(panel_launcher, "DEFAULT_PANEL_SOCKET", f"unix:{prefix}"):
                self.assertIsNone(panel_launcher._panel_socket({}))
                ordinary_path = prefix.with_name("panel-0")
                socket_path = prefix.with_name("panel-7")
                with (
                    mock.patch.object(
                        Path,
                        "glob",
                        return_value=[ordinary_path, socket_path],
                    ),
                    mock.patch.object(Path, "is_socket", side_effect=(False, True)),
                ):
                    self.assertEqual(panel_launcher._panel_socket({}), f"unix:{socket_path}")

        command = ["/kitten", "quick-access-terminal"]
        child_environment = {"KISESH_CALLER": "panel"}
        prepared = (command, child_environment, Path("/kitten"))
        failed: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(command, 7)
        succeeded: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(command, 0)
        wake_failed: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(command, 5)
        with (
            mock.patch("kisesh.panel_launcher._quick_access_command", return_value=prepared),
            mock.patch("kisesh.panel_launcher.subprocess.run", return_value=failed),
        ):
            self.assertEqual(panel_launcher.run([], {}), 7)
        with (
            mock.patch("kisesh.panel_launcher._quick_access_command", return_value=prepared),
            mock.patch("kisesh.panel_launcher._panel_socket", return_value=None),
            mock.patch("kisesh.panel_launcher.subprocess.run", return_value=succeeded),
        ):
            self.assertEqual(panel_launcher.run([], {}), 0)
        with (
            mock.patch("kisesh.panel_launcher._quick_access_command", return_value=prepared),
            mock.patch("kisesh.panel_launcher._panel_socket", return_value="unix:/tmp/panel.sock"),
            mock.patch(
                "kisesh.panel_launcher.subprocess.run",
                side_effect=(succeeded, wake_failed),
            ) as run,
        ):
            self.assertEqual(panel_launcher.run([], {}), 5)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "ctrl+g")

    def test_panel_launcher_reports_expected_setup_errors_without_a_traceback(self) -> None:
        with (
            mock.patch(
                "kisesh.panel_launcher.run",
                side_effect=panel_launcher.PanelLaunchError("socket unavailable"),
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(panel_launcher.main([]), 1)

        self.assertIn("socket unavailable", stderr.write.call_args_list[0].args[0])

    def test_optional_quick_access_profile_remains_centered_and_theme_aware(self) -> None:
        project = Path(__file__).parents[1]
        profile = (project / "kisesh/integration/quick-access-terminal.conf").read_text(
            encoding="utf-8"
        )

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
