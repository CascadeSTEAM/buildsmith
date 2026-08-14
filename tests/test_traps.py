"""Tests asserting our tooling cannot commit a known trap.

Every ★ entry in `docs/traps.md` that our emitters can prevent gets a case here.
A trap without a test is a trap we will hit again.

These are pure-python and need nothing installed and nothing running — they test
*our* refusal to emit a bad payload. Whether Builder still fails the way the
ledger records is a different question, answered by `sandbox/trap-check.py`
against the pinned commit. Both matter: this file stops us shipping the mistake,
that one stops the ledger going stale.

    python3 -m unittest discover -s tests -v      # or: pytest tests/
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.blocks import (  # noqa: E402
    BlockError,
    assert_ids_preserved,
    assign_ids,
    block_ids,
    new_block,
    restyle,
    validate,
)
from buildsmith.primitives.repeater import (  # noqa: E402
    RepeaterError,
    binding,
    repeater,
    validate_repeater,
)


class Trap001ComponentShellCollapse(unittest.TestCase):
    """Replacing a component's block must not drop blockIds."""

    def setUp(self):
        self.existing = assign_ids(
            new_block(
                "div",
                children=[
                    new_block("header", children=[new_block("nav", inner_html="links")]),
                    new_block("footer", inner_html="address"),
                ],
            ),
            seed="component:site-shell",
        )

    def test_freshly_composed_tree_is_refused(self):
        # The near-miss on record: compose a new tree, write it over the old one.
        # New ids, so no page's shell matches, so every interior collapses.
        fresh = assign_ids(
            new_block("div", children=[new_block("header"), new_block("footer")]),
            seed="a-different-seed",
        )
        with self.assertRaises(BlockError) as caught:
            assert_ids_preserved(self.existing, fresh)
        self.assertIn("TRAP-001", str(caught.exception))

    def test_restyling_in_place_is_allowed(self):
        after = restyle(self.existing, base={"backgroundColor": "var(--x, #fff)"})
        assert_ids_preserved(self.existing, after)  # must not raise

    def test_growing_the_tree_is_allowed(self):
        after = assign_ids(dict(self.existing), seed="component:site-shell")
        after["children"] = list(after["children"]) + [new_block("aside", block_id="new")]
        assert_ids_preserved(self.existing, after)  # gaining ids is fine

    def test_ids_are_deterministic_across_runs(self):
        # Random ids would make every re-emit look like a full rewrite, and would
        # break shell matching on every publish. Same tree, same seed, same ids.
        again = assign_ids(
            new_block(
                "div",
                children=[
                    new_block("header", children=[new_block("nav", inner_html="links")]),
                    new_block("footer", inner_html="address"),
                ],
            ),
            seed="component:site-shell",
        )
        self.assertEqual(block_ids(self.existing), block_ids(again))

    def test_ids_read_back_from_a_site_survive(self):
        # An id that came off a live site is authoritative. Re-running assign_ids
        # must not renumber it.
        tree = new_block("div", block_id="from-the-live-site", children=[new_block("p")])
        assign_ids(tree, seed="whatever")
        self.assertEqual(tree["blockId"], "from-the-live-site")


