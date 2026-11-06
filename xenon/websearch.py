"""Keyless web search through DuckDuckGo's lite HTML endpoint.

Used by the ``search`` command and by the voice fast path. The parser is kept
separate from the fetch so it can be tested against a saved page.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

DEFAULT_TIMEOUT_S = 15.0
ENDPOINT = "https://lite.duckduckgo.com/lite/?q="
USER_AGENT = "Xenon/1.0 (local assistant)"

Fetcher = Callable[[str, float], str]


class SearchUnavailable(RuntimeError):
    """Raised when the search page could not be fetched."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def _fetch(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def search(
    query: str,
    limit: int = 5,
    timeout: float = DEFAULT_TIMEOUT_S,
    fetch: Fetcher | None = None,
) -> list[SearchResult]:
    """Return up to ``limit`` results, or raise ``SearchUnavailable``."""
    query = query.strip()
    if not query:
        raise ValueError("search needs a query")
    url = ENDPOINT + urllib.parse.quote(query)
    try:
        page = (fetch or _fetch)(url, timeout)
    except Exception as error:
        raise SearchUnavailable(f"search failed: {error}") from error
    return parse_results(page, limit=limit)


def parse_results(page: str, limit: int = 5) -> list[SearchResult]:
    """Parse DuckDuckGo lite's simple table markup without a DOM library."""
    link_tags = re.findall(
        r"<a\b[^>]*class=['\"]result-link['\"][^>]*>.*?</a>",
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    snippet_matches = re.findall(
        r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )

    results: list[SearchResult] = []
    for index, tag in enumerate(link_tags[:limit]):
        href_match = re.search(r"href=['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
        title_match = re.search(r">(.*)</a>", tag, flags=re.IGNORECASE | re.DOTALL)
        href = href_match.group(1) if href_match else ""
        title = _text(title_match.group(1) if title_match else "")
        snippet = ""
        if index < len(snippet_matches):
            snippet = _text(snippet_matches[index])
        results.append(
            SearchResult(title=title, url=_unwrap_redirect(href), snippet=snippet)
        )
    return results


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _unwrap_redirect(href: str) -> str:
    """DuckDuckGo wraps links as ``//duckduckgo.com/l/?uddg=<target>``."""
    if not href:
        return href
    parsed = urllib.parse.urlparse(href)
    params = urllib.parse.parse_qs(parsed.query)
    if "uddg" in params:
        return params["uddg"][0]
    return href
