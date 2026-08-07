"""Tests for the payload validator.

Its job is the payload that did *not* come through this project — hand-edited,
older, or assembled by something that skipped a check. So these mostly build bad
payloads by hand, which is exactly how they arise in life.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.primitives.blocks import assign_ids, new_block  # noqa: E402
from buildsmith.tools import validate as validate


def good_component():
    return {
        "doctype": "Builder Component",
        "component_id": "site-header",
        "component_name": "Site Header",
        "block": assign_ids(
            new_block("header", base_styles={"color": "var(--u1, #0a7)"}),
            seed="component:site-header",
        ),
    }


class Components(unittest.TestCase):
    def test_a_well_formed_component_passes(self):
        self.assertEqual(validate.validate_payload(good_component()), [])

    def test_a_component_with_no_blockids_is_refused(self):
        # Page shells match on blockId; a tree with none collapses every consumer.
        payload = good_component()
        payload["block"] = new_block("header")
        problems = validate.validate_payload(payload)
        self.assertTrue(any("TRAP-001" in p for p in problems))

    def test_a_single_id_less_child_is_refused(self):
        # The subtler shape: the root has an id, one child does not. That child
        # is invisible to every id-based guard — Builder mints a random id on
        # save and no page shell ever references it. This validator is the last
        # gate for payloads that skipped the primitives, so it must see it.
        payload = good_component()
        payload["block"]["children"] = [{"element": "button"}]
        problems = validate.validate_payload(payload)
        self.assertTrue(any("no blockId" in p and "TRAP-001" in p for p in problems))

    def test_a_literal_colour_is_refused(self):
        payload = good_component()
        payload["block"]["baseStyles"] = {"color": "#ff0000"}
        self.assertTrue(any("literal colour" in p for p in validate.validate_payload(payload)))

    def test_a_non_slug_component_id_is_refused(self):
        payload = good_component()
        payload["component_id"] = "Site Header"
        self.assertTrue(any("slug" in p for p in validate.validate_payload(payload)))

    def test_a_component_block_must_be_one_object(self):
        payload = good_component()
        payload["block"] = [payload["block"]]
        self.assertTrue(any("non-empty object" in p for p in validate.validate_payload(payload)))


class Pages(unittest.TestCase):
    def test_a_well_formed_page_passes(self):
        payload = {"doctype": "Builder Page", "page_title": "About", "route": "about",
                   "blocks": [new_block("main")]}
        self.assertEqual(validate.validate_payload(payload), [])

    def test_blocks_must_be_a_list(self):
        payload = {"doctype": "Builder Page", "blocks": new_block("main")}
        self.assertTrue(any("non-empty list" in p for p in validate.validate_payload(payload)))

    def test_a_template_group_without_is_template_is_refused(self):
        # Builder tests the two together, so this does nothing while looking
        # like it should.
        payload = {"doctype": "Builder Page", "blocks": [new_block("main")],
                   "template_group": "acme"}
        self.assertTrue(any("TRAP-006" in p for p in validate.validate_payload(payload)))


class TokenPlans(unittest.TestCase):
    def test_a_mint_passes(self):
        payload = {"doctype": "Builder Variable", "operations": [
            {"kind": "mint", "key": "brand", "uuid": None,
             "payload": {"type": "Color", "value": "#0a7"}}]}
        self.assertEqual(validate.validate_payload(payload), [])

    def test_a_delete_operation_is_refused(self):
        payload = {"doctype": "Builder Variable",
                   "operations": [{"kind": "delete", "key": "brand", "uuid": "u1"}]}
        self.assertTrue(any("TRAP-007" in p for p in validate.validate_payload(payload)))

    def test_an_update_without_a_uuid_is_refused(self):
        payload = {"doctype": "Builder Variable",
                   "operations": [{"kind": "set_value", "key": "brand", "uuid": None,
                                   "payload": {"value": "#0b8"}}]}
        self.assertTrue(any("needs the uuid" in p for p in validate.validate_payload(payload)))

    def test_a_mint_supplying_a_name_is_refused(self):
        # Builder assigns a uuid and upstream rewrites anything else.
        payload = {"doctype": "Builder Variable", "operations": [
            {"kind": "mint", "key": "brand",
             "payload": {"name": "brand-primary", "type": "Color", "value": "#0a7"}}]}
        self.assertTrue(any("ADR-004" in p for p in validate.validate_payload(payload)))

    def test_an_invalid_token_type_is_refused(self):
        payload = {"doctype": "Builder Variable", "operations": [
            {"kind": "mint", "key": "body", "payload": {"type": "Font", "value": "Inter"}}]}
        self.assertTrue(any("TRAP-004" in p for p in validate.validate_payload(payload)))


class UnknownPayloads(unittest.TestCase):
    def test_an_unrecognised_payload_is_refused_rather_than_guessed(self):
        problems = validate.validate_payload({"doctype": "Something Else"})
        self.assertTrue(any("Refusing to guess" in p for p in problems))

    def test_a_non_object_is_refused(self):
        self.assertTrue(validate.validate_payload(["not", "an", "object"]))


if __name__ == "__main__":
    unittest.main()
