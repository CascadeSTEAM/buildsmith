"""Tests for the component layer.

Covers TRAP-001 (shell collapse), TRAP-002 (a skeleton carries no content),
TRAP-005 (component_id is name and must not diverge), and the mirror-image
failure that reading the pinned renderer turned up: children *added* to a
component render nowhere on existing pages, because `extend_block()` iterates
the page's shells and no shell references them.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.blocks import assign_ids, new_block  # noqa: E402
from buildsmith.primitives.components import (  # noqa: E402
    DOCTYPE,
    Component,
    ComponentError,
    assert_additions_acknowledged,
    assert_colours_tokenised,
    assert_content_preserved,
    compose,
    override_shells,
    revise,
    slug_to_component_id,
)
from buildsmith.primitives.tokens import Applied, Manifest  # noqa: E402


def live_header() -> dict:
    """A component as it exists on the site, with content and stable ids."""
    return assign_ids(
        new_block(
            "header",
            children=[
                new_block("nav", children=[new_block("a", inner_html="Home",
                                                     attributes={"href": "/"})]),
                new_block("address", inner_html="1 Example Way"),
            ],
        ),
        seed="component:site-header",
    )


class Trap001ShellCollapse(unittest.TestCase):
    def test_a_freshly_composed_tree_over_a_live_one_is_refused(self):
        # The near-miss on record: new ids match no page shell, so every
        # interior node renders as element=None across all consuming pages.
        fresh = assign_ids(
            new_block("header", children=[new_block("nav"), new_block("address")]),
            seed="something-else",
        )
        with self.assertRaises(ComponentError) as caught:
            revise({"component_id": "site-header", "block": live_header()}, fresh)
        self.assertIn("TRAP-001", str(caught.exception))

    def test_restyle_in_place_is_allowed(self):
        live = live_header()
        restyled = assign_ids(live_header(), seed="component:site-header")
        restyled["baseStyles"] = {"backgroundColor": "var(--u1, #fff)"}
        component = revise({"component_id": "site-header", "block": live}, restyled)
        self.assertEqual(component.component_id, "site-header")

    def test_previous_must_be_the_live_tree_not_a_reconstruction(self):
        with self.assertRaises(ComponentError) as caught:
            revise({"component_id": "site-header", "block": {}}, live_header())
        self.assertIn("read back", str(caught.exception).lower() + " read back")


class AdditionsRenderNowhere(unittest.TestCase):
    """The mirror image of the collapse, and the easier one to miss."""

    def test_a_new_child_is_refused_by_default(self):
        live = live_header()
        grown = assign_ids(live_header(), seed="component:site-header")
        grown["children"].append(new_block("button", block_id="brand-new"))
        with self.assertRaises(ComponentError) as caught:
            revise({"component_id": "site-header", "block": live}, grown)
        message = str(caught.exception)
        self.assertIn("brand-new", message)
        self.assertIn("ComponentSyncer", message)

    def test_acknowledging_it_records_the_sync_requirement(self):
        live = live_header()
        grown = assign_ids(live_header(), seed="component:site-header")
        grown["children"].append(new_block("button", block_id="brand-new"))
        component = revise(
            {"component_id": "site-header", "block": live}, grown, allow_additions=True
        )
        self.assertEqual(component.meta["requires_component_sync"], ["brand-new"])

    def test_no_additions_means_no_sync_note(self):
        live = live_header()
        same = assign_ids(live_header(), seed="component:site-header")
        self.assertEqual(revise({"component_id": "site-header", "block": live}, same).meta, {})

    def test_an_id_less_child_cannot_slip_past_the_guard(self):
        # block_ids() skips nodes without an id, so before this check existed a
        # child appended *without* one was neither a preserved id nor a detected
        # addition — the laziest possible mistake bypassed the whole guard.
        live = live_header()
        grown = assign_ids(live_header(), seed="component:site-header")
        grown["children"].append({"element": "button"})
        with self.assertRaises(ComponentError) as caught:
            revise({"component_id": "site-header", "block": live}, grown)
        message = str(caught.exception)
        self.assertIn("no blockId", message)
        self.assertIn("header/button[2]", message)

    def test_an_id_less_child_is_refused_even_when_additions_are_acknowledged(self):
        # allow_additions promises a ComponentSyncer pass over the *listed* ids;
        # an id-less node can never be listed, so the promise cannot cover it.
        live = live_header()
        grown = assign_ids(live_header(), seed="component:site-header")
        grown["children"].append({"element": "button"})
        with self.assertRaises(ComponentError):
            revise(
                {"component_id": "site-header", "block": live},
                grown,
                allow_additions=True,
            )

    def test_the_check_is_usable_on_its_own(self):
        before = new_block("div", block_id="a")
        after = new_block("div", block_id="a", children=[new_block("p", block_id="b")])
        with self.assertRaises(ComponentError):
            assert_additions_acknowledged(before, after)

    def test_the_standalone_check_also_refuses_an_id_less_tree(self):
        before = new_block("div", block_id="a")
        after = new_block("div", block_id="a", children=[{"element": "p"}])
        with self.assertRaises(ComponentError) as caught:
            assert_additions_acknowledged(before, after)
        self.assertIn("no blockId", str(caught.exception))


class Trap002SkeletonCarriesNoContent(unittest.TestCase):
    def test_dropping_inner_html_is_refused(self):
        before = new_block("div", block_id="a", inner_html="1 Example Way")
        after = new_block("div", block_id="a")
        with self.assertRaises(ComponentError) as caught:
            assert_content_preserved(before, after)
        self.assertIn("TRAP-002", str(caught.exception))

    def test_dropping_a_nav_link_is_refused(self):
        # A nav's content is as much its hrefs as its labels.
        before = new_block("a", block_id="a", inner_html="Home", attributes={"href": "/"})
        after = new_block("a", block_id="a", inner_html="Home")
        assert_content_preserved(before, after)  # innerHTML kept, so still fine
        stripped = new_block("a", block_id="a")
        with self.assertRaises(ComponentError):
            assert_content_preserved(before, stripped)

    def test_changing_content_is_fine(self):
        before = new_block("div", block_id="a", inner_html="old")
        after = new_block("div", block_id="a", inner_html="new")
        assert_content_preserved(before, after)


class ColoursMustBeTokens(unittest.TestCase):
    def test_a_hex_literal_is_refused(self):
        block = new_block("div", base_styles={"backgroundColor": "#0a7d55"})
        with self.assertRaises(ComponentError) as caught:
            assert_colours_tokenised(block)
        self.assertIn("literal colour", str(caught.exception))

    def test_functional_colour_notations_are_refused(self):
        for value in ("rgb(10 20 30)", "hsl(120 50% 50%)", "oklch(0.7 0.1 200)"):
            with self.subTest(value=value), self.assertRaises(ComponentError):
                assert_colours_tokenised(new_block("div", base_styles={"color": value}))

    def test_a_token_reference_passes(self):
        assert_colours_tokenised(
            new_block("div", base_styles={"backgroundColor": "var(--u1, #0a7d55)"})
        )

    def test_keywords_are_not_colours_to_tokenise(self):
        # transparent/inherit/currentColor legitimately are not tokens.
        for value in ("transparent", "inherit", "currentColor"):
            with self.subTest(value=value):
                assert_colours_tokenised(new_block("div", base_styles={"color": value}))

    def test_non_colour_properties_are_left_alone(self):
        assert_colours_tokenised(new_block("div", base_styles={"border": "1px solid"}))

    def test_nested_blocks_are_checked(self):
        tree = new_block("div", children=[new_block("p", base_styles={"color": "#fff"})])
        with self.assertRaises(ComponentError):
            assert_colours_tokenised(tree)


class Trap005ComponentIdentity(unittest.TestCase):
    def test_record_sets_component_id_which_becomes_name(self):
        record = Component("site-header", "Site Header", new_block("header")).record()
        self.assertEqual(record["doctype"], DOCTYPE)
        self.assertEqual(record["component_id"], "site-header")

    def test_update_payload_never_carries_component_id(self):
        # Sending it on an update is how a rename sneaks in, and a diverged
        # name/component_id breaks cache invalidation silently.
        payload = Component("site-header", "Site Header", new_block("header")).update_payload()
        self.assertNotIn("component_id", payload)
        self.assertEqual(payload["name"], "site-header")

    def test_ids_must_be_readable_slugs(self):
        for bad in ("Site Header", "site_header", "", "site--header-"):
            with self.subTest(slug=bad), self.assertRaises(ComponentError):
                slug_to_component_id(bad)
        self.assertEqual(slug_to_component_id("site-header"), "site-header")


class Composition(unittest.TestCase):
    def test_compose_is_deterministic(self):
        a = compose(component_id="site-header", component_name="H", root=new_block("header"))
        b = compose(component_id="site-header", component_name="H", root=new_block("header"))
        self.assertEqual(a.block["blockId"], b.block["blockId"])

    def test_compose_does_not_mutate_the_callers_tree(self):
        root = new_block("header")
        compose(component_id="site-header", component_name="H", root=root)
        self.assertNotIn("blockId", root)

    def test_compose_enforces_tokens_when_given_an_applied_map(self):
        applied = Applied.from_dict({"tokens": {"brand": {"uuid": "u1", "value": "#0a7"}}})
        with self.assertRaises(ComponentError):
            compose(
                component_id="site-header",
                component_name="H",
                root=new_block("header", base_styles={"backgroundColor": "#0a7"}),
                applied=applied,
            )

    def test_compose_refuses_a_stale_applied_map(self):
        # Composing before the token plan lands bakes yesterday's colours in.
        manifest = Manifest.from_dict({"tokens": {"brand": {"value": "#0b8"}}})
        applied = Applied.from_dict({"tokens": {"brand": {"uuid": "u1", "value": "#0a7"}}})
        with self.assertRaises(Exception) as caught:
            compose(
                component_id="site-header",
                component_name="H",
                root=new_block("header"),
                applied=applied,
                manifest=manifest,
            )
        self.assertIn("stale literals", str(caught.exception))

    def test_compose_rejects_a_token_in_an_untokenisable_property(self):
        with self.assertRaises(Exception) as caught:
            compose(
                component_id="site-header",
                component_name="H",
                root=new_block("header", base_styles={"fontWeight": "var(--u1, 600)"}),
            )
        self.assertIn("TRAP-004", str(caught.exception))


class ColourDisciplineClosesTheCheapHoles(unittest.TestCase):
    """Bootstrap critical review §4.1 — the gaps that defeated the discipline for free.

    `color: "white"` sailed past a regex that only knew hex and functional
    notation, and the shorthands — where literals actually live on real
    sites — were not scanned at all.
    """

    def test_named_colours_are_refused(self):
        for value in ("white", "White", "rebeccapurple"):
            with self.subTest(value=value), self.assertRaises(ComponentError):
                assert_colours_tokenised(new_block("div", base_styles={"color": value}))

    def test_css_color_4_notations_are_refused(self):
        for value in ("hwb(120 10% 10%)", "oklab(0.5 0.1 0.1)",
                      "color-mix(in oklch, #fff, #000)"):
            with self.subTest(value=value), self.assertRaises(ComponentError):
                assert_colours_tokenised(new_block("div", base_styles={"color": value}))

    def test_shorthands_are_scanned(self):
        cases = {
            "border": "1px solid #fff",
            "boxShadow": "0 1px 2px rgb(0 0 0 / 0.2)",
            "background": "linear-gradient(#fff, #000)",
            "outline": "2px dashed tan",
            "textDecoration": "underline wavy red",
        }
        for prop, value in cases.items():
            with self.subTest(prop=prop), self.assertRaises(ComponentError):
                assert_colours_tokenised(new_block("div", base_styles={prop: value}))

    def test_a_shorthand_with_no_colour_in_it_passes(self):
        assert_colours_tokenised(new_block("div", base_styles={"border": "1px solid"}))

    def test_a_literal_beside_a_token_reference_is_still_refused(self):
        # The old check passed the whole value if "var(--" appeared anywhere,
        # so the second shadow's literal rode along unseen.
        with self.assertRaises(ComponentError):
            assert_colours_tokenised(new_block(
                "div", base_styles={"boxShadow": "0 1px var(--u1, #000), 0 2px #fff"}
            ))

    def test_token_fallbacks_are_sanctioned_even_nested(self):
        for value in ("var(--u1, rgb(0 0 0))",
                      "0 1px var(--u1, #000), 0 2px var(--u2, white)"):
            with self.subTest(value=value):
                assert_colours_tokenised(
                    new_block("div", base_styles={"boxShadow": value})
                )

    def test_url_filenames_are_not_colours(self):
        assert_colours_tokenised(
            new_block("div", base_styles={"backgroundImage": "url(white.png)"})
        )


class RevisionKeepsTheStyleDiscipline(unittest.TestCase):
    """Bootstrap critical review §4.1 — revision is *the* sanctioned way to change an in-use
    component's styles, so it is exactly where colours change; it must run
    the same checks compose() does."""

    def _restyled(self, styles: dict) -> dict:
        tree = assign_ids(live_header(), seed="component:site-header")
        tree["baseStyles"] = styles
        return tree

    def test_revise_with_applied_refuses_a_literal_colour(self):
        applied = Applied.from_dict({"tokens": {"brand": {"uuid": "u1", "value": "#0a7"}}})
        with self.assertRaises(ComponentError) as caught:
            revise(
                {"component_id": "site-header", "block": live_header()},
                self._restyled({"backgroundColor": "white"}),
                applied=applied,
            )
        self.assertIn("literal colour", str(caught.exception))

    def test_revise_refuses_a_token_in_an_untokenisable_property(self):
        with self.assertRaises(Exception) as caught:
            revise(
                {"component_id": "site-header", "block": live_header()},
                self._restyled({"fontWeight": "var(--u1, 600)"}),
            )
        self.assertIn("TRAP-004", str(caught.exception))

    def test_manifest_without_applied_enables_nothing_and_says_so(self):
        # The caller who passed manifest= believes two checks are on; ignoring
        # it silently would be a vacuous pass wearing a safety argument.
        manifest = Manifest.from_dict({"tokens": {"brand": {"value": "#0b8"}}})
        with self.assertRaises(ComponentError):
            compose(component_id="site-header", component_name="H",
                    root=new_block("header"), manifest=manifest)
        with self.assertRaises(ComponentError):
            revise(
                {"component_id": "site-header", "block": live_header()},
                assign_ids(live_header(), seed="component:site-header"),
                manifest=manifest,
            )


class OverrideShells(unittest.TestCase):
    """`override_shells()` — extraction's other missing half (issue #19):
    the page-side tree `compose()`/`revise()` never built."""

    def _pair(self):
        # Two occurrences of the same shape, own ids preserved per page —
        # exactly what a page's original subtree looks like pre-extraction.
        component = compose(component_id="menu-card", component_name="Menu Card",
                            root=new_block("div", children=[
                                new_block("h2"), new_block("p")]))
        page_instance = assign_ids(
            new_block("div", children=[new_block("h2"), new_block("p")]),
            seed="page:home")
        return component, page_instance

    def test_shell_preserves_the_pages_own_blockids(self):
        component, instance = self._pair()
        shell = override_shells(component.block, instance, component_id="menu-card")
        self.assertEqual(shell["blockId"], instance["blockId"])
        self.assertEqual(shell["children"][0]["blockId"],
                         instance["children"][0]["blockId"])

    def test_shell_references_the_components_blockids(self):
        component, instance = self._pair()
        shell = override_shells(component.block, instance, component_id="menu-card")
        self.assertEqual(shell["referenceBlockId"], component.block["blockId"])
        self.assertEqual(shell["children"][1]["referenceBlockId"],
                         component.block["children"][1]["blockId"])

    def test_every_node_is_marked_extended_and_carries_no_content(self):
        component, instance = self._pair()
        shell = override_shells(component.block, instance, component_id="menu-card")
        for node in (shell, *shell["children"]):
            self.assertEqual(node["extendedFromComponent"], "menu-card")
            self.assertNotIn("element", node)
            self.assertNotIn("innerHTML", node)

    def test_refuses_a_shape_mismatch_rather_than_pair_by_position(self):
        component, instance = self._pair()
        instance["children"].append(new_block("span"))  # 3 children now, not 2
        with self.assertRaises(ComponentError) as caught:
            override_shells(component.block, instance, component_id="menu-card")
        self.assertIn("TRAP-001", str(caught.exception))

    def test_refuses_an_instance_node_with_no_blockid(self):
        component, instance = self._pair()
        del instance["blockId"]
        with self.assertRaises(ComponentError):
            override_shells(component.block, instance, component_id="menu-card")

    def test_rejects_an_invalid_component_id(self):
        component, instance = self._pair()
        with self.assertRaises(ComponentError):
            override_shells(component.block, instance, component_id="Menu Card")


if __name__ == "__main__":
    unittest.main()
