"""Tests for the run journal.

The journal exists so a site is explicable a year later. These check the ways
that fails quietly: a record that omits which Builder it was built against, a
corrupt line that vanishes from the log, and a render that reads like a clean
history when it is actually stitched across incompatible pins.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.tools import journal as journal


def moment(day: str, hour: int = 12):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").astimezone(UTC)


class Appending(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_record_lands_in_the_private_layer(self):
        journal.append("example", "theme", root=self.root, now=moment("2026-08-04"))
        path = self.root / "sites" / "example" / "journal" / "2026-08-04.jsonl"
        self.assertTrue(path.exists())

    def test_records_append_rather_than_overwrite(self):
        for tool in ("tokens", "components", "template"):
            journal.append("example", tool, root=self.root, now=moment("2026-08-04"))
        entries = journal.read_entries("example", root=self.root)
        self.assertEqual(len(entries), 3)
        # Same timestamp, so the sort is stable and insertion order survives —
        # which is what makes a same-minute sequence of runs readable.
        self.assertEqual([e.tool for e in entries], ["tokens", "components", "template"])

    def test_each_record_carries_the_builder_pin(self):
        # Without it an old record is uninterpretable: the same payload can be
        # right for one Builder commit and wrong for another.
        (self.root / "sandbox").mkdir(parents=True)
        (self.root / "sandbox" / "pins.env").write_text(
            "BUILDER_REF=15cb01e4\nBUILDER_REF_STATUS=confirmed  # trailing comment\n"
        )
        entry = journal.append("example", "theme", root=self.root, now=moment("2026-08-04"))
        self.assertEqual(entry.builder["BUILDER_REF"], "15cb01e4")
        self.assertEqual(entry.builder["BUILDER_REF_STATUS"], "confirmed")

    def test_counts_and_warnings_round_trip(self):
        journal.append(
            "example", "theme", root=self.root, now=moment("2026-08-04"),
            counts={"components": 11}, warnings=["3 orphan tokens"], outputs=["build/"],
        )
        entry = journal.read_entries("example", root=self.root)[0]
        self.assertEqual(entry.counts["components"], 11)
        self.assertEqual(entry.warnings, ["3 orphan tokens"])
        self.assertEqual(entry.outputs, ["build/"])

    def test_no_journal_yet_is_not_an_error(self):
        self.assertEqual(journal.read_entries("never-run", root=self.root), [])


class Reading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_corrupt_line_is_surfaced_not_skipped(self):
        # A journal with a silent hole in it is worse than one that admits it.
        directory = journal.journal_dir("example", root=self.root)
        directory.mkdir(parents=True)
        (directory / "2026-08-04.jsonl").write_text(
            json.dumps({"tool": "theme", "timestamp": "2026-08-04T12:00:00+00:00"})
            + "\n{ this is not json\n"
        )
        entries = journal.read_entries("example", root=self.root)
        self.assertEqual(len(entries), 2)
        self.assertIn("UNREADABLE", [e.tool for e in entries])

    def test_since_filters_by_day(self):
        journal.append("example", "old", root=self.root, now=moment("2026-08-01"))
        journal.append("example", "new", root=self.root, now=moment("2026-08-04"))
        entries = journal.read_entries("example", root=self.root, since="2026-08-04")
        self.assertEqual([e.tool for e in entries], ["new"])


class Rendering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _pin(self, ref):
        sandbox = self.root / "sandbox"
        sandbox.mkdir(exist_ok=True)
        (sandbox / "pins.env").write_text(f"BUILDER_REF={ref}\n")

    def test_an_empty_journal_says_so_rather_than_rendering_blank(self):
        text = journal.render("example", root=self.root)
        self.assertIn("No journal entries", text)
        self.assertIn("worth chasing", text)

    def test_the_log_includes_counts_and_the_pin(self):
        self._pin("15cb01e4")
        journal.append(
            "example", "theme", root=self.root, now=moment("2026-08-04"),
            counts={"components": 11}, notes="Theming pass.",
        )
        text = journal.render("example", root=self.root)
        self.assertIn("Theming pass.", text)
        self.assertIn("| components | 11 |", text)
        self.assertIn("15cb01e4", text)

    def test_runs_spanning_two_builder_pins_are_flagged(self):
        # Otherwise the log reads as one continuous history when the payloads
        # were built against Builders that may not agree.
        self._pin("aaaaaaaa")
        journal.append("example", "first", root=self.root, now=moment("2026-08-01"))
        self._pin("bbbbbbbb")
        journal.append("example", "second", root=self.root, now=moment("2026-08-04"))
        text = journal.render("example", root=self.root)
        self.assertIn("more than one Builder commit", text)

    def test_a_single_pin_produces_no_warning(self):
        self._pin("15cb01e4")
        journal.append("example", "first", root=self.root, now=moment("2026-08-01"))
        journal.append("example", "second", root=self.root, now=moment("2026-08-04"))
        self.assertNotIn("more than one Builder commit", journal.render("example", root=self.root))


if __name__ == "__main__":
    unittest.main()
