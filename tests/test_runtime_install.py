from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kisesh.runtime_install import (
    RUNTIME_MANIFEST,
    RuntimeInstallError,
    RuntimePaths,
    _desired_manifest,
    _in_place_source,
    _stage_runtime,
    check_runtime_target,
    deploy_runtime,
    ensure_command_link,
    finish_runtime,
    remove_command_link,
    remove_runtime,
    rollback_runtime,
    runtime_paths,
    validate_runtime_source,
)

PROJECT = Path(__file__).parents[1]
PROJECT_LAUNCHER = PROJECT / ".venv" / "bin" / "kisesh"


class RuntimeInstallTests(unittest.TestCase):
    """Exercise fresh, upgraded, rolled-back, foreign, and removed runtimes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "lib" / "kisesh"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def paths(
        self,
        *,
        source: Path = PROJECT,
        launcher: Path = PROJECT_LAUNCHER,
        target: Path | None = None,
    ) -> RuntimePaths:
        """Build a runtime contract around real packaged resources and disposable targets."""
        return runtime_paths(source, launcher, target or self.target)

    def launcher(self, name: str) -> Path:
        """Create an executable replacement launcher for upgrade scenarios."""
        launcher = self.root / name
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        return launcher

    def assert_deployed(self, paths: RuntimePaths) -> None:
        """Verify every stable runtime path resolves to its packaged source."""
        self.assertTrue(paths.target.is_dir())
        self.assertFalse(paths.target.is_symlink())
        self.assertEqual((paths.target / "kisesh").resolve(), paths.package.resolve())
        self.assertEqual((paths.target / "integration").resolve(), paths.integration.resolve())
        self.assertEqual((paths.target / "bin" / "kisesh").resolve(), paths.launcher.resolve())
        self.assertEqual(
            (paths.target / "bin" / "kisesh-panel").resolve(),
            paths.panel.resolve(),
        )
        self.assertIsNotNone(check_runtime_target(paths))

    def test_fresh_deployment_is_idempotent_and_removable(self) -> None:
        paths = self.paths()

        created = deploy_runtime(paths)
        self.assertTrue(created.changed)
        self.assertIsNone(created.backup)
        self.assertIsNone(created.previous_symlink)
        self.assert_deployed(paths)
        finish_runtime(created)

        unchanged = deploy_runtime(paths)
        self.assertFalse(unchanged.changed)
        rollback_runtime(unchanged)
        finish_runtime(unchanged)
        self.assert_deployed(paths)

        self.assertTrue(remove_runtime(paths))
        self.assertFalse(paths.target.exists())
        self.assertFalse(remove_runtime(paths))

    def test_previous_source_link_can_roll_back_or_finish_migration(self) -> None:
        paths = self.paths()
        paths.target.parent.mkdir(parents=True)
        paths.target.symlink_to(PROJECT, target_is_directory=True)

        migrated = deploy_runtime(paths)
        self.assertTrue(migrated.changed)
        self.assertEqual(Path(migrated.previous_symlink or "").resolve(), PROJECT.resolve())
        self.assert_deployed(paths)

        rollback_runtime(migrated)
        self.assertTrue(paths.target.is_symlink())
        self.assertEqual(paths.target.resolve(), PROJECT.resolve())

        migrated = deploy_runtime(paths)
        finish_runtime(migrated)
        self.assert_deployed(paths)

    def test_managed_upgrade_retains_exact_runtime_for_rollback_then_commits(self) -> None:
        initial = self.paths()
        finish_runtime(deploy_runtime(initial))
        initial_manifest = check_runtime_target(initial)
        self.assertIsNotNone(initial_manifest)
        replacement = self.paths(launcher=self.launcher("new-kisesh"))

        upgraded = deploy_runtime(replacement)
        self.assertTrue(upgraded.changed)
        self.assertIsNotNone(upgraded.backup)
        assert upgraded.backup is not None
        self.assertTrue(upgraded.backup.is_dir())
        self.assert_deployed(replacement)

        rollback_runtime(upgraded)
        restored = check_runtime_target(initial)
        self.assertEqual(restored, initial_manifest)

        committed = deploy_runtime(replacement)
        backup = committed.backup
        finish_runtime(committed)
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertFalse(backup.exists())
        self.assert_deployed(replacement)

    def test_in_place_source_is_preserved_on_install_and_removal(self) -> None:
        paths = self.paths(target=PROJECT)

        self.assertIsNone(check_runtime_target(paths))
        transaction = deploy_runtime(paths)
        self.assertFalse(transaction.changed)
        self.assertFalse(remove_runtime(paths))
        self.assertTrue((PROJECT / "kisesh" / "__init__.py").is_file())

    def test_source_validation_rejects_missing_or_non_executable_resources(self) -> None:
        missing = self.paths(source=self.root / "missing")
        with self.assertRaisesRegex(RuntimeInstallError, "package is incomplete"):
            validate_runtime_source(missing)

        launcher = self.launcher("not-executable")
        launcher.chmod(0o600)
        with self.assertRaisesRegex(RuntimeInstallError, "not executable"):
            validate_runtime_source(self.paths(launcher=launcher))

    def test_foreign_targets_and_mutated_managed_trees_fail_closed(self) -> None:
        cases = ("file", "foreign-link", "broken-link", "directory", "invalid-manifest")
        for name in cases:
            target = self.root / name / "kisesh"
            target.parent.mkdir(parents=True)
            paths = self.paths(target=target)
            if name == "file":
                target.write_text("foreign", encoding="utf-8")
            elif name == "foreign-link":
                foreign = self.root / "foreign"
                foreign.mkdir(exist_ok=True)
                target.symlink_to(foreign, target_is_directory=True)
            elif name == "broken-link":
                target.symlink_to(self.root / "missing", target_is_directory=True)
            else:
                target.mkdir()
                if name == "invalid-manifest":
                    (target / RUNTIME_MANIFEST).write_text("{}", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(RuntimeInstallError):
                check_runtime_target(paths)

        paths = self.paths(target=self.root / "mutated" / "kisesh")
        finish_runtime(deploy_runtime(paths))
        (paths.target / "unexpected").write_text("user", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeInstallError, "unrecognized files"):
            remove_runtime(paths)
        (paths.target / "unexpected").unlink()
        (paths.target / "bin" / "kisesh").unlink()
        (paths.target / "bin" / "kisesh").symlink_to(self.launcher("foreign-command"))
        with self.assertRaisesRegex(RuntimeInstallError, "links were modified"):
            check_runtime_target(paths)

    def test_invalid_runtime_shapes_and_manifests_are_never_claimed(self) -> None:
        paths = self.paths()
        finish_runtime(deploy_runtime(paths))
        manifest_path = paths.target / RUNTIME_MANIFEST
        valid_payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        invalid_payloads: tuple[object, ...] = (
            [],
            {**valid_payload, "schema": 99},
            {**valid_payload, "product": "foreign"},
            {**valid_payload, "launcher": ""},
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeInstallError, "invalid runtime manifest"):
                    check_runtime_target(paths)
        manifest_path.write_text(json.dumps(valid_payload), encoding="utf-8")

        binary = paths.target / "bin"
        for command in binary.iterdir():
            command.unlink()
        binary.rmdir()
        binary.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeInstallError, "binary directory"):
            check_runtime_target(paths)

    def test_every_managed_runtime_shape_is_verified_before_replacement(self) -> None:
        mutations: tuple[tuple[str, str], ...] = (
            ("extra-launcher", "unrecognized launchers"),
            ("binary-link", "binary directory"),
            ("regular-resource", "links were modified"),
            ("broken-resource", "links were modified"),
        )
        for name, message in mutations:
            with self.subTest(name=name):
                paths = self.paths(target=self.root / name / "kisesh")
                finish_runtime(deploy_runtime(paths))
                if name == "extra-launcher":
                    (paths.target / "bin" / "unexpected").write_text("foreign")
                elif name == "binary-link":
                    binary = paths.target / "bin"
                    stored = paths.target.parent / "stored-bin"
                    binary.rename(stored)
                    binary.symlink_to(stored, target_is_directory=True)
                elif name == "regular-resource":
                    package = paths.target / "kisesh"
                    package.unlink()
                    package.write_text("foreign", encoding="utf-8")
                else:
                    panel = paths.target / "bin" / "kisesh-panel"
                    panel.unlink()
                    panel.symlink_to(self.root / "missing-panel")
                with self.assertRaisesRegex(RuntimeInstallError, message):
                    check_runtime_target(paths)

    def test_path_resolution_and_staging_failures_leave_no_partial_runtime(self) -> None:
        paths = self.paths()
        with mock.patch.object(Path, "resolve", side_effect=OSError("unreadable")):
            self.assertFalse(_in_place_source(paths))

        paths.target.parent.mkdir(parents=True)
        before = set(paths.target.parent.iterdir())
        manifest = _desired_manifest(paths)
        with (
            mock.patch(
                "kisesh.runtime_install.atomic_write_text",
                side_effect=OSError("manifest failed"),
            ),
            self.assertRaisesRegex(OSError, "manifest failed"),
        ):
            _stage_runtime(paths, manifest)
        self.assertEqual(set(paths.target.parent.iterdir()), before)

    def test_rollback_rejects_a_runtime_replaced_after_the_transaction(self) -> None:
        paths = self.paths()
        transaction = deploy_runtime(paths)
        manifest_path = paths.target / RUNTIME_MANIFEST
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["deployment"] = "another-process"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeInstallError, "refusing to roll back"):
            rollback_runtime(transaction)

    def test_command_link_lifecycle_preserves_package_manager_and_foreign_commands(self) -> None:
        launcher = self.launcher("command")
        link = self.root / "bin" / "kisesh"

        self.assertTrue(ensure_command_link(link, launcher))
        self.assertFalse(ensure_command_link(link, launcher))
        self.assertTrue(remove_command_link(link, launcher))
        self.assertFalse(remove_command_link(link, launcher))

        managed_directly = self.launcher("managed-directly")
        self.assertFalse(ensure_command_link(managed_directly, managed_directly))
        self.assertFalse(remove_command_link(managed_directly, managed_directly))

        package_managed = self.root / "package-bin" / "kisesh"
        package_managed.parent.mkdir()
        package_managed.symlink_to(managed_directly)
        self.assertFalse(remove_command_link(package_managed, package_managed))
        self.assertTrue(package_managed.is_symlink())

        foreign = self.launcher("foreign")
        link.symlink_to(foreign)
        with self.assertRaisesRegex(RuntimeInstallError, "existing command"):
            ensure_command_link(link, launcher)
        with self.assertRaisesRegex(RuntimeInstallError, "changed command"):
            remove_command_link(link, launcher)

        link.unlink()
        link.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(RuntimeInstallError, "existing command"):
            ensure_command_link(link, launcher)
        with self.assertRaisesRegex(RuntimeInstallError, "changed command"):
            remove_command_link(link, launcher)

    def test_deployment_restores_previous_state_when_final_replace_fails(self) -> None:
        paths = self.paths()
        paths.target.parent.mkdir(parents=True)
        paths.target.symlink_to(PROJECT, target_is_directory=True)
        real_replace = os.replace

        def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(destination) == paths.target:
                raise OSError("replace failed")
            real_replace(source, destination)

        with (
            mock.patch("kisesh.runtime_install.os.replace", side_effect=replace),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            deploy_runtime(paths)

        self.assertTrue(paths.target.is_symlink())
        self.assertEqual(paths.target.resolve(), PROJECT.resolve())

    def test_failed_managed_upgrade_restores_the_verified_previous_runtime(self) -> None:
        paths = self.paths()
        finish_runtime(deploy_runtime(paths))
        previous = check_runtime_target(paths)
        replacement = self.paths(launcher=self.launcher("replacement"))
        real_replace = os.replace

        def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            source_name = Path(source).name
            if (
                Path(destination) == paths.target
                and source_name.startswith(".kisesh-runtime.")
                and ".previous." not in source_name
            ):
                raise OSError("replace failed")
            real_replace(source, destination)

        with (
            mock.patch("kisesh.runtime_install.os.replace", side_effect=replace),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            deploy_runtime(replacement)

        self.assertEqual(check_runtime_target(paths), previous)

    def test_concurrent_target_creation_is_preserved_when_migration_fails(self) -> None:
        paths = self.paths()
        paths.target.parent.mkdir(parents=True)
        paths.target.symlink_to(PROJECT, target_is_directory=True)
        real_replace = os.replace

        def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(destination) == paths.target:
                paths.target.mkdir()
                raise OSError("replace failed")
            real_replace(source, destination)

        with (
            mock.patch("kisesh.runtime_install.os.replace", side_effect=replace),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            deploy_runtime(paths)

        self.assertTrue(paths.target.is_dir())
        self.assertFalse(paths.target.is_symlink())

    def test_previous_source_link_can_be_removed_without_claiming_its_checkout(self) -> None:
        paths = self.paths()
        paths.target.parent.mkdir(parents=True)
        paths.target.symlink_to(PROJECT, target_is_directory=True)

        self.assertTrue(remove_runtime(paths))
        self.assertFalse(paths.target.exists())
        self.assertTrue((PROJECT / "kisesh" / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
