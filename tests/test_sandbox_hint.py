"""#2: sandbox up's next-step hint must name a real subcommand.

A printed next step that errors when pasted is worse than none — it teaches
people to distrust the tool's own guidance. `sandbox.up()` shells out
extensively (docker, bench), so this doesn't run it; it just pins that the
hint's own words are a valid `buildsmith check <what>` choice, so the two
can never drift apart silently again the way "trap" vs "traps" did.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SandboxUpHintTest(unittest.TestCase):
    def test_the_next_step_hint_names_a_real_check_subcommand(self):
        sandbox_source = (ROOT / "buildsmith" / "tools" / "sandbox.py").read_text()
        match = re.search(r'print\("\s*Next: buildsmith check (\w+)"\)', sandbox_source)
        self.assertIsNotNone(match, "no 'Next: buildsmith check <x>' hint found")
        hinted = match.group(1)

        cli_source = (ROOT / "buildsmith" / "cli.py").read_text()
        choices_match = re.search(
            r'add\("check".*?choices=\[([^\]]+)\]', cli_source, re.S
        )
        self.assertIsNotNone(choices_match, "check subcommand's choices not found in cli.py")
        choices = [c.strip().strip('"') for c in choices_match.group(1).split(",")]

        self.assertIn(hinted, choices,
                      f"sandbox up's hint says 'check {hinted}', which is not one of "
                      f"cli.py's registered check choices {choices} — pasting it fails")


if __name__ == "__main__":
    unittest.main()
