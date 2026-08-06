from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from kisesh.panel import (
    PanelError,
    _find_kitten,
    _run_panel_command,
    hide_quick_access_panel,
    is_panel_process,
)
from tests.fakes import RecordingCommandRunner


class PanelTests(unittest.TestCase):
    def test_hide_targets_the_existing_named_quick_access_instance(self) -> None:
        runner = RecordingCommandRunner()

        with patch.dict(
            "os.environ",
            {"KISESH_PANEL_CONFIG": "/kisesh/panel.conf"},
            clear=False,
        ):
            hide_quick_access_panel(
                executable="/kitten",
                instance_group="kisesh-preview",
                runner=runner,
            )

        self.assertEqual(
            runner.commands,
            [
                [
                    "/kitten",
                    "quick-access-terminal",
                    "--instance-group=kisesh-preview",
                    "--config=/kisesh/panel.conf",
                ]
            ],
        )

    def test_hide_reports_toggle_failures_instead_of_killing_the_manager(self) -> None:
        runner = RecordingCommandRunner(stderr="no instance", returncode=1)

        with self.assertRaisesRegex(PanelError, "no instance"):
            hide_quick_access_panel(
                executable="/kitten",
                instance_group="missing",
                runner=runner,
            )

        for runner, message in (
            (RecordingCommandRunner(stdout="stdout failure", returncode=2), "stdout failure"),
            (RecordingCommandRunner(returncode=3), "3"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(PanelError, message):
                hide_quick_access_panel(
                    executable="/kitten",
                    instance_group="missing",
                    runner=runner,
                )

        def unavailable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            raise OSError("spawn failed")

        with self.assertRaisesRegex(PanelError, "spawn failed"):
            hide_quick_access_panel(
                executable="/kitten",
                instance_group="missing",
                runner=unavailable,
            )

    def test_hide_requires_an_instance_group_before_spawning(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(PanelError, "instance group is unavailable"),
        ):
            hide_quick_access_panel(executable="/kitten")

    def test_default_panel_runner_executes_the_exact_bounded_command(self) -> None:
        result = _run_panel_command(
            ["/usr/bin/true"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)

    def test_kitten_resolution_uses_path_macos_fallback_and_clear_failure(self) -> None:
        with patch.object(shutil, "which", return_value="/bin/kitten"):
            self.assertEqual(_find_kitten(), "/bin/kitten")

        def macos_only(path: Path) -> bool:
            return str(path) == "/Applications/kitty.app/Contents/MacOS/kitten"

        with (
            patch.object(shutil, "which", return_value=None),
            patch.object(Path, "is_file", autospec=True, side_effect=macos_only),
        ):
            self.assertEqual(_find_kitten(), "/Applications/kitty.app/Contents/MacOS/kitten")

        with (
            patch.object(shutil, "which", return_value=None),
            patch.object(Path, "is_file", return_value=False),
            self.assertRaisesRegex(PanelError, "cannot find the kitten"),
        ):
            _find_kitten()

    def test_panel_mode_is_explicit_not_inferred_from_an_os_window(self) -> None:
        with patch.dict(
            "os.environ",
            {"KISESH_CALLER": "panel"},
            clear=True,
        ):
            self.assertTrue(is_panel_process())
        with patch.dict(
            "os.environ",
            {"KISESH_CALLER": "manager"},
            clear=True,
        ):
            self.assertFalse(is_panel_process())


if __name__ == "__main__":
    unittest.main()
