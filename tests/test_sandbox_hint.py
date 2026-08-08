"""#2: sandbox up's next-step hint must name a real subcommand.

A printed next step that errors when pasted is worse than none — it teaches
people to distrust the tool's own guidance. `sandbox.up()` shells out
extensively (docker, bench), so this doesn't run it; it just pins that the
hint's own words are a valid `buildsmith check <what>` choice, so the two
can never drift apart silently again the way "trap" vs "traps" did.
"""

from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.cli import build_parser  # noqa: E402


def _check_subcommand_choices() -> list[str]:
    """The real, registered `buildsmith check <what>` choices — read off
    `build_parser()`'s actual argparse tree (cli.py is "argparse and
    nothing else", so this is cheap), not scraped from source text. A
    harmless refactor (e.g. moving the list into a shared constant) tracks
    automatically instead of breaking this test."""
    parser = build_parser()
    subparsers = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    check_parser = subparsers.choices["check"]
    what = next(a for a in check_parser._actions if a.dest == "what")
    return list(what.choices)


class SandboxUpHintTest(unittest.TestCase):
    def test_the_next_step_hint_names_a_real_check_subcommand(self):
        sandbox_source = (ROOT / "buildsmith" / "tools" / "sandbox.py").read_text()
        match = re.search(r'print\("\s*Next: buildsmith check (\w+)"\)', sandbox_source)
        self.assertIsNotNone(match, "no 'Next: buildsmith check <x>' hint found")
        hinted = match.group(1)

        choices = _check_subcommand_choices()
        self.assertIn(hinted, choices,
                      f"sandbox up's hint says 'check {hinted}', which is not one of "
                      f"cli.py's registered check choices {choices} — pasting it fails")


if __name__ == "__main__":
    unittest.main()
