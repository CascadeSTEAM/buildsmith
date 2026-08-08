"""#17: `optimize baseline`'s summary line must parse as English.

`manifest["scripts_scanned"]` is `int | str` — a count in the normal case,
or `"UNSCANNED — ..."` (already a complete sentence) when neither script
source exists. The CLI used to append " scripts scanned" unconditionally,
producing two fragments jammed together with no punctuation between them:
"... UNSCANNED — no client-script source found scripts scanned".
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith import cli  # noqa: E402
from buildsmith.workflows.optimize import baseline as baseline_mod  # noqa: E402


def _manifest(scripts_scanned) -> dict:
    return {
        "routes_captured": ["/", "/about"],
        "routes_skipped": {},
        "viewports": [(1280, 800), (768, 1024), (375, 812)],
        "scripts_scanned": scripts_scanned,
        "checkpoint": {"content_hash": "deadbeefcafe0000"},
    }


class BaselineSummaryLineTest(unittest.TestCase):
    def _run(self, scripts_scanned) -> str:
        out = io.StringIO()
        with mock.patch.object(
            baseline_mod, "build_baseline", return_value=_manifest(scripts_scanned)
        ), mock.patch("buildsmith.tools.journal.append"), redirect_stdout(out):
            code = cli.main(["optimize", "baseline", "--site", "x"])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_an_unscanned_result_reads_as_one_clean_sentence(self):
        text = self._run("UNSCANNED — no client-script source found")
        self.assertIn(
            "2 routes x 3 viewports, UNSCANNED — no client-script source found", text
        )
        self.assertNotIn("found scripts scanned", text)

    def test_a_real_count_still_says_scripts_scanned(self):
        text = self._run(12)
        self.assertIn("2 routes x 3 viewports, 12 scripts scanned", text)


if __name__ == "__main__":
    unittest.main()
