"""Every skill is registered in AGENTS.md.

A skill nobody can find is a skill nobody loads, and `builder-safety` in
particular only works if it is discovered *before* the write rather than after.
The same discipline the operations project applies to its own skills.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"


def skill_names() -> set[str]:
    names = set()
    for path in SKILLS.glob("*/SKILL.md"):
        match = re.search(r"^name:\s*(\S+)", path.read_text(), re.M)
        assert match, f"{path} has no `name:` in its frontmatter"
        names.add(match.group(1))
    return names


class SkillsAreRegistered(unittest.TestCase):
    def test_there_are_skills(self):
        self.assertTrue(skill_names(), "no skills found — did the directory move?")

    def test_every_skill_appears_in_agents_md(self):
        agents = (ROOT / "AGENTS.md").read_text()
        missing = sorted(name for name in skill_names() if f"`{name}`" not in agents)
        self.assertEqual(
            missing, [], f"skill(s) not registered in AGENTS.md: {missing}"
        )

    def test_every_skill_has_a_description(self):
        # The description is what the harness matches a request against, so a
        # skill without one is a skill that never fires.
        pattern = re.compile(r"^description:\s*\S+", re.M)
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                self.assertRegex(path.read_text(), pattern)

    def test_the_directory_name_matches_the_declared_name(self):
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            declared = re.search(r"^name:\s*(\S+)", path.read_text(), re.M).group(1)
            self.assertEqual(declared, path.parent.name)

    def test_each_workflow_skill_points_at_builder_safety(self):
        # The one that must be loaded first is easy to skip if nothing says so.
        for name in ("builder-theme", "builder-replicate", "builder-golive"):
            with self.subTest(skill=name):
                self.assertIn("builder-safety", (SKILLS / name / "SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()


class SchemaReferenceMatchesThePin(unittest.TestCase):
    """The generated schema must describe the Builder we actually target."""

    def _schemagen(self):

        from buildsmith.tools import schemagen as module
        return module

    def test_the_recorded_commit_matches_sandbox_pins(self):
        # Moving the pin without regenerating leaves a schema reference for a
        # Builder this project no longer targets. Cheap check, no sandbox.
        schemagen = self._schemagen()
        self.assertEqual(
            schemagen.recorded_ref(ROOT / "docs" / "builder-schema.md"),
            schemagen.pinned_ref(),
        )

    def test_it_documents_the_doctypes_the_primitives_depend_on(self):
        text = (ROOT / "docs" / "builder-schema.md").read_text()
        for doctype in ("Builder Page", "Builder Component", "Builder Variable"):
            with self.subTest(doctype=doctype):
                self.assertIn(f"## {doctype}", text)
