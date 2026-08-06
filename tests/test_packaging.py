from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kisesh import __version__

PROJECT = Path(__file__).parents[1]


class PackagingTests(unittest.TestCase):
    """Validate installable artifacts rather than only source-tree imports."""

    def test_module_execution_delegates_to_the_same_typed_cli(self) -> None:
        with (
            mock.patch("kisesh.cli.main", return_value=23) as main,
            self.assertRaises(SystemExit) as stopped,
        ):
            runpy.run_module("kisesh", run_name="__main__")

        self.assertEqual(stopped.exception.code, 23)
        main.assert_called_once_with()

    def test_metadata_defines_a_locked_editable_package_and_console_commands(self) -> None:
        document = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
        lockfile = (PROJECT / "uv.lock").read_text(encoding="utf-8")

        self.assertEqual(document["project"]["version"], __version__)
        self.assertEqual(
            document["project"]["scripts"],
            {"kisesh": "kisesh.cli:main"},
        )
        self.assertEqual(document["build-system"]["build-backend"], "uv_build")
        self.assertEqual(document["tool"]["uv"]["build-backend"]["module-root"], "")
        self.assertIn(
            f'name = "kisesh"\nversion = "{__version__}"\nsource = {{ editable = "." }}',
            lockfile,
        )

    @unittest.skipUnless(shutil.which("uv"), "uv is required to build distributions")
    def test_offline_build_contains_runnable_wheel_and_complete_source_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "artifacts"
            environment = os.environ.copy()
            environment["UV_CACHE_DIR"] = str(Path(temporary) / "uv-cache")
            result = subprocess.run(
                [
                    shutil.which("uv") or "uv",
                    "build",
                    "--offline",
                    "--no-sources",
                    "--no-create-gitignore",
                    "--out-dir",
                    str(output),
                ],
                cwd=PROJECT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            wheel = next(output.glob("kisesh-*.whl"))
            source = next(output.glob("kisesh-*.tar.gz"))
            prefix = f"kisesh-{__version__}/"
            with tarfile.open(source, "r:gz") as archive:
                source_names = set(archive.getnames())
            for relative in (
                "bin/kisesh",
                "bin/kisesh-panel",
                "install",
                "integration/kisesh.conf",
                "integration/safe_close.py",
                "integration/tab_bar.py",
                "justfile",
                "kisesh/default_apps.toml",
            ):
                self.assertIn(prefix + relative, source_names)

            with zipfile.ZipFile(wheel) as archive:
                wheel_names = set(archive.namelist())
                entry_points = archive.read(
                    f"kisesh-{__version__}.dist-info/entry_points.txt"
                ).decode("utf-8")
            self.assertIn("kisesh/default_apps.toml", wheel_names)
            self.assertFalse(any(name.startswith("tests/") for name in wheel_names))
            self.assertEqual(
                entry_points,
                "[console_scripts]\nkisesh = kisesh.cli:main\n\n",
            )

            installed = Path(temporary) / "installed"
            installation = subprocess.run(
                [
                    shutil.which("uv") or "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--no-deps",
                    "--target",
                    str(installed),
                    str(wheel),
                ],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(installation.returncode, 0, installation.stderr)

            wheel_environment = os.environ.copy()
            wheel_environment.update(
                {
                    "PATH": "/usr/bin:/bin",
                    "PYTHONPATH": str(installed),
                }
            )
            invoked = subprocess.run(
                [str(installed / "bin" / "kisesh"), "--help"],
                cwd=temporary,
                env=wheel_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertIn("usage: kisesh", invoked.stdout)
            imported = subprocess.run(
                [sys.executable, "-c", "import kisesh; print(kisesh.__file__)"],
                cwd=temporary,
                env=wheel_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                Path(imported.stdout.strip()).resolve(),
                (installed / "kisesh" / "__init__.py").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
