"""The pre-commit lint step's file selection.

The ruff step only runs when a staged path matches LINTABLE. If that pattern
rots — a rename, a new package root — lint silently stops running on every
commit and nothing notices: the exact erosion the step was added to prevent.
So the pattern is pinned here against representative paths.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buildsmith.tools.precommit import LINTABLE


class LintableSelectionTest(unittest.TestCase):
    def test_our_python_is_lintable(self) -> None:
        for path in (
            "buildsmith/cli.py",
            "buildsmith/tools/prepush.py",
            "buildsmith/workflows/optimize/gates.py",
            "tests/test_precommit.py",
            "install.py",
        ):
            self.assertIsNotNone(LINTABLE.match(path), path)

    def test_everything_else_is_not(self) -> None:
        for path in (
            "docs/catalog.md",
            "pyproject.toml",
            "sites/example/site.yml",
            "buildsmith/tools/prepush.pyc",
            # not our Python: a hypothetical vendored tree
            "vendor/thing.py",
            # prefix must anchor: a lookalike outside the package
            "notbuildsmith/x.py",
        ):
            self.assertIsNone(LINTABLE.match(path), path)

    def test_bench_scripts_are_lintable_but_style_exempt(self) -> None:
        """bench_scripts ARE linted (they are our Python), with the risky
        rules ignored in pyproject per-file-ignores — adopt._run patches one
        by exact line match, so reformatting is the hazard, not linting."""
        self.assertIsNotNone(
            LINTABLE.match("buildsmith/tools/bench_scripts/adopt.py")
        )


if __name__ == "__main__":
    unittest.main()
