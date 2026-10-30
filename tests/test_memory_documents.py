import tempfile
import unittest
from pathlib import Path

from xenon.memory import MemoryDocument, MemoryStore


class DocumentUpsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_indexes_connector_documents_with_source_attribution(self) -> None:
        result = self.store.upsert_documents(
            "notion",
            "notion://workspace",
            [
                MemoryDocument(
                    external_id="notion://page/abc",
                    title="Memory design",
                    body="Durable notes live in SQLite with full-text search.",
                    tags=("design", "memory"),
                )
            ],
        )

        self.assertEqual(result.added, 1)
        hit = self.store.search("durable")[0]
        self.assertEqual(hit.title, "Memory design")
        self.assertEqual(hit.path, "notion://page/abc")
        self.assertEqual(hit.source_kind, "notion")
        self.assertEqual(hit.source_root, "notion://workspace")
        self.assertEqual(hit.tags, ("design", "memory"))

    def test_reupserting_identical_documents_changes_nothing(self) -> None:
        documents = [MemoryDocument("page-1", "Note", "unchanged body")]

        first = self.store.upsert_documents("notion", "root", documents)
        second = self.store.upsert_documents("notion", "root", documents)

        self.assertEqual(first.added, 1)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.updated, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(self.store.search("unchanged")), 1)

    def test_edited_document_replaces_indexed_text(self) -> None:
        self.store.upsert_documents(
            "notion", "root", [MemoryDocument("page-1", "Note", "first wording")]
        )

        result = self.store.upsert_documents(
            "notion", "root", [MemoryDocument("page-1", "Note", "second wording")]
        )

        self.assertEqual(result.updated, 1)
        self.assertEqual(self.store.search("wording")[0].snippet.count("second"), 1)
        self.assertEqual(self.store.search("first"), [])

    def test_replace_missing_removes_departed_pages(self) -> None:
        self.store.upsert_documents(
            "notion",
            "root",
            [
                MemoryDocument("page-1", "Keep", "keep this page"),
                MemoryDocument("page-2", "Drop", "drop that page"),
            ],
        )

        result = self.store.upsert_documents(
            "notion",
            "root",
            [MemoryDocument("page-1", "Keep", "keep this page")],
            replace_missing=True,
        )

        self.assertEqual(result.removed, 1)
        self.assertEqual(self.store.search("drop"), [])
        self.assertEqual([hit.title for hit in self.store.search("keep")], ["Keep"])

    def test_delete_source_removes_connector_documents(self) -> None:
        result = self.store.upsert_documents(
            "notion", "root", [MemoryDocument("page-1", "Note", "external body")]
        )

        self.assertEqual(self.store.delete_source(result.source_id), 1)
        self.assertEqual(self.store.search("external"), [])

    def test_upsert_requires_a_kind_and_root(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upsert_documents("", "root", [MemoryDocument("a", "b", "c")])


if __name__ == "__main__":
    unittest.main()
