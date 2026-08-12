"""Tests for the page/template layer.

Covers the no-exceptions rule that every site build emits a template, TRAP-006
(and its correction — the developer_mode gate needs *both* fields), TRAP-010
(static routes shadow dynamic ones), and the route collision that does not
error at render time because Builder resolves by most-recently-published.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.blocks import new_block  # noqa: E402
from buildsmith.primitives.template import (  # noqa: E402
    DOCTYPE,
    TemplateError,
    assert_template_emitted,
    check_routes,
    page,
    page_template,
    prerequisites,
    requires_developer_mode,
    side_effects,
)

BLOCKS = [new_block("div", children=[new_block("h1", inner_html="Hello")])]


def a_template(**kw):
    kw.setdefault("title", "Marketing Template")
    kw.setdefault("route", "template/marketing")
    kw.setdefault("blocks", BLOCKS)
    return page_template(**kw)


class EveryBuildEmitsATemplate(unittest.TestCase):
    def test_an_ordinary_page_requires_one(self):
        with self.assertRaises(TemplateError) as caught:
            page(title="About", route="about", blocks=BLOCKS)
        self.assertIn("no exception", str(caught.exception).lower())

    def test_passing_a_non_template_as_the_template_is_refused(self):
        real = a_template()
        ordinary = page(title="About", route="about", blocks=BLOCKS, template=real)
        with self.assertRaises(TemplateError):
            page(title="Contact", route="contact", blocks=BLOCKS, template=ordinary)

    def test_a_build_with_no_template_is_refused(self):
        real = a_template()
        pages = [page(title="About", route="about", blocks=BLOCKS, template=real)]
        with self.assertRaises(TemplateError) as caught:
            assert_template_emitted(pages)
        self.assertIn("no template", str(caught.exception))

    def test_several_templates_are_fine_when_they_share_a_group(self):
        # A template group is a set of pages, and export_template_group exports
        # every one of them — so more than one is normal, not an error.
        pages = [
            a_template(title="Landing", route="t/landing", template_group="acme"),
            a_template(title="Contact", route="t/contact", template_group="acme"),
        ]
        self.assertEqual(len(assert_template_emitted(pages)), 2)

    def test_templates_spanning_two_groups_are_refused(self):
        pages = [
            a_template(title="Landing", route="t/landing", template_group="acme"),
            a_template(title="Contact", route="t/contact", template_group="other"),
        ]
        with self.assertRaises(TemplateError) as caught:
            assert_template_emitted(pages)
        self.assertIn("different deliverables", str(caught.exception))

    def test_a_page_inherits_the_templates_project_folder(self):
        real = a_template(project_folder="buildsmith")
        ordinary = page(title="About", route="about", blocks=BLOCKS, template=real)
        self.assertEqual(ordinary.project_folder, "buildsmith")


class Trap006DeveloperModeGate(unittest.TestCase):
    def test_is_template_alone_does_not_need_developer_mode(self):
        # The guard tests is_template AND template_group together; a
        # user-saved template is deliberately left alone by both the guard and
        # the fixture sync.
        self.assertFalse(requires_developer_mode(a_template()))
        self.assertEqual(side_effects(a_template()), [])

    def test_a_shipped_group_needs_developer_mode(self):
        shipped = a_template(template_group="acme-marketing")
        self.assertTrue(requires_developer_mode(shipped))

    def test_side_effects_name_the_filesystem_writes(self):
        effects = " ".join(side_effects(a_template(template_group="acme-marketing")))
        self.assertIn("developer_mode", effects)
        self.assertIn("builder_templates/acme-marketing", effects)
        self.assertIn("builder_assets/acme-marketing", effects)
        # The export covers the whole group, not just the page being saved.
        self.assertIn("whole", effects)

    def test_template_group_must_survive_a_filesystem(self):
        for bad in ("Acme Marketing", "acme/marketing", ""):
            with self.subTest(group=bad), self.assertRaises(TemplateError):
                a_template(template_group=bad)


class Trap010Routes(unittest.TestCase):
    def test_duplicate_routes_are_refused(self):
        real = a_template()
        pages = [
            page(title="About", route="about", blocks=BLOCKS, template=real),
            page(title="About Us", route="about", blocks=BLOCKS, template=real),
        ]
        with self.assertRaises(TemplateError) as caught:
            check_routes(pages)
        self.assertIn("most-recently-published", str(caught.exception))

    def test_static_shadowing_a_dynamic_route_is_reported_not_refused(self):
        real = a_template()
        pages = [
            page(title="Hello", route="posts/hello", blocks=BLOCKS, template=real),
            page(
                title="Post",
                route="posts/:slug",
                blocks=BLOCKS,
                template=real,
                dynamic_route=True,
            ),
        ]
        notes = check_routes(pages)
        self.assertEqual(len(notes), 1)
        self.assertIn("TRAP-010", notes[0])

    def test_unrelated_routes_produce_no_notes(self):
        real = a_template()
        pages = [
            page(title="About", route="about", blocks=BLOCKS, template=real),
            page(
                title="Post",
                route="posts/:slug",
                blocks=BLOCKS,
                template=real,
                dynamic_route=True,
            ),
        ]
        self.assertEqual(check_routes(pages), [])

    def test_a_dynamic_route_may_carry_placeholder_segments(self):
        real = a_template()
        p = page(
            title="Post", route="posts/:slug", blocks=BLOCKS, template=real, dynamic_route=True
        )
        self.assertEqual(p.route, "posts/:slug")

    def test_a_malformed_route_is_refused(self):
        real = a_template()
        for bad in ("About Us", "About", "posts//x "):
            with self.subTest(route=bad), self.assertRaises(TemplateError):
                page(title="X", route=bad, blocks=BLOCKS, template=real)


class PayloadShape(unittest.TestCase):
    def test_blocks_must_be_a_list_not_a_single_block(self):
        # Builder Component.block is one dict; Builder Page.blocks is a list.
        with self.assertRaises(TemplateError) as caught:
            a_template(blocks=new_block("div"))
        self.assertIn("list of root blocks", str(caught.exception))

    def test_empty_blocks_are_refused(self):
        with self.assertRaises(TemplateError):
            a_template(blocks=[])

    def test_record_shape(self):
        record = a_template(template_group="acme").record()
        self.assertEqual(record["doctype"], DOCTYPE)
        self.assertEqual(record["is_template"], 1)
        self.assertEqual(record["template_group"], "acme")
        self.assertEqual(record["published"], 0)

    def test_an_ordinary_page_carries_no_template_fields(self):
        record = page(
            title="About", route="about", blocks=BLOCKS, template=a_template()
        ).record()
        self.assertNotIn("is_template", record)
        self.assertNotIn("template_group", record)

    def test_blocks_are_validated_and_copied(self):
        source = [new_block("div")]
        result = a_template(blocks=source)
        self.assertIsNot(result.blocks[0], source[0])

    def test_page_data_script_is_carried_when_set(self):
        script = 'data.items = frappe.db.get_all("Menu Item", fields=["item_name"])'
        record = page(
            title="Menu", route="menu", blocks=BLOCKS, template=a_template(),
            page_data_script=script,
        ).record()
        self.assertEqual(record["page_data_script"], script)

    def test_page_data_script_is_absent_when_unset(self):
        # Matches every other optional field here (favicon, head_html): an
        # empty value is omitted rather than sent as "", so applying this
        # payload never overwrites something already live with nothing.
        record = page(
            title="About", route="about", blocks=BLOCKS, template=a_template()
        ).record()
        self.assertNotIn("page_data_script", record)

    def test_page_data_script_survives_update_payload(self):
        script = 'data.items = frappe.db.get_all("Menu Item", fields=["item_name"])'
        built = page(
            title="Menu", route="menu", blocks=BLOCKS, template=a_template(),
            page_data_script=script, name="page-abc12345",
        )
        self.assertEqual(built.update_payload()["page_data_script"], script)


if __name__ == "__main__":
    unittest.main()


class Trap014EmptyRoutes(unittest.TestCase):
    """An empty route is not "the home page" — Builder rewrites it to a hash.

    Found by browsing a replicated site and getting the desk login where the
    homepage should have been. `set_default_values()` turns an empty route into
    `pages/<name>`, so the page lands at an unpredictable URL and `/` is still
    served by Website Settings.home_page, which nobody set.
    """

    def test_an_empty_route_is_refused(self):
        for empty in ("", "   ", "/"):
            with self.subTest(route=empty), self.assertRaises(TemplateError) as caught:
                a_template(route=empty)
            self.assertIn("TRAP-014", str(caught.exception))

    def test_the_error_names_the_actual_fix(self):
        with self.assertRaises(TemplateError) as caught:
            page(title="Home", route="", blocks=BLOCKS, template=a_template())
        message = str(caught.exception)
        self.assertIn("home_page", message)
        self.assertIn("'home'", message)


class Prerequisites(unittest.TestCase):
    """Things the target site needs, which validating payloads cannot catch."""

    def test_a_project_folder_is_required_when_referenced(self):
        pages = [a_template(project_folder="buildsmith")]
        needed = " ".join(prerequisites(pages))
        self.assertIn("Builder Project Folder", needed)
        self.assertIn("LinkValidationError", needed)

    def test_a_home_page_requires_the_website_setting(self):
        real = a_template()
        pages = [real, page(title="Home", route="home", blocks=BLOCKS, template=real)]
        self.assertTrue(any("home_page" in p for p in prerequisites(pages)))

    def test_a_build_with_neither_needs_nothing(self):
        self.assertEqual(prerequisites([a_template()]), [])