class Trap003Repeaters(unittest.TestCase):
    """Six ways a repeater fails silently; none of them survivable here."""

    def child(self, **kw):
        return new_block("p", inner_html="row", **kw)

    def test_rule1_all_three_keys_required(self):
        for missing in ("isRepeaterBlock", "children", "dataKey"):
            with self.subTest(missing=missing):
                block = repeater(data_key="items", child=self.child())
                if missing == "children":
                    block["children"] = []
                else:
                    del block[missing]
                with self.assertRaises(RepeaterError) as caught:
                    validate_repeater(block)
                self.assertIn("rule 1", str(caught.exception))

    def test_rule1_missing_data_key_refused_at_construction(self):
        with self.assertRaises(RepeaterError):
            repeater(data_key="", child=self.child())

    def test_rule2_only_one_child(self):
        with self.assertRaises(RepeaterError) as caught:
            repeater(data_key="items", child=[self.child(), self.child()])
        self.assertIn("rule 2", str(caught.exception))

        block = repeater(data_key="items", child=self.child())
        block["children"].append(self.child())
        with self.assertRaises(RepeaterError) as caught:
            validate_repeater(block)
        self.assertIn("rule 2", str(caught.exception))

    def test_rule3_property_on_the_container_is_a_mixup(self):
        with self.assertRaises(RepeaterError) as caught:
            repeater(
                data_key={"key": "items", "property": "innerHTML"},
                child=self.child(),
            )
        self.assertIn("rule 3", str(caught.exception))

    def test_rule4_attribute_bindings_must_say_so(self):
        with self.assertRaises(RepeaterError) as caught:
            binding("photo", "src", type="key")
        self.assertIn("rule 4", str(caught.exception))

        # ...and it is inferred when unspecified, so the easy path is correct.
        self.assertEqual(binding("photo", "src")["type"], "attribute")
        self.assertEqual(binding("title", "innerHTML")["type"], "key")

        hand_rolled = repeater(data_key="items", child=self.child())
        hand_rolled["children"][0]["dynamicValues"] = [
            {"key": "photo", "property": "src", "type": "key"}
        ]
        with self.assertRaises(RepeaterError) as caught:
            validate_repeater(hand_rolled)
        self.assertIn("rule 4", str(caught.exception))

    def test_rule5_no_duplicate_binding(self):
        block = repeater(data_key="items", child=self.child())
        block["children"][0]["dataKey"] = {"key": "t", "property": "innerHTML", "type": "key"}
        block["children"][0]["dynamicValues"] = [
            {"key": "t", "property": "innerHTML", "type": "key"}
        ]
        with self.assertRaises(RepeaterError) as caught:
            validate_repeater(block)
        self.assertIn("rule 5", str(caught.exception))

    def test_rule6_visibility_condition_on_immediate_child(self):
        with self.assertRaises(RepeaterError) as caught:
            repeater(
                data_key="items",
                child=self.child(visibility_condition={"key": "show"}),
            )
        self.assertIn("rule 6", str(caught.exception))

    def test_a_correct_repeater_is_accepted(self):
        # Without this the suite would pass just as happily if repeater() raised
        # on everything.
        block = repeater(
            data_key="posts",
            child=new_block(
                "div",
                children=[
                    new_block("img", dynamic_values=[binding("image", "src")]),
                    new_block("h2", dynamic_values=[binding("title", "innerHTML")]),
                ],
            ),
        )
        validate_repeater(block)
        validate(block)
        self.assertTrue(block["isRepeaterBlock"])
        self.assertEqual(block["dataKey"]["key"], "posts")
        self.assertEqual(len(block["children"]), 1)


class UnknownKeysAreRefused(unittest.TestCase):
    """The renderer ignores keys it does not know. We must not."""

    def test_a_typo_is_an_error_not_a_silence(self):
        block = new_block("div")
        block["baseStyle"] = {"color": "red"}  # missing the 's'
        with self.assertRaises(BlockError) as caught:
            validate(block)
        self.assertIn("baseStyle", str(caught.exception))

    def test_known_keys_pass(self):
        validate(
            new_block(
                "div",
                classes=["x"],
                base_styles={"color": "red"},
                children=[new_block("span", inner_html="hi")],
            )
        )

    def test_validation_reaches_nested_repeaters(self):
        bad = repeater(data_key="items", child=new_block("p"))
        bad["children"].append(new_block("p"))  # rule 2, two levels down
        tree = new_block("section", children=[new_block("div", children=[bad])])
        with self.assertRaises(RepeaterError):
            validate(tree)

    def test_a_read_back_override_shell_is_valid(self):
        """`extend_block()` matches page shells on referenceBlockId (TRAP-001,
        docs/traps.md quotes the pinned source), so every tree read back from
        a live site carries it. A validate() that refuses what the renderer
        reads would make read-back — the mandated input to revise() — invalid."""
        shell = {
            "blockId": "a1b2c3d4",
            "referenceBlockId": "e5f6a7b8",
            "extendedFromComponent": "site-header",
        }
        validate(shell)

    def test_block_name_is_a_known_key(self):
        """blockName is an editor-authored outline label (e.g. "Navbar",
        "Footer", "Hero") — the renderer ignores it, but real pages read back
        from a live site carry it, same as referenceBlockId above. Authoring
        never writes it (new_block has no parameter for it), so this is
        built as a plain dict, matching a read-back shape."""
        block = new_block("div")
        block["blockName"] = "Hero"
        validate(block)

    def test_a_shell_descendant_needs_no_element(self):
        """Real Builder output pairs isChildOfComponent with referenceBlockId
        on every non-root node of an override shell — content resolves from
        the referenced component at render time, so the node itself carries
        neither `element` nor `extendedFromComponent`."""
        descendant = {
            "blockId": "c9d8e7f6",
            "referenceBlockId": "b1a2c3d4",
            "isChildOfComponent": "site-header",
        }
        validate(descendant)


if __name__ == "__main__":
    unittest.main()
