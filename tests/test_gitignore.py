"""#37: a symlink named `.venv` must be ignored, not just a real directory.

`.venv/` (trailing slash) matches only a directory — a symlink of the same
name reached `main` past that pattern, past the publication guard, and past
gitleaks, because none of them are told to look for it. This pins the fix at
its source: `git check-ignore` itself, so a future edit to `.gitignore` that
narrows the pattern back to directory-only fails loudly here first.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buildsmith.tools.gitenv import run_git

REPO_ROOT = Path(__file__).resolve().parent.parent


class VenvSymlinkIsIgnored(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        run_git("init", "-q", cwd=str(self.repo), check=True)
        (self.repo / ".gitignore").write_text(
            (REPO_ROOT / ".gitignore").read_text())

    def _check_ignore(self, name: str):
        return run_git("-C", str(self.repo), "check-ignore", "-q", name)

    def test_a_venv_symlink_is_ignored(self):
        (self.repo / ".venv").symlink_to("/nonexistent/elsewhere")
        self.assertEqual(self._check_ignore(".venv").returncode, 0)

    def test_a_real_venv_directory_is_still_ignored(self):
        (self.repo / ".venv").mkdir()
        self.assertEqual(self._check_ignore(".venv").returncode, 0)


if __name__ == "__main__":
    unittest.main()
