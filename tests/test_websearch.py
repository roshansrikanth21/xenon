import unittest

from xenon import websearch

PAGE = """
<table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc"
       class="result-link">Example &amp; Co</a></td></tr>
<tr><td class="result-snippet">A <b>short</b> snippet.</td></tr>
<tr><td><a href="https://docs.python.org/3/" class="result-link">Python docs</a></td></tr>
<tr><td class="result-snippet">The official tutorial.</td></tr>
</table>
"""


class ParseTests(unittest.TestCase):
    def test_parses_titles_snippets_and_unwraps_redirects(self) -> None:
        results = websearch.parse_results(PAGE)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Example & Co")
        self.assertEqual(results[0].url, "https://example.com/a")
        self.assertEqual(results[0].snippet, "A short snippet.")
        self.assertEqual(results[1].url, "https://docs.python.org/3/")

    def test_honours_the_limit(self) -> None:
        self.assertEqual(len(websearch.parse_results(PAGE, limit=1)), 1)

    def test_returns_an_empty_list_for_a_page_without_results(self) -> None:
        self.assertEqual(websearch.parse_results("<html></html>"), [])


class SearchTests(unittest.TestCase):
    def test_quotes_the_query_and_returns_results(self) -> None:
        seen: list[tuple[str, float]] = []

        def fetch(url: str, timeout: float) -> str:
            seen.append((url, timeout))
            return PAGE

        results = websearch.search("python asyncio", fetch=fetch)

        self.assertEqual(len(results), 2)
        self.assertEqual(
            seen[0][0],
            "https://lite.duckduckgo.com/lite/?q=python%20asyncio",
        )
        self.assertEqual(seen[0][1], websearch.DEFAULT_TIMEOUT_S)

    def test_wraps_fetch_errors(self) -> None:
        def fetch(url: str, timeout: float) -> str:
            raise TimeoutError("slow")

        with self.assertRaises(websearch.SearchUnavailable) as raised:
            websearch.search("anything", fetch=fetch)

        self.assertIn("slow", str(raised.exception))

    def test_rejects_an_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            websearch.search("   ")


if __name__ == "__main__":
    unittest.main()
