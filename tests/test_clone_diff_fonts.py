"""Font-stack reduction must be classified, never hidden and never noise.

Builder stores `font-family` as a single family name — its editor cannot parse a
stack. So importing a site that used a stack necessarily drops the fallback
chain, and every rule containing a font then differs from the source by that one
declaration. On one real page that was 338 differences.

Two failure modes, pulling in opposite directions:

- Report them as omissions and they bury every real finding. That is BS-007, and
  a detector nobody reads protects nothing.
- Normalise them away silently and a genuine regression disappears with them —
  in the *maintain* scenario a font change IS a defect, and a converter bug that
  kept the wrong family would look exactly like the intended reduction.

So a bundle matching only after reduction is counted and reported as a
transformation. Everything else stays a finding. These tests hold that line.
"""

from __future__ import annotations

import unittest

from buildsmith.tools.clone_diff import Comparison, _reduce_font_stacks


def reduce(*declarations: str) -> set[str]:
    return set(_reduce_font_stacks(frozenset(declarations)))


class ReduceFontStacksTest(unittest.TestCase):
    def test_css_escaped_stack_is_reduced(self) -> None:
        """Builder's generated CSS escapes the spaces as `\\ `.

        Without undoing that first, the split leaves a trailing backslash and
        nothing ever matches — the classification would silently never fire.
        """
        self.assertEqual(
            reduce("font-family: Merriline,\\ ui-sans-serif,\\ system-ui,\\ sans-serif"),
            {"font-family: Merriline"},
        )

    def test_quotes_are_stripped(self) -> None:
        self.assertEqual(reduce("font-family: 'Open Sans', serif"), {"font-family: Open Sans"})
        self.assertEqual(reduce('font-family: "Open Sans", serif'), {"font-family: Open Sans"})

    def test_single_family_is_unchanged(self) -> None:
        self.assertEqual(reduce("font-family: Inter"), {"font-family: Inter"})

    def test_other_declarations_are_untouched(self) -> None:
        self.assertEqual(
            reduce("color: red", "margin: 0"), {"color: red", "margin: 0"}
        )

    def test_a_different_first_family_is_not_absorbed(self) -> None:
        """The whole safety property.

        If the converter kept the *second* family instead of the first, the
        result would differ from the source in exactly the same shape as the
        intended reduction. It must not be waved through.
        """
        self.assertNotEqual(
            _reduce_font_stacks(frozenset({"font-family: Skybald,\\ Merriline,\\ cursive"})),
            _reduce_font_stacks(frozenset({"font-family: Merriline"})),
        )

    def test_a_dropped_font_is_not_absorbed(self) -> None:
        """A rule that lost its font entirely is a real omission."""
        self.assertNotEqual(
            _reduce_font_stacks(frozenset({"font-family: Merriline", "color: red"})),
            _reduce_font_stacks(frozenset({"color: red"})),
        )

    def test_a_changed_neighbour_declaration_is_not_absorbed(self) -> None:
        """Reduction must not make unrelated differences vanish with it."""
        self.assertNotEqual(
            _reduce_font_stacks(frozenset({"font-family: Merriline, serif", "color: red"})),
            _reduce_font_stacks(frozenset({"font-family: Merriline", "color: blue"})),
        )


class ReductionIsNotAPassTest(unittest.TestCase):
    def test_reduced_count_does_not_make_a_comparison_ok(self) -> None:
        """`ok` must reflect real omissions only — but a reduction still shows."""
        clean = Comparison()
        self.assertTrue(clean.ok)

        transformed = Comparison(font_stacks_reduced=338)
        self.assertTrue(transformed.ok, "a reduction is not an omission")

        broken = Comparison(font_stacks_reduced=338, missing_text=["Tea & Toast"])
        self.assertFalse(broken.ok, "a real omission alongside reductions must still fail")


if __name__ == "__main__":
    unittest.main()
