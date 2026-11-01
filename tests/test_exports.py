import json
import tempfile
import unittest
from pathlib import Path

from xenon.exports import ingest_export
from xenon.memory import MemoryStore

CONVERSATIONS = [
    {
        "uuid": "c1",
        "name": "Screen assistant",
        "created_at": "2026-10-01T10:00:00Z",
        "chat_messages": [
            {"sender": "human", "text": "Use UI Automation first."},
            {"sender": "assistant", "text": "Agreed, keep vision as a fallback."},
        ],
    },
    {
        "uuid": "c2",
        "name": "Memory design",
        "chat_messages": [{"sender": "human", "text": "Store notes in SQLite."}],
    },
]


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = MemoryStore(self.base / "memory.db")
        self.export = self.base / "claude-export"
        self.export.mkdir()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def write_json(self, name: str, payload: object) -> Path:
        path = self.export / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_imports_claude_conversations_with_citations(self) -> None:
        self.write_json("conversations.json", CONVERSATIONS)

        result = ingest_export(self.store, "claude", self.export)

        self.assertEqual(result.added, 2)
        hit = self.store.search("automation")[0]
        self.assertEqual(hit.title, "Screen assistant")
        self.assertEqual(hit.path, "claude://conversation/c1")
        self.assertEqual(hit.source_kind, "claude")
        fallback = self.store.search("fallback")[0]
        self.assertIn("assistant: Agreed", fallback.snippet)

    def test_reimporting_unchanged_export_changes_nothing(self) -> None:
        self.write_json("conversations.json", CONVERSATIONS)
        ingest_export(self.store, "claude", self.export)

        result = ingest_export(self.store, "claude", self.export)

        self.assertEqual(result.added, 0)
        self.assertEqual(result.unchanged, 2)
        self.assertEqual(len(self.store.search("sqlite")), 1)

    def test_imports_markdown_notes_alongside_conversations(self) -> None:
        (self.export / "notes").mkdir()
        (self.export / "notes" / "decision.md").write_text(
            "# Vision fallback\n\nOnly capture screenshots when UIA is weak.\n",
            encoding="utf-8",
        )
        self.write_json("conversations.json", CONVERSATIONS)

        result = ingest_export(self.store, "claude", self.export)

        self.assertEqual(result.added, 3)
        self.assertEqual(self.store.search("screenshots")[0].title, "Vision fallback")

    def test_directory_import_removes_files_that_left_the_export(self) -> None:
        note = self.export / "temporary.md"
        note.write_text("# Temporary\n\nremove me later\n", encoding="utf-8")
        ingest_export(self.store, "claude", self.export)
        note.unlink()

        result = ingest_export(self.store, "claude", self.export)

        self.assertEqual(result.removed, 1)
        self.assertEqual(self.store.search("temporary"), [])

    def test_imports_jsonl_sessions(self) -> None:
        session = self.export / "session.jsonl"
        session.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "wire up the wake word"}),
                    json.dumps({"role": "assistant", "content": "use a local VAD"}),
                ]
            ),
            encoding="utf-8",
        )

        result = ingest_export(self.store, "codex", session)

        self.assertEqual(result.added, 1)
        hit = self.store.search("VAD")[0]
        self.assertEqual(hit.source_kind, "codex")
        self.assertEqual(hit.title, "session")

    def test_rejects_unknown_formats(self) -> None:
        path = self.export / "image.png"
        path.write_bytes(b"\x89PNG")

        with self.assertRaises(ValueError):
            ingest_export(self.store, "claude", path)

    def test_importing_a_missing_path_fails_clearly(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ingest_export(self.store, "claude", self.base / "nope")


if __name__ == "__main__":
    unittest.main()
