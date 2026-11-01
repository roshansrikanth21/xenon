"""Read-only Notion connector.

Walks a root page's block tree and mirrors each page into local memory with its
Notion URL as citation. Requires an integration token with read access to the
pages the user explicitly shared; nothing is written back to Notion.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from xenon.memory import IngestResult, MemoryDocument, MemoryStore

DEFAULT_BASE_URL = "https://api.notion.com/v1"
DEFAULT_VERSION = "2022-06-28"
DEFAULT_TIMEOUT_S = 30.0
MAX_DEPTH = 4

Transport = Callable[[str, str, dict, bytes | None, float], dict]


class NotionUnavailable(RuntimeError):
    """Raised when the Notion API cannot be reached or returns invalid data."""


@dataclass(frozen=True)
class NotionConfig:
    token: str
    root_page_id: str = ""
    base_url: str = DEFAULT_BASE_URL
    version: str = DEFAULT_VERSION
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    title: str
    url: str
    markdown: str
    last_edited_time: str = ""
    tags: tuple[str, ...] = ()


def config_from_env() -> NotionConfig | None:
    token = os.environ.get("XENON_NOTION_TOKEN", "")
    root = os.environ.get("XENON_NOTION_ROOT_PAGE", "")
    if not token or not root:
        return None
    try:
        timeout = float(os.environ.get("XENON_NOTION_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return NotionConfig(
        token=token,
        root_page_id=root,
        base_url=os.environ.get("XENON_NOTION_BASE_URL", DEFAULT_BASE_URL),
        timeout_s=timeout,
    )


def _urllib_transport(
    method: str, url: str, headers: dict, body: bytes | None, timeout: float
) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _plain(rich_text: list[dict]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


def _block_markdown(block: dict) -> str:
    kind = block.get("type", "")
    data = block.get(kind, {}) or {}
    text = _plain(data.get("rich_text", []))

    if kind == "paragraph":
        return text
    if kind == "heading_1":
        return f"# {text}"
    if kind == "heading_2":
        return f"## {text}"
    if kind == "heading_3":
        return f"### {text}"
    if kind == "bulleted_list_item":
        return f"- {text}"
    if kind == "numbered_list_item":
        return f"1. {text}"
    if kind == "to_do":
        mark = "x" if data.get("checked") else " "
        return f"- [{mark}] {text}"
    if kind == "quote":
        return f"> {text}"
    if kind == "code":
        language = data.get("language", "")
        return f"```{language}\n{text}\n```"
    if kind == "divider":
        return "---"
    if kind == "callout":
        return text
    if kind == "toggle":
        return text
    return text


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            title = _plain(prop.get("title", []))
            if title:
                return title
    return "Untitled"


def _page_tags(page: dict) -> tuple[str, ...]:
    for name, prop in page.get("properties", {}).items():
        if name.lower() != "tags":
            continue
        if prop.get("type") == "multi_select":
            return tuple(
                option["name"]
                for option in prop.get("multi_select", [])
                if option.get("name")
            )
        if prop.get("type") == "select" and prop.get("select"):
            return (prop["select"]["name"],)
    return ()


class NotionClient:
    """Fetches a page tree through the Notion API, read-only."""

    def __init__(
        self,
        config: NotionConfig,
        transport: Transport | None = None,
        max_depth: int = MAX_DEPTH,
    ) -> None:
        self._config = config
        self._transport = transport if transport is not None else _urllib_transport
        self._max_depth = max_depth

    def fetch_pages(self, root_page_id: str) -> list[NotionPage]:
        pages: list[NotionPage] = []
        self._visit(root_page_id, 0, pages, set())
        return pages

    def _visit(
        self, page_id: str, depth: int, out: list[NotionPage], visited: set[str]
    ) -> None:
        if page_id in visited or depth > self._max_depth:
            return
        visited.add(page_id)

        page = self._request("GET", f"/pages/{page_id}")
        if page.get("archived") or page.get("in_trash"):
            return

        markdown: list[str] = []
        child_pages: list[str] = []
        for block in self._children(page_id):
            if block.get("archived") or block.get("in_trash"):
                continue
            if block.get("type") == "child_page":
                child_pages.append(block["id"])
                continue
            rendered = _block_markdown(block)
            if rendered:
                markdown.append(rendered)

        out.append(
            NotionPage(
                page_id=page_id,
                title=_page_title(page),
                url=page.get("url", ""),
                markdown="\n".join(markdown),
                last_edited_time=page.get("last_edited_time", ""),
                tags=_page_tags(page),
            )
        )

        for child in child_pages:
            self._visit(child, depth + 1, out, visited)

    def _children(self, page_id: str) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={urllib.parse.quote(cursor)}"
            payload = self._request("GET", path)
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return results

    def _request(self, method: str, path: str) -> dict:
        url = self._config.base_url.rstrip("/") + path
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Notion-Version": self._config.version,
            "Content-Type": "application/json",
        }
        try:
            return self._transport(
                method, url, headers, None, self._config.timeout_s
            )
        except Exception as error:
            raise NotionUnavailable(str(error)) from error


def sync_to_memory(
    client: NotionClient, store: MemoryStore, root_page_id: str
) -> IngestResult:
    """Mirror the page tree into memory, removing pages that left the tree."""
    pages = client.fetch_pages(root_page_id)
    documents = [
        MemoryDocument(
            external_id=page.url or f"notion://page/{page.page_id}",
            title=page.title,
            body=page.markdown,
            tags=page.tags,
        )
        for page in pages
    ]
    return store.upsert_documents(
        "notion",
        f"notion://page/{root_page_id}",
        documents,
        replace_missing=True,
    )
