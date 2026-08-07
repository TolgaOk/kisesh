from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kisesh.installer import (
    COMPAT_MANAGED_BEGIN,
    COMPAT_MANAGED_END,
    DEFAULT_LISTEN_ON,
    INTEGRATION_INCLUDE,
    MANAGED_BEGIN,
    MANAGED_END,
    ConfigProbe,
    InstallArguments,
    InstallError,
    InstallPaths,
    _app_config_content,
    _backup_once,
    _check_install_target,
    _console_launcher,
    _disable,
    _editable_config,
    _enable,
    _expand_home,
    _find_executable,
    _home,
    _kitty_config,
    _probe_config,
    _read_config,
    _remove_product_data,
    _strip_kisesh_config,
    _uninstall,
    _validate_source,
    main,
)
from kisesh.tab_bar_install import install_tab_bar, tab_bar_paths

PROJECT = Path(__file__).parents[1]


class InstallerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def paths(self, *, source: Path = PROJECT) -> InstallPaths:
        return InstallPaths(
            home=self.home,
            source=source,
            launcher=PROJECT / ".venv" / "bin" / "kisesh",
            panel_launcher=PROJECT / ".venv" / "bin" / "kisesh-panel",
            target=self.home / ".local" / "lib" / "kisesh",
            kitty_config=self.home / ".config" / "kitty" / "kitty.conf",
            app_config=self.home / ".config" / "kisesh" / "apps.toml",
            data=self.home / ".local" / "share" / "kisesh",
        )

    def test_home_and_config_resolution_follow_every_documented_precedence(self) -> None:
        self.assertEqual(_expand_home("~", self.home), self.home)
        self.assertEqual(_expand_home("~/config", self.home), self.home / "config")
        self.assertEqual(_expand_home("/absolute", self.home), Path("/absolute"))

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(InstallError, "HOME is unavailable"),
        ):
            _home()

        scenarios: tuple[tuple[Path | None, dict[str, str], Path], ...] = (
            (Path("~/explicit.conf"), {}, self.home / "explicit.conf"),
            (
                None,
                {"KISESH_KITTY_CONFIG": "~/kisesh.conf"},
                self.home / "kisesh.conf",
            ),
            (
                None,
                {"KITTY_CONFIG_DIRECTORY": "~/kitty-directory"},
                self.home / "kitty-directory" / "kitty.conf",
            ),
            (
                None,
                {"XDG_CONFIG_HOME": "~/xdg"},
                self.home / "xdg" / "kitty" / "kitty.conf",
            ),
        )
        for override, environment, expected in scenarios:
            with (
                self.subTest(expected=expected),
                mock.patch.dict("os.environ", environment, clear=True),
            ):
                self.assertEqual(_kitty_config(self.home, override), expected)

        macos = self.home / "Library" / "Preferences" / "kitty" / "kitty.conf"
        macos.parent.mkdir(parents=True)
        macos.touch()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_kitty_config(self.home), macos)
        conventional = self.home / ".config" / "kitty" / "kitty.conf"
        conventional.parent.mkdir(parents=True)
        conventional.touch()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_kitty_config(self.home), conventional)

    def test_source_and_target_checks_fail_closed_without_replacing_foreign_files(self) -> None:
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        with self.assertRaisesRegex(InstallError, "package is incomplete"):
            _validate_source(self.paths(source=incomplete))

        with (
            mock.patch("kisesh.installer.validate_runtime_source"),
            self.assertRaisesRegex(InstallError, "default_apps.toml"),
        ):
            _validate_source(self.paths(source=incomplete))

        in_place = self.paths(source=PROJECT)
        in_place = InstallPaths(
            home=in_place.home,
            source=PROJECT,
            launcher=in_place.launcher,
            panel_launcher=in_place.panel_launcher,
            target=PROJECT,
            kitty_config=in_place.kitty_config,
            app_config=in_place.app_config,
            data=in_place.data,
        )
        _check_install_target(in_place)

        app_directory = self.paths().app_config
        app_directory.mkdir(parents=True)
        with self.assertRaisesRegex(InstallError, "app config is not a file"):
            paths = self.paths()
            _app_config_content(paths)

    def test_console_launcher_resolves_each_install_style_and_rejects_missing_commands(
        self,
    ) -> None:
        executable = self.root / "valid" / "kisesh"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

        with mock.patch.dict("os.environ", {"KISESH_CLI": str(executable)}, clear=True):
            self.assertEqual(
                _console_launcher(self.root / "source", "kisesh", "KISESH_CLI"), executable
            )

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(sys, "argv", [str(executable)]),
        ):
            self.assertEqual(
                _console_launcher(self.root / "source", "kisesh", "KISESH_CLI"), executable
            )

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(sys, "argv", ["kisesh"]),
            mock.patch.object(shutil, "which", return_value=str(executable)),
        ):
            self.assertEqual(
                _console_launcher(self.root / "source", "kisesh", "KISESH_CLI"), executable
            )

        unavailable = self.root / "unavailable"
        unavailable.write_text("not executable", encoding="utf-8")
        with (
            mock.patch.dict("os.environ", {"KISESH_CLI": str(unavailable)}, clear=True),
            mock.patch.object(sys, "argv", ["test-runner"]),
            mock.patch.object(sys, "executable", str(self.root / "missing-python")),
            self.assertRaisesRegex(InstallError, "launcher was not found"),
        ):
            _console_launcher(self.root / "missing-source", "kisesh", "KISESH_CLI")

    def test_config_stripping_rejects_nested_and_unmatched_markers(self) -> None:
        paths = self.paths()
        cases = (
            (f"{MANAGED_BEGIN}\n{MANAGED_BEGIN}\n", "nested"),
            (f"{MANAGED_END}\n", "unmatched"),
        )
        for content, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(InstallError, message):
                _strip_kisesh_config(content, paths)

        absolute = f"include {paths.target / 'integration' / 'kisesh.conf'}\n"
        stripped, changed = _strip_kisesh_config(f"font_size 14\n{absolute}", paths)
        self.assertTrue(changed)
        self.assertEqual(stripped, "font_size 14\n")

        stripped, changed = _strip_kisesh_config(
            f"font_size 16\n{COMPAT_MANAGED_BEGIN}\n{INTEGRATION_INCLUDE}\n{COMPAT_MANAGED_END}\n",
            paths,
        )
        self.assertTrue(changed)
        self.assertEqual(stripped, "font_size 16\n")

    def test_config_symlinks_preserve_targets_and_report_resolution_or_read_errors(self) -> None:
        target = self.root / "real-kitty.conf"
        target.write_text("font_size 14\n", encoding="utf-8")
        link = self.root / "kitty.conf"
        link.symlink_to(target)
        self.assertEqual(_editable_config(link), target.resolve())
        self.assertEqual(_read_config(self.root / "missing"), "")

        broken = self.root / "broken.conf"
        broken.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(InstallError, "cannot resolve Kitty config symlink"):
            _editable_config(broken)

        with (
            mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")),
            self.assertRaisesRegex(InstallError, "cannot read Kitty config"),
        ):
            _read_config(target)

    def test_backup_is_once_only_and_copy_failures_leave_a_clear_error(self) -> None:
        config = self.root / "kitty.conf"
        self.assertIsNone(_backup_once(config))
        config.write_text("font_size 14\n", encoding="utf-8")
        backup = _backup_once(config)
        self.assertIsNotNone(backup)
        assert backup is not None
        backup.write_text("original backup\n", encoding="utf-8")
        self.assertEqual(_backup_once(config), backup)
        self.assertEqual(backup.read_text(encoding="utf-8"), "original backup\n")

        backup.unlink()
        with (
            mock.patch.object(shutil, "copy2", side_effect=OSError("disk full")),
            self.assertRaisesRegex(InstallError, "cannot back up"),
        ):
            _backup_once(config)

    def test_executable_resolution_covers_explicit_path_path_app_and_failure(self) -> None:
        configured = self.root / "configured-kitty"
        configured.write_text("binary", encoding="utf-8")
        configured.chmod(0o755)
        with mock.patch.dict("os.environ", {"TEST_KITTY": str(configured)}, clear=True):
            self.assertEqual(_find_executable("TEST_KITTY", "kitty", "/app/kitty"), str(configured))

        with (
            mock.patch.dict("os.environ", {"TEST_KITTY": "/missing"}, clear=True),
            self.assertRaisesRegex(InstallError, "is not executable"),
        ):
            _find_executable("TEST_KITTY", "kitty", "/app/kitty")

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(shutil, "which", return_value="/path/kitty"),
        ):
            self.assertEqual(_find_executable("TEST_KITTY", "kitty", "/app/kitty"), "/path/kitty")

        def app_only(path: Path) -> bool:
            return str(path) == "/app/kitty"

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.object(Path, "is_file", autospec=True, side_effect=app_only),
            mock.patch.object(os, "access", return_value=True),
        ):
            self.assertEqual(_find_executable("TEST_KITTY", "kitty", "/app/kitty"), "/app/kitty")
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.object(Path, "is_file", return_value=False),
            self.assertRaisesRegex(InstallError, "kitty was not found"),
        ):
            _find_executable("TEST_KITTY", "kitty", "/app/kitty")

    def test_config_probe_translates_every_process_boundary(self) -> None:
        config = self.root / "kitty" / "kitty.conf"
        failures = (
            (
                subprocess.CompletedProcess([], 2, stdout="", stderr="parser failed"),
                "parser failed",
            ),
            (subprocess.CompletedProcess([], 3, stdout="fallback", stderr=""), "fallback"),
            (subprocess.CompletedProcess([], 0, stdout="not-json", stderr=""), "unreadable"),
            (subprocess.CompletedProcess([], 0, stdout='{"bad":"wrong"}', stderr=""), "invalid"),
        )
        for result, message in failures:
            with (
                self.subTest(message=message),
                mock.patch.object(subprocess, "run", return_value=result),
                self.assertRaisesRegex(InstallError, message),
            ):
                _probe_config("/kitty", config, "font_size 14\n")
        with (
            mock.patch.object(
                subprocess,
                "run",
                side_effect=OSError("spawn failed"),
            ),
            self.assertRaisesRegex(InstallError, "cannot validate"),
        ):
            _probe_config("/kitty", config, "")

    def test_enable_rejects_final_remote_control_and_socket_state_without_mutation(self) -> None:
        paths = self.paths()
        valid = ConfigProbe((), "socket-only", DEFAULT_LISTEN_ON)
        invalid_finals = (
            (ConfigProbe((), "no", DEFAULT_LISTEN_ON), "remote control"),
            (ConfigProbe((), "socket-only", "none"), "listen_on"),
        )
        for final, message in invalid_finals:
            with (
                self.subTest(message=message),
                mock.patch("kisesh.installer._validate_source"),
                mock.patch("kisesh.installer._check_install_target"),
                mock.patch("kisesh.installer._find_executable", return_value="/binary"),
                mock.patch("kisesh.installer.deploy_runtime") as deploy,
                mock.patch("kisesh.installer.rollback_runtime"),
                mock.patch("kisesh.installer._probe_config", side_effect=(valid, final)),
                self.assertRaisesRegex(InstallError, message),
            ):
                deploy.return_value = mock.MagicMock(changed=False)
                _enable(paths)
        self.assertFalse(paths.kitty_config.exists())
        self.assertFalse(paths.target.exists())

    def test_enable_failure_preserves_a_package_manager_owned_command(self) -> None:
        paths = self.paths()
        command = paths.home / ".local" / "bin" / "kisesh"
        command.parent.mkdir(parents=True)
        command.symlink_to(paths.launcher)
        valid = ConfigProbe((), "socket-only", DEFAULT_LISTEN_ON)
        invalid = ConfigProbe((), "no", DEFAULT_LISTEN_ON)

        with (
            mock.patch("kisesh.installer._validate_source"),
            mock.patch("kisesh.installer._check_install_target"),
            mock.patch("kisesh.installer._find_executable", return_value="/binary"),
            mock.patch("kisesh.installer.deploy_runtime") as deploy,
            mock.patch("kisesh.installer.rollback_runtime"),
            mock.patch("kisesh.installer._probe_config", side_effect=(valid, invalid)),
            self.assertRaisesRegex(InstallError, "remote control"),
        ):
            deploy.return_value = mock.MagicMock(changed=False)
            _enable(paths)

        self.assertTrue(command.is_symlink())
        self.assertEqual(command.resolve(), paths.launcher.resolve())

    def test_enable_reports_a_previous_runtime_that_cannot_be_cleaned_up(self) -> None:
        paths = self.paths()
        valid = ConfigProbe((), "socket-only", DEFAULT_LISTEN_ON)
        errors = io.StringIO()
        output = io.StringIO()

        with (
            mock.patch("kisesh.installer._find_executable", return_value="/binary"),
            mock.patch("kisesh.installer._probe_config", side_effect=(valid, valid)),
            mock.patch("kisesh.installer.finish_runtime", side_effect=OSError("busy")),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            _enable(paths)

        self.assertIn("previous runtime remains", errors.getvalue())
        self.assertTrue(paths.target.is_dir())

    def test_enable_restores_the_previous_tab_bar_when_config_write_fails(self) -> None:
        paths = self.paths()
        paths.app_config.parent.mkdir(parents=True)
        paths.kitty_config.parent.mkdir(parents=True)
        paths.kitty_config.write_text(
            f"allow_remote_control socket-only\nlisten_on {DEFAULT_LISTEN_ON}\n",
            encoding="utf-8",
        )
        tab_bar = paths.kitty_config.parent / "tab_bar.py"
        tab_bar.write_text("original = True\n", encoding="utf-8")
        valid = ConfigProbe((), "socket-only", DEFAULT_LISTEN_ON)

        with (
            mock.patch("kisesh.installer._find_executable", return_value="/binary"),
            mock.patch("kisesh.installer._probe_config", side_effect=(valid, valid)),
            mock.patch("kisesh.installer._atomic_write", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            _enable(paths)

        self.assertFalse(tab_bar.is_symlink())
        self.assertEqual(tab_bar.read_text(encoding="utf-8"), "original = True\n")
        self.assertFalse(paths.target.exists())
        self.assertTrue(paths.app_config.parent.is_dir())
        self.assertFalse(paths.app_config.exists())
        self.assertFalse((paths.data / ".integration" / "tab-bar.json").exists())

    def test_enable_removes_a_new_app_config_directory_after_write_failure(self) -> None:
        """Roll back the first-use XDG directory with the rest of the transaction."""
        paths = self.paths()
        valid = ConfigProbe((), "socket-only", DEFAULT_LISTEN_ON)

        with (
            mock.patch("kisesh.installer._find_executable", return_value="/binary"),
            mock.patch("kisesh.installer._probe_config", side_effect=(valid, valid)),
            mock.patch("kisesh.installer._atomic_write", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            _enable(paths)

        self.assertFalse(paths.app_config.exists())
        self.assertFalse(paths.app_config.parent.exists())

    def test_disable_reinstalls_the_native_bar_when_config_write_fails(self) -> None:
        paths = self.paths()
        paths.target.mkdir(parents=True)
        (paths.target / "integration").symlink_to(
            PROJECT / "kisesh" / "integration", target_is_directory=True
        )
        paths.kitty_config.parent.mkdir(parents=True)
        paths.kitty_config.write_text(
            f"{MANAGED_BEGIN}\n{INTEGRATION_INCLUDE}\n{MANAGED_END}\n",
            encoding="utf-8",
        )
        tab_bar = paths.kitty_config.parent / "tab_bar.py"
        tab_bar.write_text("original = True\n", encoding="utf-8")
        bar_paths = tab_bar_paths(paths.kitty_config, paths.target, paths.data)
        install_tab_bar(bar_paths)

        with (
            mock.patch("kisesh.installer._atomic_write", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            _disable(paths)

        self.assertTrue(bar_paths.live.is_symlink())
        self.assertEqual(
            bar_paths.live.resolve(), (PROJECT / "kisesh" / "integration" / "tab_bar.py").resolve()
        )
        self.assertTrue(bar_paths.state.exists())

    def test_disable_config_failure_without_a_managed_bar_has_no_bar_rollback(self) -> None:
        paths = self.paths()
        paths.kitty_config.parent.mkdir(parents=True)
        paths.kitty_config.write_text(
            f"{MANAGED_BEGIN}\n{INTEGRATION_INCLUDE}\n{MANAGED_END}\n",
            encoding="utf-8",
        )

        with (
            mock.patch("kisesh.installer._atomic_write", side_effect=OSError("disk full")),
            mock.patch("kisesh.installer.install_tab_bar") as reinstall,
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            _disable(paths)

        reinstall.assert_not_called()

    def test_purge_guards_scope_unlinks_symlinks_and_reports_already_absent_data(self) -> None:
        paths = self.paths()
        with self.assertRaisesRegex(InstallError, "unsafe purge path"):
            _remove_product_data(self.root / "other", self.root)

        paths.data.parent.mkdir(parents=True)
        external = self.root / "external-data"
        external.mkdir()
        paths.data.symlink_to(external, target_is_directory=True)
        self.assertTrue(_remove_product_data(paths.data, paths.data.parent))
        self.assertFalse(paths.data.exists())
        self.assertTrue(external.exists())
        self.assertFalse(_remove_product_data(paths.data, paths.data.parent))

        output = io.StringIO()
        with redirect_stdout(output):
            _uninstall(paths, purge=True)
        self.assertIn("session data already absent", output.getvalue())

    def test_disable_handles_a_config_removed_between_read_and_backup(self) -> None:
        paths = self.paths()
        output = io.StringIO()
        with (
            mock.patch(
                "kisesh.installer._read_config",
                return_value=f"{INTEGRATION_INCLUDE}\n",
            ),
            redirect_stdout(output),
        ):
            self.assertTrue(_disable(paths))
        self.assertNotIn("backup:", output.getvalue())
        self.assertEqual(paths.kitty_config.read_text(encoding="utf-8"), "")

    def test_main_formats_operating_system_failures_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch(
                "kisesh.installer.parse_arguments",
                return_value=InstallArguments(),
            ),
            mock.patch("kisesh.installer.install_paths", return_value=self.paths()),
            mock.patch("kisesh.installer._enable", side_effect=OSError("disk failed")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main([]), 1)
        self.assertEqual(stderr.getvalue(), "kisesh installer: disk failed\n")


if __name__ == "__main__":
    unittest.main()
