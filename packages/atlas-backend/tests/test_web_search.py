"""Reading DuckDuckGo Lite's answer, against the markup it actually sends.

The fixture below is trimmed from a real response. That matters more than usual
here: the parser is a regular expression over someone else's HTML, and a fixture
invented to match the regex would prove only that the regex matches itself. The
quoting is theirs — ``class='result-link'`` in single quotes, ``rel`` before
``href`` — and both were things a first draft got wrong.

Nothing here touches the network. What the endpoint does when it is unhappy is
covered by asserting on status codes rather than by provoking it.
"""

from __future__ import annotations

import httpx
import pytest

from atlas_backend.web.search import SearchError, WebSearch

#: Trimmed from a live response to `python 3.14 release date`. Two ordinary
#: results, one wrapped in DuckDuckGo's redirector, and a snippet with an
#: HTML entity in it. Tags are wrapped across lines where the originals ran
#: long — which is not a distortion: real pages do that, and a parser that
#: only handles attributes on one line would be the bug.
LITE_HTML = """
<html><body><form>...</form><table>
<tr><td>1.&nbsp;</td><td>
  <a rel="nofollow" href="https://www.python.org/downloads/release/python-3140/"
     class='result-link'>Python Release Python 3.14.0 | Python.org</a>
</td></tr>
<tr><td></td><td class='result-snippet'>Release date: Oct. 7, 2025 &mdash;
  This is the stable release.</td></tr>
<tr><td>2.&nbsp;</td><td>
  <a rel="nofollow"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpeps.python.org%2Fpep%2D0745%2F&amp;rut=abc"
     class='result-link'>PEP 745 &ndash; Python 3.14 Release Schedule</a>
</td></tr>
<tr><td></td><td class='result-snippet'>This document describes the schedule.</td></tr>
<tr><td>3.&nbsp;</td><td>
  <a rel="nofollow" href="https://docs.python.org/3.14/whatsnew/"
     class='result-link'>What's new in Python 3.14</a>
</td></tr>
<tr><td></td>
  <td class='result-snippet'>Editors: Adam Turner and Hugo van Kemenade.</td></tr>
</table></body></html>
"""


def answering(html: str, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen = request  # type: ignore[attr-defined]
        return httpx.Response(status, html=html)

    return httpx.MockTransport(handler)


class TestParsingWhatComesBack:
    async def test_it_finds_the_results(self) -> None:
        results = await WebSearch(transport=answering(LITE_HTML)).search("python 3.14")

        assert [result.title for result in results] == [
            "Python Release Python 3.14.0 | Python.org",
            "PEP 745 – Python 3.14 Release Schedule",
            "What's new in Python 3.14",
        ]

    async def test_a_redirector_link_is_unwrapped_to_the_real_address(self) -> None:
        """A `duckduckgo.com/l/?uddg=...` link tells nobody where the answer came
        from, and where it came from is most of how a person judges it."""
        results = await WebSearch(transport=answering(LITE_HTML)).search("python")

        assert results[1].url == "https://peps.python.org/pep-0745/"
        assert results[1].host == "peps.python.org"

    async def test_entities_are_decoded_rather_than_read_aloud(self) -> None:
        results = await WebSearch(transport=answering(LITE_HTML)).search("python")

        assert "&mdash;" not in results[0].snippet
        assert "—" in results[0].snippet

    async def test_each_result_keeps_its_own_snippet(self) -> None:
        """Snippets and links are in separate table rows, so pairing them is by
        position — the one thing about this parser that could silently go wrong
        and still produce plausible output."""
        results = await WebSearch(transport=answering(LITE_HTML)).search("python")

        assert results[2].snippet.startswith("Editors: Adam Turner")

    async def test_the_limit_is_honoured(self) -> None:
        results = await WebSearch(transport=answering(LITE_HTML)).search("python", limit=2)

        assert len(results) == 2

    async def test_a_page_with_no_results_is_empty_rather_than_an_error(self) -> None:
        """Nothing found is an answer. It is the endpoint's answer to a GET as
        well, which is why the request below is a POST."""
        results = await WebSearch(transport=answering("<html><form></form></html>")).search("x")

        assert results == []


class TestHowItAsks:
    async def test_it_posts_the_query_because_a_get_returns_nothing(self) -> None:
        transport = answering(LITE_HTML)
        await WebSearch(transport=transport).search("кто такой Абай")

        request = transport.handler.seen  # type: ignore[attr-defined,union-attr]
        assert request.method == "POST"
        assert b"%D0%BA%D1%82%D0%BE" in request.content, "the query is form-encoded UTF-8"

    async def test_an_empty_query_never_reaches_the_network(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("it should not have asked")

        with pytest.raises(SearchError, match="nothing to search for"):
            await WebSearch(transport=httpx.MockTransport(explode)).search("   ")


class TestWhenItGoesWrong:
    async def test_a_refusal_is_reported_as_a_refusal(self) -> None:
        """202 is what the other DuckDuckGo endpoints answer a plain client —
        a bot challenge, not a page. Reading it as "no results" would have the
        assistant say it found nothing, which is a different and wrong claim."""
        with pytest.raises(SearchError, match="202"):
            await WebSearch(transport=answering("", status=202)).search("python")

    async def test_a_network_failure_says_so_without_quoting_anything(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(SearchError, match="could not reach the search engine"):
            await WebSearch(transport=httpx.MockTransport(fail)).search("python")
