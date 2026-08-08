"""Tests for the gate ledger — the record that a transform was PROVED.

The failure this closes (bootstrap critical review §2.2, private notes): apply a transform, skip the
oracle, re-baseline — and the unproven visual change is absorbed into the
reference forever, with no machine-checkable trace that the proof never ran.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.workflows.optimize import baseline, gates  # noqa: E402


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = gates.ROOT
        gates.ROOT = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, gates, "ROOT", self._old_root)

    def _write_baseline_manifest(self, content_hash: str = "abc123") -> None:
        state = self.root / "sites" / "x" / "opt" / "baseline" / "state"
        state.mkdir(parents=True)
        (state / "manifest.json").write_text(
            json.dumps({"content_hash": content_hash}))

    def test_an_apply_is_pending_until_an_oracle_passes(self):
        self._write_baseline_manifest()
        gates.record_apply("x", "tokenize")
        self.assertEqual([e["transform"] for e in gates.pending("x")],
                         ["tokenize"])

    def test_the_entry_records_which_baseline_it_was_applied_against(self):
        self._write_baseline_manifest("deadbeef")
        entry = gates.record_apply("x", "tokenize")
        self.assertEqual(entry["baseline_hash"], "deadbeef")

    def test_a_passing_oracle_clears_the_pending_entry(self):
        gates.record_apply("x", "tokenize")
        self.assertEqual(gates.record_oracle("x", True), 1)
        self.assertEqual(gates.pending("x"), [])

    def test_a_failing_oracle_is_recorded_but_still_pending(self):
        # The change is applied and provably NOT equivalent — that must not
        # unlock re-baselining, but the attempt must be visible.
        gates.record_apply("x", "collapse")
        gates.record_oracle("x", False, failed=3)
        open_entries = gates.pending("x")
        self.assertEqual(len(open_entries), 1)
        self.assertEqual(open_entries[0]["oracle"],
                         {"ok": False, "failed": 3,
                          "at": open_entries[0]["oracle"]["at"]})

    def test_an_oracle_with_nothing_pending_records_nothing(self):
        self.assertEqual(gates.record_oracle("x", True), 0)

    def test_assert_no_pending_refuses_and_names_the_transform(self):
        gates.record_apply("x", "fonts")
        with self.assertRaises(SystemExit) as caught:
            gates.assert_no_pending("x")
        self.assertIn("fonts", str(caught.exception))
        self.assertIn("--force", str(caught.exception))

    def test_force_waives_and_the_waiver_is_written_down(self):
        gates.record_apply("x", "fonts")
        waived = gates.assert_no_pending("x", force=True)
        self.assertEqual([e["transform"] for e in waived], ["fonts"])
        self.assertEqual(gates.pending("x"), [])
        # The ledger keeps the entry, marked — it never lies by omission.
        stored = json.loads((self.root / "sites" / "x" / "opt"
                             / "gates.json").read_text())
        self.assertTrue(stored["entries"][0]["waived"])
        self.assertIn("waived_at", stored["entries"][0])

    def test_a_clean_ledger_does_not_block(self):
        self.assertEqual(gates.assert_no_pending("x"), [])


class LoosenedOracleDoesNotSettleTheGate(unittest.TestCase):
    """`optimize oracle --threshold 1` passes everything; it must not clear
    the ledger — the recorded-waiver path for skipping the proof is
    `optimize baseline --force`, nothing else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = gates.ROOT
        gates.ROOT = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, gates, "ROOT", self._old_root)

    def _run_oracle_cli(self, *extra: str) -> int:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        from buildsmith import cli
        from buildsmith.tools import journal
        from buildsmith.workflows.optimize import oracle

        with mock.patch.object(oracle, "run_oracle",
                               return_value={"ok": True, "failed": 0}), \
             mock.patch.object(oracle, "render_report", return_value=""), \
             mock.patch.object(journal, "append"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return cli.main(["optimize", "oracle", "--site", "x", *extra])

    def test_a_loosened_threshold_leaves_the_entry_pending(self):
        gates.record_apply("x", "tokenize")
        self._run_oracle_cli("--threshold", "1")
        self.assertEqual(len(gates.pending("x")), 1)

    def test_a_redirected_clone_leaves_the_entry_pending(self):
        gates.record_apply("x", "tokenize")
        self._run_oracle_cli("--clone", "http://127.0.0.1:9999")
        self.assertEqual(len(gates.pending("x")), 1)

    def test_a_default_run_settles_it(self):
        gates.record_apply("x", "tokenize")
        self._run_oracle_cli()
        self.assertEqual(gates.pending("x"), [])


class BaselineRefusesUnprovenState(unittest.TestCase):
    """build_baseline() itself carries the gate, so no caller can skip it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = gates.ROOT
        gates.ROOT = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, gates, "ROOT", self._old_root)

    def test_rebaselining_over_an_unproven_apply_is_refused(self):
        gates.record_apply("x", "tokenize")
        with self.assertRaises(SystemExit) as caught:
            baseline.build_baseline("x", out=self.root / "out")
        message = str(caught.exception)
        self.assertIn("tokenize", message)
        self.assertIn("oracle", message)


class AnyPendingTest(unittest.TestCase):
    """#20: any tool about to mutate the shared sandbox needs one place to
    ask "is anything applied-but-unproved right now", across every site."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = gates.ROOT
        gates.ROOT = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, gates, "ROOT", self._old_root)

    def test_nothing_pending_when_no_sites_exist(self):
        self.assertEqual(gates.any_pending(), {})

    def test_an_unproved_apply_on_one_site_is_found(self):
        gates.record_apply("x", "tokenize")
        found = gates.any_pending()
        self.assertEqual(list(found), ["x"])
        self.assertEqual([e["transform"] for e in found["x"]], ["tokenize"])

    def test_a_settled_site_does_not_appear(self):
        gates.record_apply("x", "tokenize")
        gates.record_oracle("x", ok=True)
        self.assertEqual(gates.any_pending(), {})

    def test_a_waived_site_does_not_appear(self):
        gates.record_apply("x", "tokenize")
        gates.assert_no_pending("x", force=True)
        self.assertEqual(gates.any_pending(), {})

    def test_pending_on_one_site_does_not_hide_a_settled_one(self):
        gates.record_apply("settled", "fonts")
        gates.record_oracle("settled", ok=True)
        gates.record_apply("dirty", "collapse")
        self.assertEqual(list(gates.any_pending()), ["dirty"])


if __name__ == "__main__":
    unittest.main()
