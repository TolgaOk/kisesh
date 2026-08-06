from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
BOOTSTRAP = PROJECT / "bootstrap.sh"


class BootstrapTests(unittest.TestCase):
    """Run both public curl-bootstrap paths with isolated fake tool installations."""

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
            "log = pathlib.Path(os.environ['KISESH_BOOTSTRAP_LOG'])\n"
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
            "log = pathlib.Path(os.environ['KISESH_BOOTSTRAP_LOG'])\n"
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
        """Return a deterministic GUI-like environment for the shell bootstrap."""
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "XDG_DATA_HOME": str(self.home / "data"),
                "KISESH_BOOTSTRAP_LOG": str(self.log),
                "KISESH_PACKAGE_URL": "https://example.invalid/kisesh.tar.gz",
                "KISESH_PYTHON": "3.12",
            }
        )
        return environment

    def run_bootstrap(
        self,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        """Execute the exact public script and retain both user-facing streams."""
        return subprocess.run(
            [str(BOOTSTRAP)],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def commands(self) -> list[dict[str, object]]:
        """Decode commands observed across the fake uv and installed CLI boundary."""
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_existing_uv_installs_an_isolated_tool_then_runs_integration(self) -> None:
        environment = self.environment()
        environment["KISESH_UV"] = str(self.uv)

        result = self.run_bootstrap(environment)
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
            {"program": "kisesh", "args": ["install"], "cli": str(cli)},
        )
        self.assertIn("Restart Kitty once", result.stdout)

    def test_curl_path_uses_a_temporary_pinned_uv_without_persisting_it(self) -> None:
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

        result = self.run_bootstrap(environment)
        curl_call = json.loads(curl_log.read_text(encoding="utf-8"))
        temporary = Path(curl_call["output"]).parent

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://example.invalid/uv-install.sh", curl_call["args"])
        self.assertFalse(temporary.exists())
        self.assertEqual(self.commands()[-1]["program"], "kisesh")

    def test_bootstrap_reports_missing_tools_and_incomplete_tool_installs(self) -> None:
        invalid_uv = self.environment()
        invalid_uv["KISESH_UV"] = str(self.root / "missing-uv")
        result = self.run_bootstrap(invalid_uv)
        self.assertEqual(result.returncode, 1)
        self.assertIn("uv is not executable", result.stderr)

        no_command_uv = self.bin / "no-command-uv"
        no_command_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        no_command_uv.chmod(0o755)
        no_command = self.environment()
        no_command["KISESH_UV"] = str(no_command_uv)
        result = self.run_bootstrap(no_command)
        self.assertEqual(result.returncode, 1)
        self.assertIn("installed command is missing", result.stderr)

        missing_curl = self.environment()
        missing_curl["PATH"] = "/usr/bin:/bin"
        missing_curl["KISESH_CURL"] = str(self.root / "missing-curl")
        missing_curl.pop("KISESH_UV", None)
        result = self.run_bootstrap(missing_curl)
        self.assertEqual(result.returncode, 1)
        self.assertIn("curl was not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
