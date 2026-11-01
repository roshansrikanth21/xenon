"""Agent Skills support: discovery, progressive disclosure, and on-demand load.

A skill is a folder with a ``SKILL.md`` file whose frontmatter declares ``name``
and ``description``. Startup only reads that metadata; the full instructions are
loaded when a skill is actually used. This mirrors the Agent Skills format:
https://agentskills.io/specification
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from xenon.memory import STOPWORDS

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_FILENAME = "SKILL.md"


class SkillError(ValueError):
    """Raised when a skill folder is malformed or ambiguous."""


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: Path

    @property
    def directory(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    instructions: str


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    metadata: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        metadata[key.strip()] = value.strip().strip("'\"")
    return metadata, text[end + 4 :].lstrip("\r\n")


class SkillRegistry:
    """Discovers skills under a root and loads instructions on demand."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._metadata: dict[str, SkillMetadata] = self._discover()

    def _discover(self) -> dict[str, SkillMetadata]:
        if not self.root.is_dir():
            raise SkillError(f"skills folder not found: {self.root}")

        found: dict[str, SkillMetadata] = {}
        for path in sorted(self.root.rglob(SKILL_FILENAME)):
            if not path.is_file():
                continue
            metadata, _ = _split_frontmatter(
                path.read_text(encoding="utf-8-sig", errors="replace")
            )
            name = metadata.get("name", "").strip()
            description = metadata.get("description", "").strip()
            if not name or not description:
                raise SkillError(f"{path}: name and description are required")
            if not NAME_PATTERN.match(name):
                raise SkillError(f"{path}: invalid skill name {name!r}")
            if name in found:
                raise SkillError(
                    f"{path}: duplicate skill name {name!r} (also in {found[name].path})"
                )
            found[name] = SkillMetadata(name=name, description=description, path=path)
        return found

    def catalog(self) -> list[SkillMetadata]:
        """Return metadata only, safe to put in a startup prompt."""
        return [self._metadata[name] for name in sorted(self._metadata)]

    def load(self, name: str) -> Skill:
        metadata = self._metadata.get(name)
        if metadata is None:
            raise SkillError(f"unknown skill: {name}")
        text = metadata.path.read_text(encoding="utf-8-sig", errors="replace")
        _, instructions = _split_frontmatter(text)
        return Skill(
            name=metadata.name,
            description=metadata.description,
            directory=metadata.directory,
            instructions=instructions.strip(),
        )

    def find(self, query: str, limit: int = 3) -> list[SkillMetadata]:
        """Rank skills whose name or description matches the request."""
        tokens = {
            token
            for token in re.findall(r"\w+", query.lower(), flags=re.UNICODE)
            if token not in STOPWORDS
        }
        if not tokens:
            return []

        scored: list[tuple[int, str, SkillMetadata]] = []
        for metadata in self.catalog():
            haystack = f"{metadata.name} {metadata.description}".lower()
            score = sum(1 for token in tokens if token in haystack)
            if score:
                scored.append((score, metadata.name, metadata))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [metadata for _, _, metadata in scored[:limit]]
