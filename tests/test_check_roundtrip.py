"""#20: check_roundtrip must not take unrelated optimize state with it.

Its "clean slate" step used to delete every Builder Variable and Component
on `sandbox.localhost` — the same shared bench every optimize transform
targets by default — so a `tokenize --apply` proved minutes earlier could
vanish while the gate ledger still called it applied. These tests pin the
two-part fix: the wipe is scoped off `sandbox.localhost` for anything but
its own named fixtures, and the check refuses outright while any site's
ledger holds an applied-but-unproved transform.
"""

from __future__ import annotations

import unittest
from unittest import mock

from buildsmith.tools import check_roundtrip


class CleanSlateScriptTest(unittest.TestCase):
    """The generated script, not the subprocess plumbing around it."""

    def test_sandbox_localhost_only_deletes_its_own_named_fixtures(self):
        script = check_roundtrip._clean_slate_script()
        sandbox_block, _, rest = script.partition("roundtrip.localhost")
        self.assertIn("sandbox.localhost", sandbox_block)
        self.assertIn("Builder Page", sandbox_block)
        self.assertIn("roundtrip-proof", sandbox_block,
                      "sandbox.localhost's Page delete must stay scoped to the "
                      "check's own route")
        self.assertNotIn("Builder Variable", sandbox_block,
                          "a blanket Variable wipe on sandbox.localhost is "
                          "exactly what destroyed unrelated optimize state (#20)")
        self.assertNotIn("Builder Component", sandbox_block,
                          "a blanket Component wipe on sandbox.localhost is "
                          "exactly what destroyed unrelated optimize state (#20)")

    def test_the_scratch_site_still_gets_the_blanket_wipe(self):
        script = check_roundtrip._clean_slate_script()
        self.assertIn("Builder Variable", script)
        self.assertIn("Builder Component", script)

    def test_the_scratch_site_name_is_not_hardcoded_in_the_builder(self):
        script = check_roundtrip._clean_slate_script("other.localhost")
        self.assertIn("other.localhost", script)
        self.assertNotIn("roundtrip.localhost", script)


class RefusesWhilePendingTest(unittest.TestCase):
    """The defense-in-depth guard: refuse before touching the sandbox at all."""

    def test_a_pending_ledger_refuses_before_any_subprocess_runs(self):
        pending = {"example": [{"transform": "tokenize"}]}
        with mock.patch(
            "buildsmith.workflows.optimize.gates.any_pending", return_value=pending
        ), mock.patch.object(check_roundtrip.subprocess, "run") as run:
            with self.assertRaises(SystemExit) as caught:
                check_roundtrip.main([])
            run.assert_not_called()
        message = str(caught.exception)
        self.assertIn("example", message)
        self.assertIn("tokenize", message)
        self.assertIn("oracle", message)

    def test_a_clean_ledger_proceeds_to_the_sandbox_check(self):
        with mock.patch(
            "buildsmith.workflows.optimize.gates.any_pending", return_value={}
        ), mock.patch.object(
            check_roundtrip.subprocess, "run",
            return_value=mock.Mock(stdout=""),
        ) as run:
            with self.assertRaises(SystemExit) as caught:
                check_roundtrip.main([])
            run.assert_called_once()
        self.assertIn("sandbox is not running", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
