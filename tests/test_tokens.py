"""Tests for the token layer.

Covers TRAP-004 (only Color/Dimension), TRAP-007 (never delete, rename in
place), TRAP-011 (the doctype name lives in one constant), and the silent-failure
modes found while reading the pinned Builder: an empty value emits no CSS
variable at all, and composing against a stale applied map bakes yesterday's
colours in as fallbacks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.tokens import (  # noqa: E402
    DOCTYPE,
    Applied,
    Manifest,
    Token,
    TokenError,
    assert_tokenisable,
    plan,
    validate_styles,
)


def manifest(**tokens) -> Manifest:
    return Manifest.from_dict({"tokens": tokens})


def applied(**entries) -> Applied:
    return Applied.from_dict({"tokens": entries})


class Trap004OnlyColorOrDimension(unittest.TestCase):
    def test_a_third_type_is_refused(self):
        with self.assertRaises(TokenError) as caught:
            Token(key="body", value="Inter", type="Font")
        self.assertIn("TRAP-004", str(caught.exception))

    def test_the_two_valid_types_pass(self):
        Token(key="brand", value="#0a7", type="Color")
        Token(key="gutter", value="24px", type="Dimension")

    def test_untokenisable_properties_name_the_alternative(self):
        for prop in ("fontFamily", "fontWeight", "lineHeight", "boxShadow"):
            with self.subTest(prop=prop), self.assertRaises(TokenError) as caught:
                assert_tokenisable(prop)
            self.assertIn("component prop", str(caught.exception))

    def test_a_token_reference_in_an_untokenisable_property_is_caught(self):
        with self.assertRaises(TokenError):
            validate_styles({"fontWeight": "var(--abc, 600)"})

    def test_a_literal_in_an_untokenisable_property_is_fine(self):
        # The trap is believing it can be a token, not using the property.
        validate_styles({"fontWeight": "600", "fontFamily": "Inter, sans-serif"})


class SilentFailureGuards(unittest.TestCase):
    def test_empty_value_is_refused(self):
        # get_css_variables() skips falsy values: no CSS variable is emitted and
        # every reference falls back to its literal, so the page looks close.
        for bad in ("", "   "):
            with self.subTest(value=bad), self.assertRaises(TokenError):
                Token(key="brand", value=bad)

    def test_precomposed_light_dark_is_refused(self):
        # Builder composes light-dark() from value + dark_value itself.
        with self.assertRaises(TokenError) as caught:
            Token(key="bg", value="light-dark(#fff, #000)")
        self.assertIn("light-dark", str(caught.exception))

    def test_identical_dark_value_is_dropped_from_the_record(self):
        record = Token(key="bg", value="#fff", dark_value="#fff").record()
        self.assertNotIn("dark_value", record)

    def test_differing_dark_value_is_kept(self):
        record = Token(key="bg", value="#fff", dark_value="#111").record()
        self.assertEqual(record["dark_value"], "#111")

    def test_record_never_supplies_a_name(self):
        # Builder assigns a uuid; supplying a readable name gets it rewritten by
        # upstream's refactor_builder_variables patch on the next migrate.
        self.assertNotIn("name", Token(key="brand", value="#0a7").record())

    def test_the_doctype_lives_in_one_constant(self):
        # TRAP-011: renamed to Builder Token after the pin. One line to migrate.
        self.assertEqual(Token(key="brand", value="#0a7").record()["doctype"], DOCTYPE)


class ManifestValidation(unittest.TestCase):
    def test_duplicate_variable_name_is_refused(self):
        # variable_name is the only link from a live record back to a logical
        # key, so duplicates make read-back ambiguous.
        with self.assertRaises(TokenError) as caught:
            manifest(
                brand={"value": "#0a7", "label": "Primary"},
                accent={"value": "#f50", "label": "Primary"},
            )
        self.assertIn("read-back ambiguous", str(caught.exception))

    def test_unknown_field_is_refused(self):
        with self.assertRaises(TokenError):
            manifest(brand={"value": "#0a7", "colour": "#0a7"})

    def test_bare_mapping_without_tokens_wrapper_is_accepted(self):
        m = Manifest.from_dict({"_meta": {"site": "example"}, "brand": {"value": "#0a7"}})
        self.assertEqual(len(m), 1)
        self.assertEqual(m.meta["site"], "example")


class Trap007NeverDelete(unittest.TestCase):
    def test_orphans_are_reported_but_never_deleted(self):
        p = plan(
            manifest(brand={"value": "#0a7"}),
            applied(
                brand={"uuid": "u1", "value": "#0a7"},
                retired={"uuid": "u2", "value": "#ccc"},
            ),
        )
        self.assertEqual([o["key"] for o in p.orphans], ["retired"])
        # The orphan produces no operation of any kind — deletion is not merely
        # absent from the plan, it is not an operation this module can express.
        self.assertEqual(p.operations, [])
        kinds = {op["kind"] for op in p.to_dict()["operations"]}
        self.assertNotIn("delete", kinds)

    def test_orphans_survive_into_the_emitted_payload(self):
        p = plan(manifest(), applied(retired={"uuid": "u2", "value": "#ccc"}))
        self.assertEqual(p.to_dict()["orphans"][0]["uuid"], "u2")

    def test_a_rename_keeps_the_uuid(self):
        p = plan(
            manifest(brand={"value": "#0a7", "label": "Brand Primary"}),
            applied(brand={"uuid": "u1", "value": "#0a7", "variable_name": "Old Name"}),
        )
        self.assertEqual(len(p), 1)
        op = p.operations[0]
        self.assertEqual((op.kind, op.uuid), ("rename", "u1"))
        self.assertEqual(op.payload, {"variable_name": "Brand Primary"})


class PlanDiffing(unittest.TestCase):
    def test_missing_token_is_minted(self):
        p = plan(manifest(brand={"value": "#0a7"}), applied())
        self.assertEqual(p.operations[0].kind, "mint")
        self.assertIsNone(p.operations[0].uuid)

    def test_matching_state_produces_nothing(self):
        p = plan(
            manifest(brand={"value": "#0a7", "label": "brand"}),
            applied(brand={"uuid": "u1", "value": "#0a7", "variable_name": "brand"}),
        )
        self.assertFalse(p)
        self.assertIn("nothing to do", p.summary())

    def test_value_change_is_an_update_not_a_recreate(self):
        p = plan(
            manifest(brand={"value": "#0b8"}),
            applied(brand={"uuid": "u1", "value": "#0a7"}),
        )
        self.assertEqual([op.kind for op in p.operations], ["set_value"])
        self.assertEqual(p.operations[0].uuid, "u1")

    def test_type_conflicts_are_collected_not_raised_one_at_a_time(self):
        with self.assertRaises(TokenError) as caught:
            plan(
                manifest(
                    a={"value": "1px", "type": "Dimension"},
                    b={"value": "2px", "type": "Dimension"},
                ),
                applied(
                    a={"uuid": "u1", "value": "1px", "type": "Color"},
                    b={"uuid": "u2", "value": "2px", "type": "Color"},
                ),
            )
        message = str(caught.exception)
        self.assertIn("a:", message)
        self.assertIn("b:", message)

    def test_plan_writes_a_payload_file(self):
        p = plan(manifest(brand={"value": "#0a7"}), applied())
        with tempfile.TemporaryDirectory() as d:
            out = p.write(Path(d) / "plan.json")
            written = json.loads(out.read_text())
        self.assertEqual(written["doctype"], DOCTYPE)
        self.assertEqual(written["operations"][0]["kind"], "mint")


class References(unittest.TestCase):
    def test_ref_carries_a_literal_fallback(self):
        a = applied(brand={"uuid": "u1", "value": "#0a7"})
        self.assertEqual(a.ref("brand"), "var(--u1, #0a7)")

    def test_a_missing_token_raises_rather_than_inventing_a_uuid(self):
        with self.assertRaises(TokenError) as caught:
            applied().ref("brand")
        self.assertIn("read back from the site", str(caught.exception))

    def test_composing_against_a_stale_map_is_refused(self):
        # The failure this prevents: the plan is not applied yet, so every
        # reference would embed the old colour as its fallback.
        m = manifest(brand={"value": "#0b8"})
        a = applied(brand={"uuid": "u1", "value": "#0a7"})
        with self.assertRaises(TokenError) as caught:
            a.assert_in_sync(m)
        self.assertIn("stale literals", str(caught.exception))

    def test_in_sync_map_composes_cleanly(self):
        m = manifest(brand={"value": "#0a7", "label": "brand"})
        a = applied(brand={"uuid": "u1", "value": "#0a7", "variable_name": "brand"})
        a.assert_in_sync(m)
        self.assertEqual(a.ref("brand"), "var(--u1, #0a7)")


if __name__ == "__main__":
    unittest.main()
