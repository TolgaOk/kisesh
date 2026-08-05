from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kitty_workbench.tab_bar_install import (
    BackupRecord,
    TabBarInstallError,
    TabBarPaths,
    _is_managed,
    _load_record,
    _record_original,
    install_tab_bar,
    restore_tab_bar,
    tab_bar_paths,
)


class TabBarInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config" / "kitty" / "kitty.conf"
        self.config.parent.mkdir(parents=True)
        self.source_root = self.root / "install"
        self.source = self.source_root / "integration" / "tab_bar.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("def draw_tab():\n    return 1\n", encoding="utf-8")
        self.data = self.root / "data" / "kitty-workbench"
        self.paths = tab_bar_paths(self.config, self.source_root, self.data)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_regular_custom_bar_round_trips_content_mode_and_idempotency(self) -> None:
        original = "def draw_tab():\n    return 'original'\n"
        self.paths.live.write_text(original, encoding="utf-8")
        self.paths.live.chmod(0o640)

        self.assertTrue(install_tab_bar(self.paths))
        self.assertTrue(_is_managed(self.paths))
        self.assertEqual(self.paths.live.resolve(), self.source.resolve())
        self.assertEqual(self.paths.backup.read_text(encoding="utf-8"), original)
        self.assertEqual(_load_record(self.paths), BackupRecord("file"))
        state = self.paths.state.read_text(encoding="utf-8")

        self.assertFalse(install_tab_bar(self.paths))
        self.assertEqual(self.paths.state.read_text(encoding="utf-8"), state)
        self.assertTrue(restore_tab_bar(self.paths))
        self.assertFalse(self.paths.live.is_symlink())
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), original)
        self.assertEqual(self.paths.live.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.backup.exists())
        self.assertFalse(restore_tab_bar(self.paths))

    def test_absent_and_symlink_custom_bars_restore_their_exact_original_kind(self) -> None:
        self.assertTrue(install_tab_bar(self.paths))
        self.assertTrue(self.paths.live.is_symlink())
        self.assertTrue(restore_tab_bar(self.paths))
        self.assertFalse(self.paths.live.exists())
        self.assertFalse(self.paths.live.is_symlink())

        legacy = self.paths.live.parent / "legacy.py"
        legacy.write_text("legacy = True\n", encoding="utf-8")
        self.paths.live.symlink_to("legacy.py")
        self.assertTrue(install_tab_bar(self.paths))
        self.assertTrue(restore_tab_bar(self.paths))
        self.assertTrue(self.paths.live.is_symlink())
        self.assertEqual(os.readlink(self.paths.live), "legacy.py")

    def test_preexisting_workbench_link_is_treated_as_user_owned_original_state(self) -> None:
        self.paths.live.symlink_to(self.paths.source)

        self.assertFalse(install_tab_bar(self.paths))
        self.assertEqual(_load_record(self.paths), BackupRecord("symlink", str(self.paths.source)))
        self.assertTrue(restore_tab_bar(self.paths))
        self.assertTrue(self.paths.live.is_symlink())
        self.assertEqual(self.paths.live.resolve(), self.paths.source.resolve())

    def test_modified_live_bar_and_missing_backup_fail_without_overwriting_either(self) -> None:
        original = "original = True\n"
        self.paths.live.write_text(original, encoding="utf-8")
        install_tab_bar(self.paths)
        self.paths.live.unlink()
        self.paths.live.write_text("user_change = True\n", encoding="utf-8")

        with self.assertRaisesRegex(TabBarInstallError, "modified custom tab bar"):
            restore_tab_bar(self.paths)
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), "user_change = True\n")
        self.assertTrue(self.paths.state.exists())

        self.paths.live.unlink()
        self.paths.live.symlink_to(self.paths.source)
        self.paths.backup.unlink()
        with self.assertRaisesRegex(TabBarInstallError, "backup is missing"):
            restore_tab_bar(self.paths)
        self.assertTrue(_is_managed(self.paths))

    def test_interrupted_install_and_restore_states_recover_on_the_next_action(self) -> None:
        original = "original = True\n"
        self.paths.live.write_text(original, encoding="utf-8")
        install_tab_bar(self.paths)
        self.paths.live.unlink()

        self.assertTrue(restore_tab_bar(self.paths))
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), original)

        install_tab_bar(self.paths)
        self.paths.live.unlink()
        shutil.copy2(self.paths.backup, self.paths.live)
        self.assertTrue(restore_tab_bar(self.paths))
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), original)
        self.assertFalse(self.paths.state.exists())

    def test_invalid_recovery_records_and_unsafe_live_types_are_rejected(self) -> None:
        invalid_payloads: tuple[object, ...] = (
            [],
            {"version": 2, "kind": "absent", "target": None},
            {"version": 1, "kind": "unknown", "target": None},
            {"version": 1, "kind": "symlink", "target": None},
            {"version": 1, "kind": "file", "target": "unexpected"},
        )
        self.paths.state.parent.mkdir(parents=True)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.paths.state.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(TabBarInstallError, "invalid tab-bar recovery"):
                    _load_record(self.paths)
        self.paths.state.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(TabBarInstallError, "cannot read"):
            _load_record(self.paths)

        self.paths.state.unlink()
        self.paths.live.mkdir()
        with self.assertRaisesRegex(TabBarInstallError, "non-file"):
            install_tab_bar(self.paths)

    def test_missing_source_state_conflicts_and_filesystem_failures_roll_back(self) -> None:
        missing = TabBarPaths(
            self.paths.live,
            self.root / "missing.py",
            self.paths.state,
            self.paths.backup,
        )
        with self.assertRaisesRegex(TabBarInstallError, "source is missing"):
            install_tab_bar(missing)

        self.paths.state.parent.mkdir(parents=True)
        self.paths.state.write_text(BackupRecord("absent").to_json(), encoding="utf-8")
        with self.assertRaisesRegex(TabBarInstallError, "recovery state already exists"):
            _record_original(self.paths)
        self.paths.state.unlink()

        self.paths.live.write_text("original = True\n", encoding="utf-8")
        with (
            mock.patch(
                "kitty_workbench.tab_bar_install.atomic_write_text",
                side_effect=OSError("disk full"),
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            install_tab_bar(self.paths)
        self.assertFalse(self.paths.backup.exists())
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), "original = True\n")

        self.paths.live.unlink()
        with (
            mock.patch.object(Path, "symlink_to", side_effect=OSError("link failed")),
            self.assertRaisesRegex(OSError, "link failed"),
        ):
            install_tab_bar(self.paths)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.live.exists())

    def test_backup_copy_failures_and_missing_recovery_state_fail_closed(self) -> None:
        self.paths.live.write_text("original = True\n", encoding="utf-8")
        with (
            mock.patch.object(shutil, "copy2", side_effect=OSError("copy failed")),
            self.assertRaisesRegex(TabBarInstallError, "cannot back up"),
        ):
            install_tab_bar(self.paths)
        self.assertEqual(self.paths.live.read_text(encoding="utf-8"), "original = True\n")
        self.assertFalse(self.paths.state.exists())

        self.paths.live.unlink()
        self.paths.live.symlink_to(self.paths.source)
        with self.assertRaisesRegex(TabBarInstallError, "without recovery state"):
            restore_tab_bar(self.paths)

    def test_reenable_completes_an_interrupted_restore_before_installing(self) -> None:
        original = "original = True\n"
        self.paths.live.write_text(original, encoding="utf-8")
        install_tab_bar(self.paths)
        self.paths.live.unlink()

        self.assertTrue(install_tab_bar(self.paths))
        self.assertTrue(_is_managed(self.paths))
        self.assertEqual(self.paths.backup.read_text(encoding="utf-8"), original)

    def test_resolution_and_state_read_errors_fail_closed(self) -> None:
        self.paths.live.symlink_to(self.paths.source)
        with mock.patch.object(Path, "resolve", side_effect=OSError("unreadable")):
            self.assertFalse(_is_managed(self.paths))

        self.paths.state.parent.mkdir(parents=True)
        self.paths.state.write_text(BackupRecord("absent").to_json(), encoding="utf-8")
        with (
            mock.patch.object(Path, "read_text", side_effect=OSError("denied")),
            self.assertRaisesRegex(TabBarInstallError, "cannot read"),
        ):
            _load_record(self.paths)


if __name__ == "__main__":
    unittest.main()
