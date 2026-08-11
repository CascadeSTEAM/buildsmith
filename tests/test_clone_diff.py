"""`compare_rules` compares generated rules by what they SAY, not by their name.

Builder mints a `.fb-<hash>` class name per site, so the same styles always
wear different names on any two renders. publish_verify used to diff selector
*keys* and reported 71 missing rules on a page that was identical to dev —
three routes, three floods, zero real differences. The bundle comparison exists
to make those green, and these tests hold its line: identical declarations
match whatever their names are, a changed declaration is still an omission, and
the only differences ever classified away are the importer's own font-stack
reduction and declarations the clone's renderer *adds* — never something the
source said that the clone dropped.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.tools import clone_diff as cd  # noqa: E402


class CompareRules(unittest.TestCase):
    def test_same_declarations_different_hash_names_match(self):
        # The whole point: hashes re-mint per site, declarations are identical.
        src = {".fb-aaa111": {"color: red", "font-size: 14px"}}
        cln = {".fb-bbb222": {"color: red", "font-size: 14px"}}
        result = cd.compare_rules(src, cln)
        self.assertTrue(result.ok)
        self.assertEqual(result.generated_missing, 0)
        self.assertEqual(result.missing_selectors, [])

    def test_a_changed_declaration_is_still_missing(self):
        src = {".fb-aaa111": {"color: red"}, ".fb-ccc333": {"color: blue"}}
        cln = {".fb-bbb222": {"color: red"}, ".fb-ddd444": {"color: green"}}
        result = cd.compare_rules(src, cln)
        self.assertFalse(result.ok)
        self.assertEqual(result.generated_missing, 1)

    def test_media_query_context_is_part_of_the_match(self):
        # Same declarations under the same condition but different hashes: match.
        src = {"@media (max-width: 767px) .fb-aaa111": {"display: none"}}
        cln = {"@media (max-width: 767px) .fb-bbb222": {"display: none"}}
        self.assertTrue(cd.compare_rules(src, cln).ok)
        # Same declarations under DIFFERENT conditions: not the same rule.
        src = {"@media (max-width: 767px) .fb-aaa111": {"display: none"}}
        cln = {"@media (min-width: 768px) .fb-bbb222": {"display: none"}}
        self.assertFalse(cd.compare_rules(src, cln).ok)

    def test_stable_selectors_are_still_compared_by_name(self):
        # A name we control is a name that must match.
        src = {".site-header": {"color: red"}}
        cln = {}
        result = cd.compare_rules(src, cln)
        self.assertFalse(result.ok)
        self.assertIn(".site-header", result.missing_selectors)
        self.assertEqual(result.generated_missing, 0)

    def test_clone_extra_rules_are_not_reported(self):
        # Only source-absent is an omission; the clone rendering more is not.
        src = {".fb-aaa111": {"color: red"}}
        cln = {".fb-bbb222": {"color: red"}, ".fb-eee555": {"display: block"}}
        self.assertTrue(cd.compare_rules(src, cln).ok)


class GeneratedClassification(unittest.TestCase):
    def test_clone_rule_covering_the_source_is_not_an_omission(self):
        # The pin's renderer adds overflow-wrap that the live renderer lacks.
        # Every source declaration is present inside the clone rule, so nothing
        # the source said is missing — reported as covered, not as a failure.
        src = {".fb-aaa111": {"color: red"}}
        cln = {".fb-bbb222": {"color: red", "overflow-wrap: break-word"}}
        result = cd.compare_rules(src, cln)
        self.assertTrue(result.ok)
        self.assertEqual(result.generated_missing, 0)
        self.assertEqual(sum(result.generated_covered.values()), 1)

    def test_font_stack_plus_renderer_extra_is_a_transformation(self):
        # The live Builder emits stacks; the pin emits the single first family
        # AND its own overflow-wrap. The only thing the source has that the
        # clone does not is the stack, which the importer reduces on purpose.
        src = {".fb-aaa111": {
            "font-family: Nunito,\\ ui-sans-serif,\\ system-ui,\\ sans-serif",
            "color: red",
        }}
        cln = {".fb-bbb222": {
            "font-family: Nunito",
            "color: red",
            "overflow-wrap: break-word",
        }}
        result = cd.compare_rules(src, cln)
        self.assertTrue(result.ok)
        self.assertEqual(result.generated_missing, 0)
        self.assertEqual(result.font_stacks_reduced, 1)

    def test_wrong_first_family_is_not_absorbed(self):
        # The whole safety property: if the converter kept the SECOND family,
        # the difference has the same shape as the intended reduction. It must
        # not be waved through.
        src = {".fb-aaa111": {"font-family: Skybald,\\ Merriline,\\ cursive"}}
        cln = {".fb-bbb222": {"font-family: Merriline"}}
        result = cd.compare_rules(src, cln)
        self.assertFalse(result.ok)
        self.assertEqual(result.generated_missing, 1)
        self.assertEqual(result.font_stacks_reduced, 0)

    def test_a_changed_neighbour_declaration_is_not_absorbed(self):
        # The stack reduced, but color went red -> blue too. That change is a
        # defect and must survive the classification.
        src = {".fb-aaa111": {
            "font-family: Nunito,\\ system-ui,\\ sans-serif", "color: red"}}
        cln = {".fb-bbb222": {"font-family: Nunito", "color: blue"}}
        result = cd.compare_rules(src, cln)
        self.assertFalse(result.ok)
        self.assertEqual(result.generated_missing, 1)
        self.assertEqual(result.font_stacks_reduced, 0)

    def test_a_single_family_font_change_is_not_absorbed(self):
        # Not a stack, so no reduction is involved — this is the maintain
        # scenario's font change and it stays a defect.
        src = {".fb-aaa111": {"font-family: Nunito"}}
        cln = {".fb-bbb222": {"font-family: Inter"}}
        result = cd.compare_rules(src, cln)
        self.assertFalse(result.ok)
        self.assertEqual(result.generated_missing, 1)


class GeneratedGrouping(unittest.TestCase):
    def test_identical_differences_group_into_one_signature(self):
        # 42 one-declaration differences must read as one finding, not forty-two.
        src = {
            ".fb-aaa111": {"color: red", "padding: 4px"},
            ".fb-ccc333": {"color: red", "padding: 4px"},
        }
        cln = {".fb-bbb222": {"color: red"}, ".fb-ddd444": {"color: red"}}
        result = cd.compare_rules(src, cln)
        self.assertEqual(result.generated_missing, 2)
        self.assertEqual(len(result.generated_omitted), 1)
        signature, count = next(iter(result.generated_omitted.items()))
        self.assertEqual(count, 2)
        self.assertIn("padding: 4px", signature[1])

    def test_distinct_differences_get_distinct_signatures(self):
        src = {
            ".fb-aaa111": {"color: red", "padding: 4px"},
            ".fb-ccc333": {"font-weight: 700", "color: blue"},
        }
        cln = {".fb-bbb222": {"color: red"}, ".fb-ddd444": {"color: blue"}}
        result = cd.compare_rules(src, cln)
        self.assertEqual(result.generated_missing, 2)
        self.assertEqual(len(result.generated_omitted), 2)


class CompareWiring(unittest.TestCase):
    def test_compare_fetches_then_delegates_to_compare_rules(self):
        with mock.patch.object(cd, "fetch") as fetch:
            fetch.side_effect = [
                "<html><style>.fb-aaa111{color:red}</style></html>",
                "<html><style>.fb-bbb222{color:red}</style></html>",
            ]
            result = cd.compare("https://src.test/", "https://cln.test/")
        self.assertEqual(
            fetch.call_args_list,
            [mock.call("https://src.test/"), mock.call("https://cln.test/")],
        )
        # Identical declarations under re-minted hashes: no selector findings.
        self.assertTrue(result.ok)
        self.assertEqual(result.generated_missing, 0)


if __name__ == "__main__":
    unittest.main()
