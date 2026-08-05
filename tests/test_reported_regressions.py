from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from typing import cast


class ReportedRegressionLedgerTests(unittest.TestCase):
    def test_every_reported_bug_points_to_one_discovered_behavior_test(self) -> None:
        path = Path(__file__).with_name("reported_bugs.toml")
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        bugs = cast(dict[str, str], payload["bugs"])

        self.assertGreaterEqual(len(bugs), 35)
        for bug, test_name in bugs.items():
            with self.subTest(bug=bug):
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromName(test_name)
                self.assertEqual(loader.errors, [])
                self.assertEqual(suite.countTestCases(), 1)


if __name__ == "__main__":
    unittest.main()
