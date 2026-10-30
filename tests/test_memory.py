import tempfile
import time
import unittest
from pathlib import Path

from xenon.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.store = MemoryStore(self.base / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_ingests_markdown_with_source_attribution_and_tags(self) -> None:
        self.write(
            "projects/xenon.md",
            "---\ntags: [assistant, windows]\n---\n# Xenon plan\n\nUse UI Automation first.\n",
        )

        result = self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(result.added, 1)
        self.assertEqual(result.unchanged, 0)
        hits = self.store.search("automation")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.title, "Xenon plan")
        self.assertTrue(hit.path.endswith("xenon.md"))
        self.assertEqual(hit.source_kind, "obsidian")
        self.assertEqual(hit.source_root, str(self.vault.resolve()))
        self.assertEqual(hit.tags, ("assistant", "windows"))
        self.assertIn("Automation", hit.snippet)

    def test_reingesting_unchanged_files_creates_no_duplicates(self) -> None:
        self.write("note.md", "# Note\n\ndurable content\n")

        first = self.store.ingest_markdown("obsidian", self.vault)
        second = self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(first.added, 1)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(self.store.search("durable")), 1)

    def test_updated_file_replaces_old_content(self) -> None:
        path = self.write("note.md", "# Note\n\nold wording\n")
        self.store.ingest_markdown("obsidian", self.vault)
        time.sleep(0.01)
        path.write_text("# Note\n\nnew wording\n", encoding="utf-8")

        result = self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(result.updated, 1)
        self.assertEqual(self.store.search("wording")[0].title, "Note")
        self.assertEqual(len(self.store.search("old")), 0)
        self.assertGreaterEqual(len(self.store.search("new")), 1)

    def test_removed_file_disappears_from_memory(self) -> None:
        path = self.write("note.md", "# Note\n\nephemeral fact\n")
        self.store.ingest_markdown("obsidian", self.vault)
        path.unlink()

        result = self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(result.removed, 1)
        self.assertEqual(self.store.search("ephemeral"), [])

    def test_search_returns_only_relevant_documents(self) -> None:
        self.write("a.md", "# Alpha\n\nsourdough starter schedule\n")
        self.write("b.md", "# Beta\n\nkubernetes ingress notes\n")
        self.store.ingest_markdown("obsidian", self.vault)

        hits = self.store.search("sourdough")

        self.assertEqual([hit.title for hit in hits], ["Alpha"])

    def test_skips_hidden_and_trash_directories(self) -> None:
        self.write(".obsidian/workspace.md", "# private workspace state\n")
        self.write(".trash/old.md", "# deleted thought\n")
        self.write("kept.md", "# Kept\n\nvisible note\n")

        result = self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(result.added, 1)
        self.assertEqual(self.store.search("workspace"), [])
        self.assertEqual(self.store.search("deleted"), [])

    def test_parses_block_style_tags(self) -> None:
        self.write("note.md", "---\ntags:\n  - alpha\n  - beta\n---\n# Tagged\n\nbody\n")

        self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(self.store.search("tagged")[0].tags, ("alpha", "beta"))

    def test_delete_source_removes_derived_memories(self) -> None:
        self.write("note.md", "# Note\n\nremovable fact\n")
        result = self.store.ingest_markdown("obsidian", self.vault)

        removed = self.store.delete_source(result.source_id)

        self.assertEqual(removed, 1)
        self.assertEqual(self.store.search("removable"), [])

    def test_delete_document_removes_one_memory(self) -> None:
        self.write("a.md", "# Alpha\n\nshared topic alpha\n")
        self.write("b.md", "# Beta\n\nshared topic beta\n")
        self.store.ingest_markdown("obsidian", self.vault)
        target = self.store.search("topic")[0]
        other = self.store.search("topic")[1]

        self.assertTrue(self.store.delete_document(target.document_id))

        remaining = self.store.search("topic")
        self.assertEqual([hit.document_id for hit in remaining], [other.document_id])

    def test_strips_utf8_bom_from_files_written_by_windows_editors(self) -> None:
        path = self.vault / "bom.md"
        path.write_bytes("\ufeff# BOM note\n\nbom content\n".encode("utf-8"))
        self.store.ingest_markdown("obsidian", self.vault)

        hit = self.store.search("bom")[0]

        self.assertEqual(hit.title, "BOM note")
        self.assertNotIn("\ufeff", hit.snippet)

    def test_empty_query_returns_no_results(self) -> None:
        self.write("note.md", "# Note\n\nbody\n")
        self.store.ingest_markdown("obsidian", self.vault)

        self.assertEqual(self.store.search("   "), [])


if __name__ == "__main__":
    unittest.main()
