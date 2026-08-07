"""The secret scan's exit-code contract (ADR-010).

The whole value of the gitleaks step is the 0/1/2 mapping: a missing scanner
or a crashed scan must exit 2 ("could not check"), never 0 — an unscanned
commit reaching a public repo is exactly the silent no-op the guard family
exists to prevent. These tests pin that mapping without needing gitleaks
installed, by stubbing the binary lookup and the subprocess.
"""

import os
import unittest
from unittest import mock

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM, EXIT_UNCHECKED
from buildsmith.tools import secretscan


def _completed(returncode: int) -> mock.Mock:
    return mock.Mock(returncode=returncode)


class ExitCodeContractTest(unittest.TestCase):
    def test_missing_binary_is_unchecked_not_ok(self) -> None:
        with mock.patch.object(secretscan, "gitleaks_path", return_value=None):
            self.assertEqual(secretscan.scan_staged(), EXIT_UNCHECKED)
            self.assertEqual(secretscan.scan_history(), EXIT_UNCHECKED)

    def test_clean_scan_is_ok(self) -> None:
        with mock.patch.object(secretscan, "gitleaks_path", return_value="/bin/gl"), \
             mock.patch.object(secretscan.subprocess, "run",
                               return_value=_completed(0)):
            self.assertEqual(secretscan.scan_staged(), EXIT_OK)

    def test_findings_are_a_problem(self) -> None:
        with mock.patch.object(secretscan, "gitleaks_path", return_value="/bin/gl"), \
             mock.patch.object(secretscan.subprocess, "run",
                               return_value=_completed(1)):
            self.assertEqual(secretscan.scan_staged(), EXIT_PROBLEM)

    def test_scanner_crash_is_unchecked_not_a_finding(self) -> None:
        """Exit 126, a bad config, not-a-repo: the scan never ran. Reporting
        that as 1 sends someone hunting a leak that was never detected."""
        with mock.patch.object(secretscan, "gitleaks_path", return_value="/bin/gl"), \
             mock.patch.object(secretscan.subprocess, "run",
                               return_value=_completed(126)):
            self.assertEqual(secretscan.scan_staged(), EXIT_UNCHECKED)

    def test_scan_is_redacted_and_uses_repo_config(self) -> None:
        """Findings may print into shared terminals and public CI logs, so
        --redact is not optional; and the config must be the committed one,
        where allowlist reviews are visible."""
        with mock.patch.object(secretscan, "gitleaks_path", return_value="/bin/gl"), \
             mock.patch.object(secretscan.subprocess, "run",
                               return_value=_completed(0)) as run:
            secretscan.scan_history()
        argv = run.call_args.args[0]
        self.assertIn("--redact", argv)
        self.assertIn(".gitleaks.toml", argv)


class SkipHatchTest(unittest.TestCase):
    def test_skip_is_explicit_and_says_so(self) -> None:
        with mock.patch.dict(os.environ, {"BUILDSMITH_SKIP_GITLEAKS": "1"}):
            self.assertEqual(secretscan.main([]), EXIT_OK)

    def test_default_runs_staged_history_flag_runs_history(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k != "BUILDSMITH_SKIP_GITLEAKS"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(secretscan, "scan_staged",
                               return_value=EXIT_OK) as staged, \
             mock.patch.object(secretscan, "scan_history",
                               return_value=EXIT_OK) as history:
            secretscan.main([])
            staged.assert_called_once()
            history.assert_not_called()
            secretscan.main(["--history"])
            history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
