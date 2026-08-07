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
            {
                "kisesh": "kisesh.cli:main",
                "kisesh-panel": "kisesh.panel_launcher:main",
            },
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
                "install.sh",
                "justfile",
                "kisesh/default_apps.toml",
                "kisesh/integration/actions.py",
                "kisesh/integration/kisesh.conf",
                "kisesh/integration/tab_bar.py",
            ):
                self.assertIn(prefix + relative, source_names)

            with zipfile.ZipFile(wheel) as archive:
                wheel_names = set(archive.namelist())
                metadata = archive.read(f"kisesh-{__version__}.dist-info/METADATA").decode("utf-8")
                entry_points = archive.read(
                    f"kisesh-{__version__}.dist-info/entry_points.txt"
                ).decode("utf-8")
            self.assertIn("kisesh/default_apps.toml", wheel_names)
            for resource in (
                "kisesh/integration/actions.py",
                "kisesh/integration/kisesh.conf",
                "kisesh/integration/kitty_api.py",
                "kisesh/integration/quick-access-terminal.conf",
                "kisesh/integration/tab_bar.py",
            ):
                self.assertIn(resource, wheel_names)
            self.assertFalse(any(name.startswith("tests/") for name in wheel_names))
            self.assertIn("Requires-Dist: tyro>=1.0.15,<2", metadata)
            self.assertEqual(
                entry_points,
                "[console_scripts]\nkisesh = kisesh.cli:main\n"
                "kisesh-panel = kisesh.panel_launcher:main\n\n",
            )

            installed = Path(temporary) / "installed"
            installation = subprocess.run(
                [
                    shutil.which("uv") or "uv",
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
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
            self.assertIn("install", invoked.stdout)
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

            home = Path(temporary) / "home"
            home.mkdir()
            kitty = Path(temporary) / "kitty"
            kitty.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                "text = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')\n"
                "allow = 'no'\n"
                "listen = 'none'\n"
                "for raw in text.splitlines():\n"
                "    fields = raw.strip().split(maxsplit=1)\n"
                "    if len(fields) == 2 and fields[0] == 'allow_remote_control':\n"
                "        allow = fields[1]\n"
                "    if len(fields) == 2 and fields[0] == 'listen_on':\n"
                "        listen = fields[1]\n"
                "print(json.dumps({'bad': [], 'allow': allow, 'listen': listen}))\n",
                encoding="utf-8",
            )
            kitty.chmod(0o755)
            wheel_environment.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"),
                    "KISESH_KITTY": str(kitty),
                }
            )
            enabled = subprocess.run(
                [str(installed / "bin" / "kisesh"), "install"],
                cwd=temporary,
                env=wheel_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            runtime = home / ".local" / "lib" / "kisesh"
            command_link = home / ".local" / "bin" / "kisesh"
            kitty_config = home / "config" / "kitty" / "kitty.conf"
            self.assertTrue(runtime.is_dir())
            self.assertEqual((runtime / "kisesh").resolve(), (installed / "kisesh").resolve())
            self.assertEqual(
                (runtime / "integration").resolve(),
                (installed / "kisesh" / "integration").resolve(),
            )
            self.assertEqual(
                (runtime / "bin" / "kisesh-panel").resolve(),
                (installed / "bin" / "kisesh-panel").resolve(),
            )
            self.assertEqual(command_link.resolve(), (installed / "bin" / "kisesh").resolve())
            self.assertIn("# BEGIN kisesh", kitty_config.read_text(encoding="utf-8"))

            uninstalled = subprocess.run(
                [str(installed / "bin" / "kisesh"), "uninstall"],
                cwd=temporary,
                env=wheel_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertFalse(runtime.exists())
            self.assertFalse(command_link.exists())
            self.assertNotIn("# BEGIN kisesh", kitty_config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
