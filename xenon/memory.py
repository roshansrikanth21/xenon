"""Local long-term memory backed by SQLite full-text search.

The store is the source of truth: Markdown files are mirrored into it, not
moved or copied elsewhere. Every document keeps its source root, relative path,
content hash, and timestamp so answers can cite where a fact came from and a
single file can be forgotten without touching the rest.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

MARKDOWN_SUFFIXES = {".md", ".markdown"}
HIDDEN_PREFIX = "."
STOPWORDS = frozenset(
    """
    a about after all am an and any are as at be been before but by can could did
    do does for from had has have he her here him his how i if in into is it its
    me my of on or our out she should so that the their them then there these they
    this to was we were what when where which who why will with would you your
    """.split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, root)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, path)
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    document_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);
"""


@dataclass(frozen=True)
class MemoryHit:
    document_id: str
    title: str
    path: str
    snippet: str
    tags: tuple[str, ...]
    source_id: str
    source_kind: str
    source_root: str
    score: float


@dataclass(frozen=True)
class IngestResult:
    source_id: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0


@dataclass(frozen=True)
class ParsedMarkdown:
    title: str
    body: str
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MemoryDocument:
    """A connector record ready to be indexed.

    ``external_id`` is the stable identity inside the source: a file path for
    vaults, a page URI for Notion, a conversation id for chat exports.
    """

    external_id: str
    title: str
    body: str
    tags: tuple[str, ...] = ()
    mtime: float = 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _document_hash(document: "MemoryDocument") -> str:
    payload = "\x1f".join(
        [document.title, document.body, ",".join(document.tags)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_markdown(text: str, fallback_title: str) -> ParsedMarkdown:
    """Extract a title and tags from Markdown, ignoring YAML frontmatter."""
    body = text
    tags: list[str] = []
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            front = text[3:end]
            body = text[end + 4 :].lstrip("\r\n")
            tags = _parse_tags(front)

    title = fallback_title
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip() or fallback_title
            break

    return ParsedMarkdown(title=title, body=body, tags=tuple(tags))


def _parse_tags(frontmatter: str) -> list[str]:
    tags: list[str] = []
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("tags:"):
            raw = line.split(":", 1)[1].strip()
            if raw:
                if raw.startswith("[") and raw.endswith("]"):
                    raw = raw[1:-1]
                tags.extend(
                    value.strip().strip("'\"")
                    for value in raw.split(",")
                    if value.strip()
                )
            else:
                index += 1
                while index < len(lines) and lines[index].strip().startswith("- "):
                    tags.append(lines[index].strip()[2:].strip().strip("'\""))
                    index += 1
                continue
        index += 1
    return [tag for tag in tags if tag]


def _query_expression(query: str) -> tuple[str, str]:
    """Return ``(and_expression, or_expression)`` for a natural query.

    Questions are not keyword lists, so an all-terms match is tried first and
    a ranked any-terms match is the fallback. Stopwords are dropped so a long
    question still finds the note that answers it.
    """
    tokens = [
        token
        for token in re.findall(r"\w+", query, flags=re.UNICODE)
        if token.lower() not in STOPWORDS
    ]
    if not tokens:
        return "", ""
    quoted = [f'"{token}"*' for token in tokens]
    return " ".join(quoted), " OR ".join(quoted)


class MemoryStore:
    """Durable memory with full-text retrieval and per-source deletion."""

    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self._clock = clock if clock is not None else _utc_now

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def ingest_markdown(self, kind: str, root: str | Path) -> IngestResult:
        """Mirror a Markdown tree into memory, keeping unchanged files intact."""
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(root_path)

        documents: list[MemoryDocument] = []
        for file_path in sorted(root_path.rglob("*")):
            if not file_path.is_file() or file_path.suffix.lower() not in MARKDOWN_SUFFIXES:
                continue
            relative = file_path.relative_to(root_path)
            if any(part.startswith(HIDDEN_PREFIX) for part in relative.parts):
                continue
            text = file_path.read_text(encoding="utf-8-sig", errors="replace")
            parsed = parse_markdown(text, file_path.stem)
            documents.append(
                MemoryDocument(
                    external_id=str(file_path),
                    title=parsed.title,
                    body=parsed.body,
                    tags=parsed.tags,
                    mtime=file_path.stat().st_mtime,
                )
            )

        return self.upsert_documents(
            kind, str(root_path), documents, replace_missing=True
        )

    def upsert_documents(
        self,
        kind: str,
        root: str,
        documents: Iterable[MemoryDocument],
        replace_missing: bool = False,
    ) -> IngestResult:
        """Index connector documents, skipping records whose text is unchanged."""
        if not kind or not root:
            raise ValueError("kind and root are required")
        source_id = self._ensure_source(kind, root)
        return self._apply(source_id, list(documents), replace_missing)

    def _apply(
        self,
        source_id: str,
        documents: list[MemoryDocument],
        replace_missing: bool,
    ) -> IngestResult:
        existing = {
            row["path"]: row
            for row in self._connection.execute(
                "SELECT id, path, content_hash FROM documents WHERE source_id = ?",
                (source_id,),
            )
        }

        added = updated = unchanged = 0
        seen: set[str] = set()

        with self._connection:
            for document in documents:
                stored_path = document.external_id
                seen.add(stored_path)
                digest = _document_hash(document)
                previous = existing.get(stored_path)

                if previous is not None and previous["content_hash"] == digest:
                    unchanged += 1
                    continue

                if previous is None:
                    document_id = self._document_id(source_id, stored_path)
                    self._connection.execute(
                        "INSERT INTO documents"
                        " (id, source_id, path, title, body, tags, content_hash, file_mtime, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            document_id,
                            source_id,
                            stored_path,
                            document.title,
                            document.body,
                            ",".join(document.tags),
                            digest,
                            document.mtime,
                            self._clock(),
                        ),
                    )
                    added += 1
                else:
                    document_id = previous["id"]
                    self._connection.execute(
                        "UPDATE documents SET title = ?, body = ?, tags = ?, content_hash = ?,"
                        " file_mtime = ?, updated_at = ? WHERE id = ?",
                        (
                            document.title,
                            document.body,
                            ",".join(document.tags),
                            digest,
                            document.mtime,
                            self._clock(),
                            document_id,
                        ),
                    )
                    updated += 1

                self._index(document_id, document)

            removed = 0
            if replace_missing:
                for stored_path, row in existing.items():
                    if stored_path in seen:
                        continue
                    self._unindex(row["id"])
                    self._connection.execute(
                        "DELETE FROM documents WHERE id = ?", (row["id"],)
                    )
                    removed += 1

        return IngestResult(
            source_id=source_id,
            added=added,
            updated=updated,
            unchanged=unchanged,
            removed=removed,
        )

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        and_expression, or_expression = _query_expression(query)
        if not and_expression:
            return []

        rows = self._search_rows(and_expression, limit)
        if not rows and or_expression != and_expression:
            rows = self._search_rows(or_expression, limit)
        return [
            MemoryHit(
                document_id=row["id"],
                title=row["title"],
                path=row["path"],
                snippet=row["snippet"],
                tags=tuple(tag for tag in row["tags"].split(",") if tag),
                source_id=row["source_id"],
                source_kind=row["kind"],
                source_root=row["root"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    def _search_rows(self, expression: str, limit: int) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT d.id, d.title, d.path, d.tags, d.source_id,
                   s.kind, s.root,
                   snippet(documents_fts, 2, '[', ']', '…', 12) AS snippet,
                   bm25(documents_fts) AS score
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.document_id
            JOIN sources s ON s.id = d.source_id
            WHERE documents_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()

    def delete_document(self, document_id: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM documents WHERE id = ?", (document_id,)
        )
        self._unindex(document_id)
        self._connection.commit()
        return cursor.rowcount > 0

    def delete_source(self, source_id: str) -> int:
        document_ids = [
            row["id"]
            for row in self._connection.execute(
                "SELECT id FROM documents WHERE source_id = ?", (source_id,)
            )
        ]
        with self._connection:
            for document_id in document_ids:
                self._connection.execute(
                    "DELETE FROM documents WHERE id = ?", (document_id,)
                )
            self._connection.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        for document_id in document_ids:
            self._unindex(document_id)
        self._connection.commit()
        return len(document_ids)

    def _ensure_source(self, kind: str, root: str) -> str:
        source_id = hashlib.sha1(f"{kind}:{root}".encode("utf-8")).hexdigest()[:12]
        self._connection.execute(
            "INSERT OR IGNORE INTO sources (id, kind, root, created_at) VALUES (?, ?, ?, ?)",
            (source_id, kind, root, self._clock()),
        )
        self._connection.commit()
        return source_id

    def _index(self, document_id: str, document: MemoryDocument) -> None:
        self._unindex(document_id)
        self._connection.execute(
            "INSERT INTO documents_fts (document_id, title, body, tags) VALUES (?, ?, ?, ?)",
            (document_id, document.title, document.body, ",".join(document.tags)),
        )

    def _unindex(self, document_id: str) -> None:
        self._connection.execute(
            "DELETE FROM documents_fts WHERE document_id = ?", (document_id,)
        )

    @staticmethod
    def _document_id(source_id: str, path: str) -> str:
        return hashlib.sha1(f"{source_id}:{path}".encode("utf-8")).hexdigest()[:16]
