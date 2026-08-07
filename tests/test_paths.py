from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kisesh.paths import data_root, runtime_root


class PathTests(unittest.TestCase):
    def test_data_root_precedence_is_explicit_then_kisesh_then_xdg_then_home(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KISESH_DATA_DIR": "/configured/kisesh",
                "XDG_DATA_HOME": "/configured/xdg",
            },
            clear=True,
        ):
            self.assertEqual(data_root("~/explicit"), Path("~/explicit").expanduser())
            self.assertEqual(data_root(), Path("/configured/kisesh"))

        with patch.dict("os.environ", {"XDG_DATA_HOME": "/configured/xdg"}, clear=True):
            self.assertEqual(data_root(), Path("/configured/xdg/kisesh"))

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(data_root(), Path("~/.local/share/kisesh").expanduser())

    def test_runtime_root_prefers_explicit_then_stable_install(self) -> None:
        with patch.dict("os.environ", {"KISESH_INSTALL_ROOT": "/configured"}, clear=True):
            self.assertEqual(runtime_root(), Path("/configured"))

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(runtime_root(), Path("~/.local/lib/kisesh").expanduser())

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            packaged_file = Path(temporary) / "site-packages" / "kisesh" / "paths.py"
            with (
                patch.dict("os.environ", {"HOME": str(home)}, clear=True),
                patch("kisesh.paths.__file__", str(packaged_file)),
            ):
                self.assertEqual(runtime_root(), home / ".local" / "lib" / "kisesh")


if __name__ == "__main__":
    unittest.main()
