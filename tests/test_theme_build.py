"""Tests for the W2 theme workflow and the generated catalogue.

The build's job is to turn declarative design inputs into payloads without ever
producing one that is quietly wrong. These cover the ordering that matters
(tokens before composition), the `@token` resolution, and the two states a site
can be in — never minted, or minted and read back — which must produce very
different plans.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.primitives.tokens import Applied  # noqa: E402
from buildsmith.tools import docgen as docgen
from buildsmith.workflows.theme import build_site, resolve_tokens  # noqa: E402
from buildsmith.workflows.theme.build import BuildError  # noqa: E402

EXAMPLE = ROOT / "sites" / "example"


class TokenResolution(unittest.TestCase):
    def setUp(self):
        self.applied = Applied.from_dict(
            {"tokens": {"brand": {"uuid": "u1", "value": "#0a7"}}}
        )

    def test_a_sigil_becomes_a_var_reference_with_a_fallback(self):
        self.assertEqual(resolve_tokens("@brand", self.applied), "var(--u1, #0a7)")

    def test_resolution_reaches_nested_structures(self):
        spec = {"baseStyles": {"color": "@brand"}, "children": [{"a": ["@brand"]}]}
        out = resolve_tokens(spec, self.applied)
        self.assertEqual(out["baseStyles"]["color"], "var(--u1, #0a7)")
        self.assertEqual(out["children"][0]["a"][0], "var(--u1, #0a7)")

    def test_an_unknown_token_raises_rather_than_passing_through(self):
        # Otherwise "@brnad-primary" renders as literal text in a style.
        with self.assertRaises(BuildError) as caught:
            resolve_tokens({"color": "@brnad-primary"}, self.applied)
        self.assertIn("brnad-primary", str(caught.exception))

    def test_ordinary_strings_are_untouched(self):
        self.assertEqual(resolve_tokens("1px solid", self.applied), "1px solid")
        self.assertEqual(resolve_tokens("hello@example.test", self.applied), "hello@example.test")


class BuildingTheExampleSite(unittest.TestCase):
    def test_it_builds(self):
        result = build_site(EXAMPLE, site="example")
        self.assertEqual(result.counts["components"], 2)
        self.assertEqual(result.counts["templates"], 1)
        self.assertGreaterEqual(result.counts["pages"], 1)

    def test_an_unminted_site_plans_to_mint_everything(self):
        # The fixture has no tokens-applied.json, so nothing is live yet.
        result = build_site(EXAMPLE, site="example")
        self.assertEqual({op.kind for op in result.token_plan.operations}, {"mint"})

    def test_an_unminted_site_warns_that_the_payloads_are_a_preview(self):
        result = build_site(EXAMPLE, site="example")
        self.assertTrue(any("UNMINTED" in w for w in result.warnings))

    def test_a_synced_site_plans_nothing_and_warns_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d) / "example"
            shutil.copytree(EXAMPLE, site)
            manifest = json.loads((site / "design" / "tokens.json").read_text())["tokens"]
            (site / "tokens-applied.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            key: {
                                "uuid": f"uuid-{i}",
                                "value": spec["value"],
                                "dark_value": spec.get("dark_value"),
                                "variable_name": spec["label"],
                                "type": spec["type"],
                                "group": spec["group"],
                            }
                            for i, (key, spec) in enumerate(manifest.items())
                        }
                    }
                )
            )
            result = build_site(site, site="example")
        self.assertEqual(len(result.token_plan), 0)
        self.assertEqual(result.warnings, [])
        # And every colour is now a real reference, not a literal.
        styles = result.components[0].block["baseStyles"]
        self.assertTrue(all(v.startswith("var(--uuid-") for v in styles.values()))

    def test_payloads_can_be_written_out(self):
        result = build_site(EXAMPLE, site="example")
        with tempfile.TemporaryDirectory() as d:
            written = result.write(d)
            names = {p.name for p in written}
        self.assertIn("token-plan.json", names)
        self.assertIn("site-header.json", names)


class BuildRefusals(unittest.TestCase):
    def _site(self, d, *, drop=None):
        site = Path(d) / "example"
        shutil.copytree(EXAMPLE, site)
        if drop:
            (site / drop).unlink()
        return site

    def test_a_site_with_no_design_inputs_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            bare = Path(d) / "nothing"
            bare.mkdir()
            with self.assertRaises(BuildError) as caught:
                build_site(bare)
        self.assertIn("design inputs", str(caught.exception))

    def test_a_missing_template_is_refused(self):
        # The no-exceptions rule, enforced at the workflow level too.
        with tempfile.TemporaryDirectory() as d:
            site = self._site(d, drop="design/template.json")
            with self.assertRaises(BuildError) as caught:
                build_site(site)
        self.assertIn("emits a template", str(caught.exception))

    def test_a_missing_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            site = self._site(d, drop="design/tokens.json")
            with self.assertRaises(BuildError) as caught:
                build_site(site)
        self.assertIn("defined by its tokens", str(caught.exception))

    def test_malformed_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            site = self._site(d)
            (site / "design" / "tokens.json").write_text("{ not json")
            with self.assertRaises(BuildError) as caught:
                build_site(site)
        self.assertIn("tokens.json", str(caught.exception))


class Catalogue(unittest.TestCase):
    def test_the_committed_catalogue_is_current(self):
        # The same assertion the pre-commit hook makes. If this fails, run
        # buildsmith docs and stage the result.
        committed = (ROOT / "docs" / "catalog.md").read_text()
        self.assertEqual(committed, docgen.generate("example"))

    def test_generation_is_deterministic(self):
        self.assertEqual(docgen.generate("example"), docgen.generate("example"))

    def test_it_lists_tokens_and_the_components_that_use_them(self):
        text = docgen.generate("example")
        self.assertIn("brand-primary", text)
        self.assertIn("site-header", text)
        self.assertIn("Tokens consumed:", text)

    def test_it_shows_logical_keys_rather_than_uuids(self):
        # uuids are per-site and opaque; a reader can act on a key.
        text = docgen.generate("example")
        self.assertIn("`brand-ink`", text)
        self.assertNotIn("UNMINTED-", text)


if __name__ == "__main__":
    unittest.main()
