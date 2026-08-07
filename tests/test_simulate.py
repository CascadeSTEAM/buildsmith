"""Tests for buildsmith simulate — TRAP-001 caught before it reaches a site.

The headline case is the one from the incident record: a freshly composed tree
written over an in-use component, which would have wiped the header and footer
across all 13 pages.

The rest guard the ways a simulator can lie: passing because it checked nothing,
passing because the export was incomplete, and blaming a payload for damage that
was already there.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.tools import simulate as simulate_mod

simulate = simulate_mod.simulate
load_state = simulate_mod.load_state
pages_using = simulate_mod.pages_using


def component(child_id="nav-1", extra_children=()):
    return {
        "blockId": "root",
        "element": "header",
        "children": [
            {"blockId": child_id, "element": "nav", "innerHTML": "Home"},
            *extra_children,
        ],
    }


def page(name, *, reference="nav-1", pinned=False, route=None):
    """A page carrying the empty override shells Builder leaves behind."""
    shell = {
        "blockId": f"{name}-shell",
        "referenceBlockId": "root",
        "extendedFromComponent": "site-header",
        "children": [
            {
                "blockId": f"{name}-child",
                "referenceBlockId": reference,
                "element": None,
                "children": [],
            }
        ],
    }
    if pinned:
        shell["componentVersion"] = "snapshot-1"
    return {"name": name, "route": route or f"/{name}", "blocks": [shell]}


def state(pages, current=None):
    return {
        "components": {"site-header": {"block": current or component()}},
        "pages": pages,
    }


class TheNearMiss(unittest.TestCase):
    def test_a_freshly_composed_tree_collapses_every_consuming_page(self):
        pages = [page(f"page-{i}") for i in range(13)]
        report = simulate(state(pages), [{"component_id": "site-header",
                                          "block": component(child_id="REISSUED")}])
        self.assertFalse(report.ok)
        self.assertEqual(len(report.collapses), 13)
        self.assertIn("element=None", report.collapses[0].detail)
        self.assertIn("COLLAPSE", report.summary())

    def test_preserving_ids_is_clean(self):
        pages = [page(f"page-{i}") for i in range(13)]
        restyled = component()
        restyled["children"][0]["baseStyles"] = {"color": "var(--u1, #000)"}
        report = simulate(state(pages), [{"component_id": "site-header", "block": restyled}])
        self.assertTrue(report.ok)
        self.assertEqual(report.collapses, [])
        self.assertIn("clean", report.summary())

    def test_the_report_names_the_pages(self):
        report = simulate(
            state([page("home", route="/"), page("about", route="/about")]),
            [{"component_id": "site-header", "block": component(child_id="REISSUED")}],
        )
        routes = {f.route for f in report.collapses}
        self.assertEqual(routes, {"/", "/about"})


class AdditionsRenderNowhere(unittest.TestCase):
    def test_a_new_child_is_reported_as_unrenderable(self):
        grown = component(extra_children=({"blockId": "added", "element": "button"},))
        report = simulate(state([page("home")]), [{"component_id": "site-header", "block": grown}])
        # Not a collapse — nothing breaks — but it will not appear either.
        self.assertTrue(report.ok)
        self.assertEqual(len(report.unrenderable), 1)
        self.assertIn("no shell", report.unrenderable[0].detail)
        self.assertIn("ComponentSyncer", report.summary())


class DoesNotLie(unittest.TestCase):
    def test_an_empty_export_is_not_a_pass(self):
        # The failure mode every guard in this repo is built against: a run that
        # checked nothing must not read like a clean one.
        report = simulate({"pages": [], "components": {}},
                          [{"component_id": "site-header", "block": component()}])
        self.assertTrue(report.vacuous)
        self.assertFalse(report.ok)
        self.assertIn("NOTHING WAS CHECKED", report.summary())

    def test_an_incomplete_export_raises_rather_than_passing(self):
        # Pages use the component but the export omits it: comparing against
        # nothing would pass vacuously.
        with self.assertRaises(ValueError) as caught:
            simulate(
                {"components": {}, "pages": [page("home")]},
                [{"component_id": "site-header", "block": component()}],
            )
        self.assertIn("incomplete", str(caught.exception))

    def test_pre_existing_damage_is_not_blamed_on_the_payload(self):
        # This page's shell already matched nothing before the change.
        orphan = page("home", reference="long-gone")
        report = simulate(state([orphan]), [{"component_id": "site-header", "block": component()}])
        self.assertTrue(report.ok)
        self.assertEqual(len(report.pre_existing), 1)
        self.assertIn("pre-existing", report.summary())

    def test_a_pinned_page_is_unaffected(self):
        # componentVersion resolves against a snapshot, so changing the live
        # component cannot break it. Counting it would be a false positive.
        report = simulate(
            state([page("home", pinned=True)]),
            [{"component_id": "site-header", "block": component(child_id="REISSUED")}],
        )
        self.assertEqual(report.collapses, [])
        self.assertEqual(report.pinned_pages, ["home"])

    def test_a_brand_new_component_cannot_collapse_anything(self):
        report = simulate(state([page("home")]),
                          [{"component_id": "brand-new", "block": component()}])
        self.assertTrue(report.ok)


class UsageDetection(unittest.TestCase):
    def test_nested_usage_is_found(self):
        nested = {
            "name": "deep",
            "route": "/deep",
            "blocks": [{"blockId": "a", "children": [
                {"blockId": "b", "children": [
                    {"blockId": "c", "extendedFromComponent": "site-header"}]}]}],
        }
        self.assertEqual(len(pages_using([nested], "site-header")), 1)

    def test_draft_blocks_count_as_usage(self):
        # An unpublished draft is damage waiting to be published.
        draft = {"name": "d", "route": "/d", "blocks": [],
                 "draft_blocks": [{"blockId": "x", "extendedFromComponent": "site-header"}]}
        self.assertEqual(len(pages_using([draft], "site-header")), 1)

    def test_unrelated_pages_are_not_checked(self):
        other = {"name": "o", "route": "/o", "blocks": [{"blockId": "x"}]}
        self.assertEqual(pages_using([other], "site-header"), [])


class StateLoading(unittest.TestCase):
    def test_an_export_without_pages_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"components": {}}))
            with self.assertRaises(ValueError) as caught:
                load_state(path)
        self.assertIn("vacuous", str(caught.exception))

    def test_blocks_may_be_a_json_string(self):
        # Builder stores them as Long Text, so a raw export has strings.
        p = page("home")
        p["blocks"] = json.dumps(p["blocks"])
        self.assertEqual(len(pages_using([p], "site-header")), 1)


if __name__ == "__main__":
    unittest.main()
