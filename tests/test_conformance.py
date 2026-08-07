"""The check that asks "would Builder have written this?".

Everything else in the suite compares rendered output. That is why BS-022
reached the owner: a clone whose root block was `<html>`, whose `<head>` and
`<title>` were blocks, and whose `font-family` held a CSS stack rendered
perfectly and was unusable in the editor.

So the fixtures here are the *actual shapes that shipped*, not invented ones.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildsmith.tools import conformance
from buildsmith.tools.conformance import check_blocks, check_payload_dir

GOOD = [{"element": "div", "baseStyles": {"fontFamily": "Merriline"}, "children": [
    {"element": "h1", "baseStyles": {"fontFamily": "Skybald"}},
]}]

#: What the converter actually emitted before BS-022 was fixed.
SHIPPED_BROKEN = [{"element": "html", "children": [
    {"element": "head", "children": [{"element": "title", "innerHTML": "Menu"}]},
    {"element": "div", "baseStyles": {"fontFamily": "Skybald, Merriline, cursive"}},
]}]


def rules(findings) -> set[str]:
    return {f.rule for f in findings}


class SkeletonTest(unittest.TestCase):
    def test_a_conformant_page_has_no_findings(self) -> None:
        self.assertEqual(check_blocks(GOOD, "ok"), [])

    def test_the_shape_that_shipped_is_caught(self) -> None:
        found = rules(check_blocks(SHIPPED_BROKEN, "menu"))
        self.assertIn("skeleton-root", found)
        self.assertIn("skeleton-block", found)
        self.assertIn("font-stack", found)

    def test_html_root_is_named_as_the_root_problem(self) -> None:
        """The root is the one that causes the visible symptom — an unstyled
        <html> shrink-wraps in the canvas — so it must not be reported merely
        as one more nested skeleton element."""
        findings = check_blocks([{"element": "html", "children": []}], "p")
        self.assertEqual(rules(findings), {"skeleton-root"})

    def test_nested_skeleton_is_caught_at_any_depth(self) -> None:
        blocks = [{"element": "div", "children": [
            {"element": "div", "children": [{"element": "style"}]}
        ]}]
        self.assertIn("skeleton-block", rules(check_blocks(blocks, "p")))

    def test_semantic_roots_are_allowed(self) -> None:
        for element in ("div", "section", "main", "header", "footer", "nav"):
            self.assertEqual(check_blocks([{"element": element}], "p"), [], element)

    def test_an_unusual_root_is_reported_but_distinctly(self) -> None:
        """A `<span>` root is odd but not the BS-022 failure, and conflating the
        two would make the real one harder to find."""
        self.assertEqual(rules(check_blocks([{"element": "span"}], "p")), {"unusual-root"})

    def test_no_blocks_at_all_is_a_finding(self) -> None:
        self.assertEqual(rules(check_blocks([], "p")), {"empty"})


class FontTest(unittest.TestCase):
    def test_a_stack_is_caught_in_every_bucket(self) -> None:
        for bucket in conformance.STYLE_BUCKETS:
            blocks = [{"element": "div", bucket: {"fontFamily": "A, B"}}]
            self.assertIn("font-stack", rules(check_blocks(blocks, "p")), bucket)

    def test_a_css_escape_is_caught(self) -> None:
        """`Open\\ Sans` reduced from a stack but kept its escape would be sent
        to Google Fonts as `Open%5C%20Sans` — the same 400, harder to spot."""
        blocks = [{"element": "div", "baseStyles": {"fontFamily": "Open\\ Sans"}}]
        self.assertIn("font-escape", rules(check_blocks(blocks, "p")))

    def test_a_single_family_passes(self) -> None:
        blocks = [{"element": "div", "baseStyles": {"fontFamily": "Open Sans"}}]
        self.assertEqual(check_blocks(blocks, "p"), [])

    def test_hyphenated_key_is_checked_too(self) -> None:
        """Payloads written by hand or by another tool may use the CSS spelling."""
        blocks = [{"element": "div", "baseStyles": {"font-family": "A, B"}}]
        self.assertIn("font-stack", rules(check_blocks(blocks, "p")))


class PayloadDirTest(unittest.TestCase):
    def _write(self, directory: Path, name: str, record: dict) -> None:
        (directory / name).write_text(json.dumps(record))

    def test_blocks_serialised_as_json_text_are_parsed(self) -> None:
        """Frappe stores `blocks` as text, and that is how payloads are written.
        Failing to parse it would make the check silently inspect nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "p.json", {
                "doctype": "Builder Page", "route": "menu",
                "blocks": json.dumps(SHIPPED_BROKEN),
            })
            result = check_payload_dir(d)
            self.assertEqual(result.pages_checked, 1)
            self.assertFalse(result.ok)

    def test_non_page_payloads_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "c.json", {"doctype": "Builder Component", "block": "{}"})
            result = check_payload_dir(d)
            self.assertEqual(result.pages_checked, 0)
            self.assertTrue(result.ok)

    def test_an_empty_directory_cannot_be_checked(self) -> None:
        """Nothing to check must not read as a pass — exit 2, not 0."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(conformance.main(["--dir", tmp]), conformance.EXIT_UNCHECKED)

    def test_a_missing_directory_cannot_be_checked(self) -> None:
        self.assertEqual(
            conformance.main(["--dir", "/nonexistent/nope"]), conformance.EXIT_UNCHECKED
        )

    def test_unreadable_blocks_are_a_finding_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, "p.json", {
                "doctype": "Builder Page", "route": "x", "blocks": "{not json",
            })
            self.assertFalse(check_payload_dir(d).ok)


if __name__ == "__main__":
    unittest.main()


class NothingCheckedTest(unittest.TestCase):
    def test_zero_pages_does_not_print_conformant(self) -> None:
        """Silence must not read as approval — the recurring failure in this
        project is a check that measures nothing and says it passed."""
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            conformance.report(conformance.Report())
        text = out.getvalue()
        self.assertNotIn("Conformant", text)
        self.assertIn("NOTHING CHECKED", text)
