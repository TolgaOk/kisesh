from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from kitty_workbench.paths import data_root


class PathTests(unittest.TestCase):
    def test_data_root_precedence_is_explicit_then_workbench_then_xdg_then_home(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KITTY_WORKBENCH_DATA_DIR": "/configured/workbench",
                "XDG_DATA_HOME": "/configured/xdg",
            },
            clear=True,
        ):
            self.assertEqual(data_root("~/explicit"), Path("~/explicit").expanduser())
            self.assertEqual(data_root(), Path("/configured/workbench"))

        with patch.dict("os.environ", {"XDG_DATA_HOME": "/configured/xdg"}, clear=True):
            self.assertEqual(data_root(), Path("/configured/xdg/kitty-workbench"))

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(data_root(), Path("~/.local/share/kitty-workbench").expanduser())


if __name__ == "__main__":
    unittest.main()
