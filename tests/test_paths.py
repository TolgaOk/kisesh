from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from kisesh.paths import data_root


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


if __name__ == "__main__":
    unittest.main()
