"""End to end: build the example site, then try to break it and get caught.

Runs through the **CLI**, not the library, because the CLI is the interface a
container and a TUI both go through — testing the library here would leave the
thing users actually invoke unexercised.

This is the M1 exit criterion, exercised the way it would actually happen — real
files, the real CLI, real exit codes. The unit tests prove each piece in
isolation; this proves they compose, and that the formats one tool emits are the
formats the next one reads.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.workflows.theme import build_site  # noqa: E402


def run_simulate(state_path: Path, payload_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", "buildsmith.cli", "simulate",
            "--state", str(state_path),
            "--payload", str(payload_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


class BuildThenSimulate(unittest.TestCase):
    """Build real payloads, pretend they are live, then propose a bad change."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.result = build_site(ROOT / "sites" / "example", site="example")
        self.result.write(self.work / "build")

        header = next(c for c in self.result.components if c.component_id == "site-header")
        self.header = header

        # A page as Builder would hold it: override shells mirroring the
        # component, keyed by referenceBlockId.
        def shells(block):
            return {
                "blockId": f"shell-{block['blockId']}",
                "referenceBlockId": block["blockId"],
                "element": None,
                "innerHTML": None,
                "children": [shells(c) for c in block.get("children") or []],
            }

        page_root = shells(header.block)
        page_root["extendedFromComponent"] = "site-header"

        self.state_path = self.work / "state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "components": {"site-header": {"block": header.block}},
                    "pages": [
                        {"name": "home", "route": "/", "blocks": [page_root]},
                        {"name": "about", "route": "/about", "blocks": [page_root]},
                    ],
                },
                indent=2,
            )
        )

    def test_the_built_payloads_are_on_disk_and_readable(self):
        emitted = json.loads((self.work / "build" / "components" / "site-header.json").read_text())
        self.assertEqual(emitted["doctype"], "Builder Component")
        self.assertEqual(emitted["component_id"], "site-header")
        plan = json.loads((self.work / "build" / "token-plan.json").read_text())
        self.assertEqual(plan["doctype"], "Builder Variable")

    def test_an_unchanged_payload_simulates_clean(self):
        payload = self.work / "unchanged.json"
        payload.write_text(json.dumps(self.header.record()))
        completed = run_simulate(self.state_path, payload)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("clean", completed.stdout)

    def test_a_deliberately_broken_payload_is_caught(self):
        # The near-miss, reproduced: recompose the component so its blockIds are
        # re-issued. Every page's shells then match nothing.
        broken = json.loads(json.dumps(self.header.record()))

        def reissue(block):
            block["blockId"] = "REISSUED-" + block["blockId"]
            for child in block.get("children") or []:
                reissue(child)

        for child in broken["block"].get("children") or []:
            reissue(child)

        payload = self.work / "broken.json"
        payload.write_text(json.dumps(broken))

        completed = run_simulate(self.state_path, payload)
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("COLLAPSE", completed.stdout)
        self.assertIn("TRAP-001", completed.stderr)
        # Both consuming pages, not just the first one found.
        self.assertIn("/about", completed.stdout)

    def test_the_json_report_is_machine_readable(self):
        payload = self.work / "unchanged.json"
        payload.write_text(json.dumps(self.header.record()))
        completed = subprocess.run(
            [
                sys.executable, "-m", "buildsmith.tools.simulate",
                "--state", str(self.state_path), "--payload", str(payload), "--json",
            ],
            capture_output=True, text=True, cwd=ROOT,
        )
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        self.assertFalse(report["vacuous"])
        self.assertEqual(report["pages_checked"], 2)


class JournalRecordsTheBuild(unittest.TestCase):
    def test_a_build_can_be_journalled_and_rendered(self):

        from buildsmith.tools import journal as journal

        result = build_site(ROOT / "sites" / "example", site="example")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            journal.append(
                "example", "theme", root=root, counts=result.counts,
                warnings=result.warnings, notes="End-to-end build.",
            )
            log = journal.render("example", root=root)
        self.assertIn("End-to-end build.", log)
        self.assertIn("| components | 2 |", log)


if __name__ == "__main__":
    unittest.main()
