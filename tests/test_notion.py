import tempfile
import unittest
from pathlib import Path

from xenon.memory import MemoryStore
from xenon.notion import NotionClient, NotionConfig, sync_to_memory

CONFIG = NotionConfig(token="secret-token")

ROOT_PAGE = {
    "object": "page",
    "id": "root",
    "url": "https://notion.so/root",
    "last_edited_time": "2026-10-01T10:00:00.000Z",
    "properties": {
        "Name": {
            "id": "title",
            "type": "title",
            "title": [{"plain_text": "Projects"}],
        },
        "Tags": {
            "id": "tags",
            "type": "multi_select",
            "multi_select": [{"name": "work"}, {"name": "xenon"}],
        },
    },
}

ROOT_BLOCKS = {
    "results": [
        {
            "id": "b1",
            "type": "paragraph",
            "has_children": False,
            "paragraph": {"rich_text": [{"plain_text": "Xenon keeps memory local."}]},
        },
        {
            "id": "b2",
            "type": "heading_2",
            "has_children": False,
            "heading_2": {"rich_text": [{"plain_text": "Reading"}]},
        },
        {
            "id": "child-1",
            "type": "child_page",
            "has_children": False,
            "child_page": {"title": "Memory notes"},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

CHILD_PAGE = {
    "object": "page",
    "id": "child-1",
    "url": "https://notion.so/child-1",
    "last_edited_time": "2026-10-02T10:00:00.000Z",
    "properties": {
        "Name": {"id": "title", "type": "title", "title": [{"plain_text": "Memory notes"}]}
    },
}

CHILD_BLOCKS = {
    "results": [
        {
            "id": "c1",
            "type": "bulleted_list_item",
            "has_children": False,
            "bulleted_list_item": {"rich_text": [{"plain_text": "Use SQLite"}]},
        },
        {
            "id": "c2",
            "type": "to_do",
            "has_children": False,
            "to_do": {"rich_text": [{"plain_text": "Add Notion sync"}], "checked": True},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}


class FakeNotion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.pages = {"root": ROOT_PAGE, "child-1": CHILD_PAGE}
        self.children = {"root": ROOT_BLOCKS, "child-1": CHILD_BLOCKS}
        self.paginate: dict[str, list[dict]] = {}

    def __call__(self, method: str, url: str, headers: dict, body, timeout: float) -> dict:
        self.calls.append((method, url, headers))
        page_id = url.split("/pages/")[-1] if "/pages/" in url else None
        if page_id is not None:
            return self.pages[page_id]
        block_id = url.split("/blocks/")[-1].split("/children")[0]
        if block_id in self.paginate:
            pages = self.paginate[block_id]
            if "start_cursor" in url:
                return pages[1]
            return pages[0]
        return self.children[block_id]


class NotionClientTests(unittest.TestCase):
    def test_reads_page_tree_with_titles_tags_and_markdown(self) -> None:
        transport = FakeNotion()
        client = NotionClient(CONFIG, transport=transport)

        pages = client.fetch_pages("root")

        self.assertEqual([page.title for page in pages], ["Projects", "Memory notes"])
        self.assertIn("Xenon keeps memory local.", pages[0].markdown)
        self.assertIn("## Reading", pages[0].markdown)
        self.assertNotIn("Use SQLite", pages[0].markdown)
        self.assertIn("- Use SQLite", pages[1].markdown)
        self.assertIn("- [x] Add Notion sync", pages[1].markdown)
        self.assertEqual(pages[0].tags, ("work", "xenon"))
        self.assertEqual(pages[1].tags, ())
        self.assertEqual(pages[0].url, "https://notion.so/root")

    def test_sends_auth_and_version_headers(self) -> None:
        transport = FakeNotion()

        NotionClient(CONFIG, transport=transport).fetch_pages("root")

        _, _, headers = transport.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertIn("Notion-Version", headers)

    def test_follows_block_pagination(self) -> None:
        transport = FakeNotion()
        first = {
            "results": [
                {
                    "id": "p1",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "first page"}]},
                }
            ],
            "has_more": True,
            "next_cursor": "cursor-2",
        }
        second = {
            "results": [
                {
                    "id": "p2",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "second page"}]},
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }
        transport.paginate["root"] = [first, second]

        pages = NotionClient(CONFIG, transport=transport).fetch_pages("root")

        self.assertIn("first page", pages[0].markdown)
        self.assertIn("second page", pages[0].markdown)
        self.assertTrue(any("start_cursor=cursor-2" in call[1] for call in transport.calls))

    def test_skips_trashed_pages(self) -> None:
        transport = FakeNotion()
        transport.pages["child-1"] = {**CHILD_PAGE, "archived": True}

        pages = NotionClient(CONFIG, transport=transport).fetch_pages("root")

        self.assertEqual([page.page_id for page in pages], ["root"])


class NotionSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_sync_write_pages_into_memory_with_citations(self) -> None:
        client = NotionClient(CONFIG, transport=FakeNotion())

        result = sync_to_memory(client, self.store, "root")

        self.assertEqual(result.added, 2)
        hit = self.store.search("sqlite")[0]
        self.assertEqual(hit.title, "Memory notes")
        self.assertEqual(hit.path, "https://notion.so/child-1")
        self.assertEqual(hit.source_kind, "notion")

    def test_resync_is_idempotent(self) -> None:
        client = NotionClient(CONFIG, transport=FakeNotion())
        sync_to_memory(client, self.store, "root")

        result = sync_to_memory(client, self.store, "root")

        self.assertEqual(result.added, 0)
        self.assertEqual(result.unchanged, 2)
        self.assertEqual(len(self.store.search("sqlite")), 1)


if __name__ == "__main__":
    unittest.main()
