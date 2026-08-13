from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NotRequired, TypedDict, cast

PROJECT = Path(__file__).parents[1]
INSTALL_SCRIPT = PROJECT / "install.sh"
README = PROJECT / "README.md"
LATEST_RELEASE = "https://github.com/TolgaOk/kisesh/releases/latest/download"
DEFAULT_PACKAGE_URL = f"{LATEST_RELEASE}/kisesh.tar.gz"


class ObservedCommand(TypedDict):
    """Typed command record written by the isolated installer doubles."""

    program: str
    args: list[str]
    cli: NotRequired[str | None]


class InstallScriptTests(unittest.TestCase):
    """Exercise local, remote, and curl-style installation without touching the host."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.uv = self.bin / "uv"
        fake_cli = (
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "log = pathlib.Path(os.environ['KISESH_INSTALL_LOG'])\n"
            "with log.open('a') as stream:\n"
            "    payload = {\n"
            "        'program': 'kisesh',\n"
            "        'args': sys.argv[1:],\n"
            "        'cli': os.environ.get('KISESH_CLI'),\n"
            "    }\n"
            "    stream.write(json.dumps(payload) + '\\n')\n"
        )
        self.uv.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "log = pathlib.Path(os.environ['KISESH_INSTALL_LOG'])\n"
            "with log.open('a') as stream:\n"
            "    stream.write(json.dumps({'program': 'uv', 'args': sys.argv[1:]}) + '\\n')\n"
            "if sys.argv[1:3] == ['tool', 'install']:\n"
            "    cli = pathlib.Path(os.environ['UV_TOOL_BIN_DIR']) / 'kisesh'\n"
            "    cli.parent.mkdir(parents=True, exist_ok=True)\n"
            f"    cli.write_text({fake_cli!r})\n"
            "    cli.chmod(0o755)\n",
            encoding="utf-8",
        )
        self.uv.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        """Return an isolated GUI-like environment with an explicit remote source."""
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "XDG_DATA_HOME": str(self.home / "data"),
                "KISESH_INSTALL_LOG": str(self.log),
                "KISESH_PACKAGE_URL": "https://example.invalid/kisesh.tar.gz",
                "KISESH_PYTHON": "3.12",
            }
        )
        return environment

    def run_install(
        self,
        environment: dict[str, str],
        *arguments: str,
        through_stdin: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the public file or the exact shell-stdin form used by curl."""
        command = (
            ["/bin/sh", "-s", "--", *arguments]
            if through_stdin
            else [str(INSTALL_SCRIPT), *arguments]
        )
        return subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            input=INSTALL_SCRIPT.read_text(encoding="utf-8") if through_stdin else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def commands(self) -> list[ObservedCommand]:
        """Decode calls observed across the fake uv and installed CLI boundary."""
        return [
            cast(ObservedCommand, json.loads(line))
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def test_remote_source_uses_existing_uv_then_enables_integration(self) -> None:
        environment = self.environment()
        environment["KISESH_UV"] = str(self.uv)

        result = self.run_install(environment)
        commands = self.commands()
        tool_root = self.home / "data" / "kisesh-tool"
        cli = tool_root / "bin" / "kisesh"

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            commands[0],
            {
                "program": "uv",
                "args": [
                    "tool",
                    "install",
                    "--force",
                    "--python",
                    "3.12",
                    "https://example.invalid/kisesh.tar.gz",
                ],
            },
        )
        self.assertEqual(
            commands[1],
            {"program": "kisesh", "args": ["enable"], "cli": str(cli)},
        )
        self.assertIn("Kitty was left running", result.stdout)
        self.assertNotIn("restart", result.stdout.casefold())

    def test_checkout_installs_editably_and_forwards_integration_arguments(self) -> None:
        environment = self.environment()
        environment["KISESH_UV"] = str(self.uv)
        environment.pop("KISESH_PACKAGE_URL")
        kitty_config = self.root / "kitty.conf"

        result = self.run_install(environment, "--kitty-config", str(kitty_config))
        commands = self.commands()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            commands[0],
            {
                "program": "uv",
                "args": [
                    "tool",
                    "install",
                    "--force",
                    "--editable",
                    "--python",
                    "3.12",
                    str(PROJECT),
                ],
            },
        )
        self.assertEqual(
            commands[1]["args"],
            ["enable", "--kitty-config", str(kitty_config)],
        )

    def test_curl_style_stdin_uses_the_default_remote_source(self) -> None:
        environment = self.environment()
        environment["KISESH_UV"] = str(self.uv)
        environment.pop("KISESH_PACKAGE_URL")

        result = self.run_install(environment, through_stdin=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands()[0]["args"][-1], DEFAULT_PACKAGE_URL)
        self.assertNotIn("--editable", self.commands()[0]["args"])

    def test_public_install_sources_follow_release_assets(self) -> None:
        install_script = INSTALL_SCRIPT.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        release_workflow = (PROJECT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(f"default_package_url={DEFAULT_PACKAGE_URL}", install_script)
        self.assertIn(f"{LATEST_RELEASE}/install.sh", readme)
        self.assertIn(DEFAULT_PACKAGE_URL, readme)
        self.assertIn("gh release create", release_workflow)
        self.assertIn("install.sh dist/kisesh.tar.gz", release_workflow)
        self.assertNotIn("refs/heads/main", install_script + readme)
        self.assertNotIn("TolgaOk/kisesh/main/install.sh", readme)

    def test_readme_uv_recipe_installs_then_enables_only_after_success(self) -> None:
        readme = README.read_text(encoding="utf-8")
        opening = "Using `uv`:\n\n```sh\n"
        recipe = readme.split(opening, 1)[1].split("\n```", 1)[0]
        environment = self.environment()
        environment["UV_TOOL_BIN_DIR"] = str(self.bin)

        installed = subprocess.run(
            ["/bin/sh", "-c", recipe],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(
            self.commands(),
            [
                {
                    "program": "uv",
                    "args": ["tool", "install", "--python", "3.11", DEFAULT_PACKAGE_URL],
                },
                {"program": "kisesh", "args": ["enable"], "cli": None},
            ],
        )

        self.log.unlink()
        self.uv.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        failed = subprocess.run(
            ["/bin/sh", "-c", recipe],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(failed.returncode, 9)
        self.assertFalse(self.log.exists())

    def test_missing_uv_uses_a_temporary_pinned_installer(self) -> None:
        curl = self.bin / "curl"
        curl_log = self.root / "curl.json"
        curl.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "arguments = sys.argv[1:]\n"
            "output = pathlib.Path(arguments[arguments.index('--output') + 1])\n"
            "pathlib.Path(os.environ['KISESH_CURL_LOG']).write_text("
            "json.dumps({'args': arguments, 'output': str(output)}))\n"
            "source = pathlib.Path(os.environ['KISESH_FAKE_UV'])\n"
            "output.write_text('#!/bin/sh\\nmkdir -p \"$UV_UNMANAGED_INSTALL\"\\n'"
            ' + f\'cp "{source}" "$UV_UNMANAGED_INSTALL/uv"\\n\''
            " + 'chmod +x \"$UV_UNMANAGED_INSTALL/uv\"\\n')\n"
            "output.chmod(0o755)\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        environment = self.environment()
        environment.update(
            {
                "KISESH_CURL": str(curl),
                "KISESH_CURL_LOG": str(curl_log),
                "KISESH_FAKE_UV": str(self.uv),
                "KISESH_UV_INSTALLER_URL": "https://example.invalid/uv-install.sh",
            }
        )
        environment.pop("KISESH_UV", None)
        self.uv.rename(self.root / "fake-uv-source")
        environment["KISESH_FAKE_UV"] = str(self.root / "fake-uv-source")

        result = self.run_install(environment)
        curl_call = json.loads(curl_log.read_text(encoding="utf-8"))
        temporary = Path(curl_call["output"]).parent

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://example.invalid/uv-install.sh", curl_call["args"])
        self.assertFalse(temporary.exists())
        self.assertEqual(self.commands()[-1]["program"], "kisesh")

    def test_missing_tools_and_incomplete_tool_installs_fail_cleanly(self) -> None:
        invalid_uv = self.environment()
        invalid_uv["KISESH_UV"] = str(self.root / "missing-uv")
        result = self.run_install(invalid_uv)
        self.assertEqual(result.returncode, 1)
        self.assertIn("uv is not executable", result.stderr)

        no_command_uv = self.bin / "no-command-uv"
        no_command_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        no_command_uv.chmod(0o755)
        no_command = self.environment()
        no_command["KISESH_UV"] = str(no_command_uv)
        result = self.run_install(no_command)
        self.assertEqual(result.returncode, 1)
        self.assertIn("installed command is missing", result.stderr)

        missing_curl = self.environment()
        missing_curl["PATH"] = "/usr/bin:/bin"
        missing_curl["KISESH_CURL"] = str(self.root / "missing-curl")
        missing_curl.pop("KISESH_UV", None)
        result = self.run_install(missing_curl)
        self.assertEqual(result.returncode, 1)
        self.assertIn("curl was not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
