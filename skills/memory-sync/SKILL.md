---
name: memory-sync
description: Mirror Obsidian vaults, Notion page trees, Claude conversations, and Codex session logs into the local Xenon memory store, then verify retrieval.
---

# Memory sync

Use this when the user wants long-term memory updated or asks why a fact is missing.

## Procedure

1. Pick the smallest source that answers the need:
   - Obsidian or any Markdown folder:
     `python -m xenon memory ingest --kind obsidian --root "<path>"`
   - Notion page tree (read-only integration token):
     `python -m xenon notion ingest --page "<page-id>"`
   - Claude or Codex export:
     `python -m xenon memory import --kind claude --path "<file-or-folder>"`
2. Confirm the printed `added`, `updated`, `unchanged`, and `removed` counts.
3. Verify with `python -m xenon memory search "<question>"`.
4. Report the source path shown in the results.

## Rules

- Only read paths or pages the user explicitly provided.
- Never scrape an application's private storage or session cookies.
- Re-running a sync must be idempotent; an unexpected `added` count on an unchanged
  source is a bug worth reporting.
- Deletions are intentional: `replace_missing` removes documents that left the source.
