"""Tests for `optimize status` — the pipeline view over artifacts.

The bootstrap critical review §5 found: there was no way to ask "where am I in the pipeline"; the
answer lived in the operator's memory. The command must therefore be honest
about absence (a missing baseline or unmined transform is a state, said out
loud) and loud about the one thing that matters most: applied transforms
with no passing oracle.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.errors import CouldNotCheck  # noqa: E402
from buildsmith.workflows.optimize import gates, status  # noqa: E402


class StatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for mod in (gates, status):
            old = mod.ROOT
            mod.ROOT = self.root
            self.addCleanup(setattr, mod, "ROOT", old)
        self.addCleanup(self._tmp.cleanup)
        (self.root / "sites" / "x").mkdir(parents=True)

    def _write_baseline(self, content_hash: str = "abc123def456") -> None:
        out = self.root / "sites" / "x" / "opt" / "baseline"
        (out / "state").mkdir(parents=True)
        # the checkpoint manifest gates.baseline_hash reads (capture layout)
        (out / "state" / "manifest.json").write_text(
            json.dumps({"content_hash": content_hash}))
        # the baseline's own manifest — different file, different shape
        (out / "manifest.json").write_text(json.dumps({
            "created_utc": "2026-08-06T20:00:00+00:00",
            "builder_ref": "b09a40d98590",
            "routes_captured": ["", "about"],
            "routes_skipped": {"draft": 404},
            "checkpoint": {"content_hash": content_hash},
            "scripts_scanned": 24,
        }))

    def _write_proposals(self, name: str, statuses: list[str],
                         orphaned: int = 0) -> None:
        path = self.root / "sites" / "x" / "opt" / "proposals" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "proposals": [{"status": s} for s in statuses],
            "orphaned": [{}] * orphaned,
        }))

    def test_a_mistyped_site_is_could_not_check_not_not_started(self):
        with self.assertRaises(CouldNotCheck):
            status.gather("no-such-site")

    def test_nothing_started_is_a_state_said_out_loud(self):
        data = status.gather("x")
        self.assertIsNone(data["baseline"])
        self.assertEqual(data["gates"]["applied"], 0)
        self.assertEqual(set(data["proposals"]), {"tokenize", "fonts",
                                                  "componentize"})
        self.assertTrue(all(v is None for v in data["proposals"].values()))
        text = status.render(data)
        self.assertIn("baseline   NONE", text)
        self.assertIn("not mined", text)

    def test_the_baseline_line_reads_from_the_baseline_manifest(self):
        # NOT state/manifest.json — that one is the record checkpoint's and
        # has no created_utc/routes; conflating the two files was a real bug
        # caught while building this.
        self._write_baseline("abc123def456")
        data = status.gather("x")
        self.assertEqual(data["baseline"]["routes_captured"], 2)
        self.assertEqual(data["baseline"]["routes_skipped"], 1)
        text = status.render(data)
        self.assertIn("checkpoint abc123def456"[:23], text)
        self.assertIn("2 route(s) (1 skipped)", text)

    def test_pending_gates_are_the_headline(self):
        self._write_baseline()
        gates.record_apply("x", "fonts")
        data = status.gather("x")
        self.assertEqual([e["transform"] for e in data["gates"]["pending"]],
                         ["fonts"])
        text = status.render(data)
        self.assertIn("!!", text)
        self.assertIn("no passing oracle: fonts", text)
        self.assertIn("optimize oracle", text)

    def test_a_proved_ledger_raises_no_alarm(self):
        self._write_baseline()
        gates.record_apply("x", "tokenize")
        gates.record_oracle("x", True)
        data = status.gather("x")
        self.assertEqual(data["gates"], {**data["gates"], "applied": 1,
                                         "proved": 1, "pending": []})
        self.assertNotIn("!!", status.render(data))

    def test_a_failed_oracle_is_visible_and_still_pending(self):
        self._write_baseline()
        gates.record_apply("x", "collapse")
        gates.record_oracle("x", False, failed=3)
        data = status.gather("x")
        self.assertEqual(data["gates"]["failed"], 1)
        self.assertEqual(len(data["gates"]["pending"]), 1)
        self.assertIn("1 failed-oracle", status.render(data))

    def test_proposal_counts_by_status_and_orphans_are_loud(self):
        self._write_proposals("tokens", ["accepted", "proposed", "proposed"])
        self._write_proposals("components", ["proposed"], orphaned=2)
        data = status.gather("x")
        self.assertEqual(data["proposals"]["tokenize"]["by_status"],
                         {"accepted": 1, "proposed": 2})
        text = status.render(data)
        self.assertIn("1 accepted, 2 proposed", text)
        self.assertIn("2 ORPHANED", text)

    def test_unrecorded_artifacts_are_flagged_not_misread(self):
        # Dogfood finding: tokenize/fonts were applied before the ledger
        # existed, so "0 applied" read as "nothing ever ran". Artifacts with
        # no ledger entry get a note — never an "applied" claim, since
        # collapse writes its dir on dry runs too.
        run_dir = self.root / "sites" / "x" / "opt" / "transforms" / "tokenize"
        run_dir.mkdir(parents=True)
        (run_dir / "page-home.json").write_text("{}")
        data = status.gather("x")
        self.assertTrue(data["artifacts"]["tokenize"])
        text = status.render(data)
        self.assertIn("no gate-ledger entry", text)
        # once the ledger records it, the note goes away
        gates.record_apply("x", "tokenize")
        self.assertNotIn("no gate-ledger entry",
                         status.render(status.gather("x")))

    def test_gather_is_json_able(self):
        self._write_baseline()
        gates.record_apply("x", "tokenize")
        json.dumps(status.gather("x"))  # must not raise

    def test_status_is_read_only(self):
        self._write_baseline()
        self._write_proposals("tokens", ["proposed"])
        before = {p: p.read_bytes()
                  for p in (self.root / "sites").rglob("*") if p.is_file()}
        status.render(status.gather("x"))
        after = {p: p.read_bytes()
                 for p in (self.root / "sites").rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
