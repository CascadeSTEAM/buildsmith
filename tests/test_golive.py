"""Tests for the generated go-live plan.

A plan is only worth reading if it is specific. These check it names this site's
real routes and payloads, that it only raises steps the site actually needs, and
that it never blurs who performs a live action.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.tools import golive as golive
from buildsmith.workflows.replicate import emit, replicate
from buildsmith.workflows.replicate.crawl import CrawlResult


class ThePlan(unittest.TestCase):
    def setUp(self):
        self.plan = golive.generate("example")

    def test_it_names_the_real_routes_and_payloads(self):
        self.assertIn("/about", self.plan)
        self.assertIn("components/site-header.json", self.plan)

    def test_snapshot_comes_before_anything_else(self):
        # The recovery step is worthless if it is reached after the damage.
        self.assertLess(self.plan.index("Builder Snapshot"), self.plan.index("## Tokens"))

    def test_token_readback_precedes_the_component_rebuild(self):
        # Composing before reading the map back bakes stale literals in.
        # Scoped to the Tokens section: the word "rebuild" also appears in an
        # earlier build warning, which would make a whole-document search pass
        # or fail for the wrong reason.
        tokens = self.plan.split("## Tokens")[1].split("## Components")[0]
        self.assertLess(tokens.index("read the token map back"), tokens.index("rebuild"))

    def test_it_surfaces_build_warnings(self):
        self.assertIn("Build warnings", self.plan)

    def test_it_attributes_every_live_step(self):
        # A plan that blurs this is how a DNS change gets run from a design repo.
        for phrase in ("DNS and reverse proxy", "back up the site"):
            line = next(ln for ln in self.plan.splitlines() if phrase in ln)
            self.assertIn("operations project", line)

    def test_it_does_not_raise_developer_mode_for_a_site_that_does_not_need_it(self):
        # The example template has no template_group, so the gate does not apply.
        # Listing it anyway would dilute the steps that do matter.
        self.assertNotIn("enable `developer_mode`", self.plan)

    def test_the_verification_list_covers_every_public_page(self):
        section = self.plan.split("## Cutover")[1]
        self.assertIn("`/about`", section)
        # The template is not a public route, so it is not in the checklist.
        self.assertNotIn("template/example-marketing` —", section)

    def test_it_ends_with_the_rollback(self):
        self.assertIn("Restore the snapshot", self.plan)


class AReplicatedSitePlan(unittest.TestCase):
    """golive must plan a W1 site from its emitted build/ — no design inputs.

    The old code re-ran the W2 theme build, which refuses a site without
    `design/`, so a replicated site could never get a plan. It is built in a
    temp dir through the real pipeline (`replicate` + `emit`), which is
    also what an actual W1 site looks like on disk.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        site_dir = root / "sites" / "replica"
        (site_dir / "build").mkdir(parents=True)

        crawl = CrawlResult(pages={
            "": "<html><head><title>Welcome</title></head>"
                "<body><h1>Welcome</h1></body></html>",
            "menu": "<html><head><title>Menu</title></head>"
                    "<body><h1>Menu</h1></body></html>",
        })
        result = replicate(crawl, site="replica")
        self.assertEqual(result.coverage, 1.0)
        emit(result, site_dir / "build")
        (site_dir / "site.yml").write_text("workflow: replicate\n")
        self.root = root
        self.plan = golive.generate("replica", root=root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_plans_from_the_emitted_build(self):
        self.assertIn("| workflow | replicate |", self.plan)

    def test_it_names_the_real_routes(self):
        # The site root became route "home" (TRAP-014) and every source route
        # got a page, exactly like the site.yml routes on a real W1 site.
        self.assertIn("/home", self.plan)
        self.assertIn("/menu", self.plan)

    def test_a_plain_replicated_template_does_not_need_developer_mode(self):
        # A W1 template is a saved template without a template_group, so the
        # developer_mode gate and the fixture export do not apply.
        self.assertNotIn("developer_mode", self.plan)

    def test_snapshot_comes_before_anything_else(self):
        self.assertLess(self.plan.index("Builder Snapshot"), self.plan.index("## Tokens"))

    def test_it_names_the_prerequisite(self):
        # The home page needs Website Settings.home_page set, or / serves the
        # desk login (TRAP-014) — the plan must carry this for a W1 site too.
        self.assertIn("Website Settings.home_page", self.plan)

    def test_the_cache_is_cleared_after_applying(self):
        self.assertIn("clear the website cache", self.plan)

    def test_token_section_explains_w1_has_no_tokens(self):
        # W1 keeps colours inline in block styles; the plan must not pretend a
        # token manifest exists when there is none.
        self.assertIn("W1 replica", self.plan)

    def test_components_section_is_honest(self):
        self.assertIn("_No components in this build._", self.plan)

    def test_live_steps_are_attributed(self):
        for phrase in ("DNS and reverse proxy", "back up the site"):
            line = next(ln for ln in self.plan.splitlines() if phrase in ln)
            self.assertIn("operations project", line)

    def test_the_verification_list_covers_every_public_page(self):
        section = self.plan.split("## Cutover")[1]
        self.assertIn("`/home`", section)
        self.assertIn("`/menu`", section)
        # The template is not a public route.
        self.assertNotIn("`/template/replica`", section)

    def test_it_ends_with_the_rollback(self):
        self.assertIn("Restore the snapshot", self.plan)


if __name__ == "__main__":
    unittest.main()
