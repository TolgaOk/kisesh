from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kitty_workbench.installer import INTEGRATION_INCLUDE, MANAGED_BEGIN, MANAGED_END

PROJECT = Path(__file__).parents[1]
INSTALLER = PROJECT / "install"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.kitty = self.fake_bin / "kitty"
        self.kitten = self.fake_bin / "kitten"
        self.kitty.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "text = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')\n"
            "bad = []\n"
            "if 'FAKE_INVALID_KITTY_SETTING' in text:\n"
            "    bad.append('unknown config key: FAKE_INVALID_KITTY_SETTING')\n"
            "if os.environ.get('KITTY_WORKBENCH_FAKE_REJECT_MANAGED') and "
            "'# BEGIN kitty-workbench' in text:\n"
            "    bad.append('rejected managed Workbench block')\n"
            "allow = 'no'\n"
            "listen = 'none'\n"
            "for raw in text.splitlines():\n"
            "    fields = raw.strip().split(maxsplit=1)\n"
            "    if len(fields) == 2 and fields[0] == 'allow_remote_control':\n"
            "        allow = fields[1]\n"
            "    if len(fields) == 2 and fields[0] == 'listen_on':\n"
            "        listen = fields[1]\n"
            "print(json.dumps({'bad': bad, 'allow': allow, 'listen': listen}))\n",
            encoding="utf-8",
        )
        self.kitten.write_text(
            '#!/bin/sh\ntest "${1:-}" = quick-access-terminal || exit 2\nexit 0\n',
            encoding="utf-8",
        )
        self.kitty.chmod(0o755)
        self.kitten.chmod(0o755)
        self.config = self.home / "config" / "kitty" / "kitty.conf"
        self.tab_bar = self.config.parent / "tab_bar.py"
        self.target = self.home / ".local" / "lib" / "kitty-workbench"
        self.data = self.home / "data" / "kitty-workbench"
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / "config"),
                "XDG_DATA_HOME": str(self.home / "data"),
                "KITTY_WORKBENCH_KITTY": str(self.kitty),
                "KITTY_WORKBENCH_KITTEN": str(self.kitten),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(INSTALLER), *arguments],
            cwd=PROJECT,
            env=environment or self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def write_config(self, content: str) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(content, encoding="utf-8")

    def test_launcher_is_executable_and_finds_python_with_a_gui_style_path(self) -> None:
        homebrew_bin = self.home / "homebrew" / "bin"
        homebrew_bin.mkdir(parents=True)
        (homebrew_bin / "python3").symlink_to(Path(sys.executable).resolve())
        environment = dict(self.environment)
        environment["PATH"] = "/usr/bin:/bin"

        result = self.run_installer("--help", environment=environment)

        self.assertTrue(os.access(INSTALLER, os.X_OK))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--disable", result.stdout)
        self.assertIn("--uninstall", result.stdout)
        self.assertIn("--purge", result.stdout)

    def test_enable_adopts_manual_include_and_is_idempotent(self) -> None:
        original = (
            "font_size 14\n"
            "allow_remote_control yes\n"
            "listen_on unix:/tmp/existing-kitty\n"
            "map alt+s launch old-session-manager\n"
            f"{INTEGRATION_INCLUDE}\n"
        )
        self.write_config(original)

        first = self.run_installer()
        enabled = self.config.read_text(encoding="utf-8")
        second = self.run_installer("--enable")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.target.resolve(), PROJECT.resolve())
        self.assertEqual(enabled, self.config.read_text(encoding="utf-8"))
        self.assertEqual(enabled.count(MANAGED_BEGIN), 1)
        self.assertEqual(enabled.count(MANAGED_END), 1)
        self.assertEqual(enabled.count(INTEGRATION_INCLUDE), 1)
        self.assertIn("font_size 14", enabled)
        self.assertIn("allow_remote_control yes", enabled)
        self.assertIn("listen_on unix:/tmp/existing-kitty", enabled)
        self.assertNotIn("allow_remote_control socket-only", enabled)
        self.assertIn("takes precedence over existing mappings for alt+s", first.stderr)
        self.assertIn("already enabled", second.stdout)
        backup = self.config.with_name("kitty.conf.kitty-workbench.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_existing_custom_tab_bar_is_restored_exactly_on_disable(self) -> None:
        self.write_config("font_size 14\n")
        original = "def draw_tab(*args):\n    return 17\n"
        self.tab_bar.write_text(original, encoding="utf-8")
        self.tab_bar.chmod(0o640)

        enabled = self.run_installer()

        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertTrue(self.tab_bar.is_symlink())
        self.assertEqual(
            self.tab_bar.resolve(),
            (PROJECT / "integration" / "tab_bar.py").resolve(),
        )
        self.assertFalse(self.tab_bar.with_suffix(".py.kitty-workbench.bak").exists())

        disabled = self.run_installer("--disable")

        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertFalse(self.tab_bar.is_symlink())
        self.assertEqual(self.tab_bar.read_text(encoding="utf-8"), original)
        self.assertEqual(self.tab_bar.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.data / ".integration" / "tab-bar.json").exists())

    def test_disable_refuses_a_user_modified_tab_bar_without_touching_config(self) -> None:
        self.write_config("font_size 14\n")
        self.tab_bar.write_text("original = True\n", encoding="utf-8")
        self.assertEqual(self.run_installer().returncode, 0)
        enabled_config = self.config.read_text(encoding="utf-8")
        self.tab_bar.unlink()
        self.tab_bar.write_text("new_user_bar = True\n", encoding="utf-8")

        disabled = self.run_installer("--disable")

        self.assertNotEqual(disabled.returncode, 0)
        self.assertIn("modified custom tab bar", disabled.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), enabled_config)
        self.assertEqual(self.tab_bar.read_text(encoding="utf-8"), "new_user_bar = True\n")

    def test_enable_warns_when_it_takes_over_native_tab_close(self) -> None:
        self.write_config("map cmd+w close_tab\n")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("takes precedence over existing mappings for cmd+w", result.stderr)

    def test_fresh_config_gets_only_the_required_remote_control_defaults(self) -> None:
        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        configured = self.config.read_text(encoding="utf-8")
        self.assertEqual(configured.count(MANAGED_BEGIN), 1)
        self.assertIn("allow_remote_control socket-only", configured)
        self.assertIn("listen_on unix:/tmp/kitty-workbench-main", configured)
        self.assertIn(INTEGRATION_INCLUDE, configured)
        self.assertTrue(self.tab_bar.is_symlink())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.config.with_name("kitty.conf.kitty-workbench.bak").exists())

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_real_kitty_parser_accepts_the_complete_fresh_install(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / "real-config"),
                "XDG_DATA_HOME": str(self.home / "real-data"),
            }
        )
        environment.pop("KITTY_WORKBENCH_KITTY", None)
        environment.pop("KITTY_WORKBENCH_KITTEN", None)

        result = self.run_installer(environment=environment)
        config = self.home / "real-config" / "kitty" / "kitty.conf"

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("allow_remote_control socket-only", config.read_text(encoding="utf-8"))
        self.assertTrue(self.target.is_symlink())
        tab_bar = config.parent / "tab_bar.py"
        self.assertTrue(tab_bar.is_symlink())
        loader = (
            "import runpy,sys; "
            "loaded=runpy.run_path(sys.argv[1]); "
            "print(loaded['draw_tab'].__module__)"
        )
        loaded = subprocess.run(
            ["kitty", "+runpy", loader, str(tab_bar)],
            cwd=PROJECT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.strip(), "kitty_workbench.session_bar")

    def test_disable_uninstall_and_purge_have_distinct_data_boundaries(self) -> None:
        self.assertEqual(self.run_installer().returncode, 0)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "session.json").write_text("saved", encoding="utf-8")
        data_sibling = self.data.parent / "keep-data"
        data_sibling.write_text("keep", encoding="utf-8")
        with self.config.open("a", encoding="utf-8") as handle:
            handle.write("font_size 17\n")

        disabled = self.run_installer("--disable")
        disabled_config = self.config.read_text(encoding="utf-8")

        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertNotIn(MANAGED_BEGIN, disabled_config)
        self.assertNotIn(INTEGRATION_INCLUDE, disabled_config)
        self.assertIn("font_size 17", disabled_config)
        self.assertTrue(self.target.is_symlink())
        self.assertTrue((self.data / "session.json").is_file())
        self.assertFalse(self.tab_bar.exists())
        self.assertFalse(self.tab_bar.is_symlink())

        self.assertEqual(self.run_installer("--enable").returncode, 0)
        uninstalled = self.run_installer("--uninstall")

        self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
        self.assertFalse(self.target.exists())
        self.assertNotIn(MANAGED_BEGIN, self.config.read_text(encoding="utf-8"))
        self.assertTrue((self.data / "session.json").is_file())
        self.assertIn("sessions preserved", uninstalled.stdout)

        purged = self.run_installer("--purge")

        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(self.data.exists())
        self.assertEqual(data_sibling.read_text(encoding="utf-8"), "keep")
        self.assertTrue(self.config.is_file())
        self.assertIn("font_size 17", self.config.read_text(encoding="utf-8"))

    def test_conflicting_actions_fail_before_mutating_config_or_sessions(self) -> None:
        original = "font_size 16\n"
        self.write_config(original)
        self.data.mkdir(parents=True)
        saved = self.data / "session.json"
        saved.write_text("saved", encoding="utf-8")

        result = self.run_installer("--disable", "--purge")

        self.assertEqual(result.returncode, 1)
        self.assertIn("choose only one", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertEqual(saved.read_text(encoding="utf-8"), "saved")
        self.assertFalse(self.target.exists())

    def test_failed_candidate_validation_rolls_back_link_and_config(self) -> None:
        original = "font_size 15\nallow_remote_control yes\nlisten_on unix:/tmp/main\n"
        self.write_config(original)
        environment = dict(self.environment)
        environment["KITTY_WORKBENCH_FAKE_REJECT_MANAGED"] = "1"

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Workbench-enabled kitty.conf", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.config.with_name("kitty.conf.kitty-workbench.bak").exists())

    def test_existing_invalid_config_is_reported_without_modification(self) -> None:
        original = "FAKE_INVALID_KITTY_SETTING yes\n"
        self.write_config(original)

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing kitty.conf contains Kitty configuration errors", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(self.target.exists())

    def test_uninstall_refuses_a_foreign_install_path_before_editing_config(self) -> None:
        foreign = self.root / "foreign-project"
        foreign.mkdir()
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(foreign, target_is_directory=True)
        original = f"font_size 16\n{MANAGED_BEGIN}\n{INTEGRATION_INCLUDE}\n{MANAGED_END}\n"
        self.write_config(original)

        result = self.run_installer("--uninstall")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to remove existing install path", result.stderr)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.target.resolve(), foreign.resolve())
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_disable_refuses_an_unterminated_managed_block(self) -> None:
        original = f"font_size 16\n{MANAGED_BEGIN}\n{INTEGRATION_INCLUDE}\n"
        self.write_config(original)

        result = self.run_installer("--disable")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unterminated", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
