import tempfile
import unittest
from pathlib import Path

from xenon.skills import SkillError, SkillRegistry

SKILL = """---
name: screen-context
description: Read the active window with UI Automation before screenshots.
---

# Screen context

1. Read the foreground window.
2. Use vision only when the tree is empty.
"""

MEMORY = """---
name: memory-sync
description: Mirror an Obsidian vault or Claude export into local memory.
---

# Memory sync

Run the memory ingest command, then search the result.
"""


class SkillRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write("screen-context/SKILL.md", SKILL)
        self.write("memory-sync/SKILL.md", MEMORY)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_discovers_metadata_without_loading_instructions(self) -> None:
        registry = SkillRegistry(self.root)

        catalog = registry.catalog()

        self.assertEqual([skill.name for skill in catalog], ["memory-sync", "screen-context"])
        self.assertEqual(
            catalog[0].description,
            "Mirror an Obsidian vault or Claude export into local memory.",
        )
        self.assertFalse(hasattr(catalog[0], "instructions"))

    def test_loads_full_instructions_on_demand(self) -> None:
        registry = SkillRegistry(self.root)

        skill = registry.load("screen-context")

        self.assertIn("# Screen context", skill.instructions)
        self.assertNotIn("name:", skill.instructions)
        self.assertEqual(skill.directory, self.root / "screen-context")

    def test_finds_skills_by_question_keywords(self) -> None:
        registry = SkillRegistry(self.root)

        matches = registry.find("how do I mirror my obsidian vault?")

        self.assertEqual([skill.name for skill in matches], ["memory-sync"])

    def test_rejects_duplicate_names(self) -> None:
        self.write("other/SKILL.md", SKILL)

        with self.assertRaises(SkillError):
            SkillRegistry(self.root).catalog()

    def test_rejects_missing_metadata(self) -> None:
        self.write("broken/SKILL.md", "# no frontmatter\n")

        with self.assertRaises(SkillError):
            SkillRegistry(self.root).catalog()

    def test_rejects_invalid_names(self) -> None:
        self.write("bad/SKILL.md", "---\nname: Bad Name\ndescription: nope\n---\nbody\n")

        with self.assertRaises(SkillError):
            SkillRegistry(self.root).catalog()

    def test_ignores_folders_without_a_skill_file(self) -> None:
        self.write("notes/readme.md", "# just notes\n")

        self.assertEqual(len(SkillRegistry(self.root).catalog()), 2)

    def test_unknown_skill_raises(self) -> None:
        with self.assertRaises(SkillError):
            SkillRegistry(self.root).load("missing")


if __name__ == "__main__":
    unittest.main()
