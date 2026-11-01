"""Import user-provided Claude/Codex exports into local memory.

Only paths the user points at are read. Nothing is scraped from an application's
private storage, and nothing leaves the machine. Supported inputs:

- a directory of Markdown/text notes plus optional ``conversations.json``/``.jsonl``
- a Claude-style ``conversations.json`` array
- a JSONL session log of ``{"role", "content"}`` records
"""
from __future__ import annotations

import json
from pathlib import Path

from xenon.memory import (
    HIDDEN_PREFIX,
    MARKDOWN_SUFFIXES,
    IngestResult,
    MemoryDocument,
    MemoryStore,
    parse_markdown,
)

TEXT_SUFFIXES = MARKDOWN_SUFFIXES | {".txt"}
CONVERSATION_FILES = {"conversations.json"}


class ExportFormatError(ValueError):
    """Raised when an export file is not in a supported shape."""


def ingest_export(store: MemoryStore, kind: str, path: str | Path) -> IngestResult:
    """Index an export directory or single export file under ``kind``."""
    if not kind:
        raise ValueError("kind is required")

    export_path = Path(path).expanduser()
    if not export_path.exists():
        raise FileNotFoundError(export_path)

    if export_path.is_dir():
        documents = _directory_documents(export_path, kind)
        return store.upsert_documents(
            kind, str(export_path.resolve()), documents, replace_missing=True
        )

    documents = _file_documents(export_path, kind)
    return store.upsert_documents(
        kind, str(export_path.resolve().parent), documents, replace_missing=False
    )


def _directory_documents(root: Path, kind: str) -> list[MemoryDocument]:
    documents: list[MemoryDocument] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(HIDDEN_PREFIX) for part in relative.parts):
            continue

        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            parsed = parse_markdown(text, path.stem)
            documents.append(
                MemoryDocument(
                    external_id=str(path),
                    title=parsed.title,
                    body=parsed.body,
                    tags=parsed.tags,
                    mtime=path.stat().st_mtime,
                )
            )
        elif path.name.lower() in CONVERSATION_FILES:
            documents.extend(_conversation_documents(path.read_text(encoding="utf-8-sig"), kind, path))
        elif suffix == ".jsonl":
            documents.append(_jsonl_document(path, kind))
    return documents


def _file_documents(path: Path, kind: str) -> list[MemoryDocument]:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        parsed = parse_markdown(text, path.stem)
        return [
            MemoryDocument(
                external_id=str(path.resolve()),
                title=parsed.title,
                body=parsed.body,
                tags=parsed.tags,
                mtime=path.stat().st_mtime,
            )
        ]
    if suffix == ".json" or path.name.lower() in CONVERSATION_FILES:
        return _conversation_documents(path.read_text(encoding="utf-8-sig"), kind, path)
    if suffix == ".jsonl":
        return [_jsonl_document(path, kind)]
    raise ExportFormatError(f"unsupported export format: {path.name}")


def _conversation_documents(raw: str, kind: str, origin: Path) -> list[MemoryDocument]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ExportFormatError(f"{origin.name}: not valid JSON ({error})") from error

    if isinstance(payload, dict):
        payload = payload.get("conversations")
    if not isinstance(payload, list):
        raise ExportFormatError(f"{origin.name}: expected a list of conversations")

    documents: list[MemoryDocument] = []
    for index, conversation in enumerate(payload):
        if not isinstance(conversation, dict):
            continue
        identifier = conversation.get("uuid") or conversation.get("id") or f"index-{index}"
        title = conversation.get("name") or conversation.get("title") or f"Conversation {index + 1}"
        messages = conversation.get("chat_messages") or conversation.get("messages") or []
        body = _transcript(messages)
        documents.append(
            MemoryDocument(
                external_id=f"{kind}://conversation/{identifier}",
                title=str(title),
                body=body,
            )
        )
    return documents


def _transcript(messages: list) -> str:
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender") or message.get("role") or "unknown"
        text = message.get("text")
        if text is None:
            text = _content_text(message.get("content"))
        if text and str(text).strip():
            lines.append(f"{sender}: {str(text).strip()}")
    return "\n\n".join(lines)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    if isinstance(content, dict):
        return str(content.get("text", ""))
    return ""


def _jsonl_document(path: Path, kind: str) -> MemoryDocument:
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        role = record.get("role") or record.get("type") or "unknown"
        text = record.get("content")
        if text is None:
            text = record.get("text")
        text = _content_text(text)
        if text.strip():
            lines.append(f"{role}: {text.strip()}")

    return MemoryDocument(
        external_id=str(path.resolve()),
        title=path.stem,
        body="\n\n".join(lines),
        mtime=path.stat().st_mtime,
    )
