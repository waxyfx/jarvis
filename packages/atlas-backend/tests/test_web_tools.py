"""The gate between searching and opening.

``web.read`` takes a URL, and a URL is the one argument in this catalogue that
the model composes rather than chooses from a fixed set. Asked to "check the
internal dashboard" a model will produce an address happily, and an assistant
that opens whatever it is handed is a proxy into its own network for anyone who
can get text in front of it.

So the rule is: only an address a search returned, and only for a while. The
address checks in the reader are the other half and are tested next door; this
file is about the half that cannot be expressed as "is it a public address",
because an attacker's page is on a public address too.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlas_backend.web.reader import Page, WebFetchError
from atlas_backend.web.search import SearchError, SearchResult
from atlas_backend.web.tools import WebToolError, WebTools, run_web_tool
from atlas_shared.tools.catalog import CATALOG

FOUND = [
    SearchResult(
        title="Status of Python versions",
        url="https://devguide.python.org/versions/",
        snippet="The main branch is currently the future Python 3.16.",
    ),
    SearchResult(
        title="PEP 745",
        url="https://peps.python.org/pep-0745/",
        snippet="This document describes the schedule.",
    ),
]


class FakeSearch:
    def __init__(self, results: list[SearchResult] | None = None, *, broken: bool = False) -> None:
        self._results = FOUND if results is None else results
        self._broken = broken
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        if self._broken:
            raise SearchError("the search engine answered 202")
        return self._results[:limit]


class FakeReader:
    def __init__(self, *, broken: bool = False) -> None:
        self.opened: list[str] = []
        self._broken = broken

    async def read(self, url: str) -> Page:
        self.opened.append(url)
        if self._broken:
            raise WebFetchError("that page did not answer in time")
        return Page(url=url, title="Status of Python versions", text="Python 3.16 is next.")


class Clock:
    """Time the test moves on purpose, so expiry is asserted not waited for."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def tools(**kwargs: Any) -> tuple[WebTools, FakeSearch, FakeReader, Clock]:
    search = kwargs.pop("search", None) or FakeSearch()
    reader = kwargs.pop("reader", None) or FakeReader()
    clock = Clock()
    return WebTools(search=search, reader=reader, clock=clock), search, reader, clock  # type: ignore[arg-type]


class TestSearching:
    async def test_it_reports_where_each_answer_came_from(self) -> None:
        """A claim with no source is the thing this whole layer exists to stop.
        The host is how a person decides whether to believe it."""
        web, _, _, _ = tools()

        result = await web.search_web({"query": "python 3.14 release date"})

        assert result["total"] == 2
        assert result["results"][0]["source"] == "devguide.python.org"

    async def test_nothing_found_is_an_answer_not_a_failure(self) -> None:
        web, _, _, _ = tools(search=FakeSearch([]))

        result = await web.search_web({"query": "asdkjhaskdjh"})

        assert result == {"query": "asdkjhaskdjh", "total": 0}

    async def test_a_long_snippet_is_cut_to_something_speakable(self) -> None:
        """It will be read aloud. A paragraph per result is four results the
        listener stops following."""
        long_one = [SearchResult(title="T", url="https://example.com/", snippet="word " * 200)]
        web, _, _, _ = tools(search=FakeSearch(long_one))

        result = await web.search_web({"query": "x"})

        assert len(result["results"][0]["summary"]) <= 181

    async def test_a_refused_search_is_reported_rather_than_raised(self) -> None:
        web, _, _, _ = tools(search=FakeSearch(broken=True))

        with pytest.raises(WebToolError, match="202"):
            await web.search_web({"query": "python"})


class TestOpeningOnlyWhatWasFound:
    async def test_a_result_of_a_search_can_be_opened(self) -> None:
        web, _, reader, _ = tools()
        await web.search_web({"query": "python 3.14"})

        page = await web.read_page({"url": "https://devguide.python.org/versions/"})

        assert reader.opened == ["https://devguide.python.org/versions/"]
        assert page["text"] == "Python 3.16 is next."

    async def test_an_address_no_search_returned_is_refused(self) -> None:
        """The case that matters: a plausible internal address the model
        produced from the conversation rather than from a search."""
        web, _, reader, _ = tools()
        await web.search_web({"query": "python 3.14"})

        with pytest.raises(WebToolError, match="came back from a search"):
            await web.read_page({"url": "https://intranet.company.com/payroll"})

        assert reader.opened == []

    async def test_nothing_can_be_opened_before_any_search(self) -> None:
        web, _, reader, _ = tools()

        with pytest.raises(WebToolError, match="came back from a search"):
            await web.read_page({"url": "https://devguide.python.org/versions/"})

        assert reader.opened == []

    async def test_what_a_search_offered_stops_being_openable(self) -> None:
        """Not a standing permission. An address seen once should not still be
        openable an hour into a different conversation."""
        web, _, _, clock = tools()
        await web.search_web({"query": "python"})

        clock.now += 1000.0

        with pytest.raises(WebToolError, match="came back from a search"):
            await web.read_page({"url": "https://devguide.python.org/versions/"})

    async def test_a_later_search_does_not_revoke_an_earlier_result(self) -> None:
        """Two searches in one turn is normal, and the model may open a result
        of either."""
        web, _, reader, _ = tools()
        await web.search_web({"query": "python"})
        await web.search_web({"query": "python again"})

        await web.read_page({"url": "https://peps.python.org/pep-0745/"})

        assert reader.opened == ["https://peps.python.org/pep-0745/"]

    async def test_a_page_that_will_not_load_is_reported_rather_than_raised(self) -> None:
        web, _, _, _ = tools(reader=FakeReader(broken=True))
        await web.search_web({"query": "python"})

        with pytest.raises(WebToolError, match="did not answer in time"):
            await web.read_page({"url": "https://devguide.python.org/versions/"})


class TestTheSurface:
    async def test_an_unknown_web_tool_is_refused_rather_than_crashing(self) -> None:
        web, _, _, _ = tools()

        with pytest.raises(WebToolError, match="not something I can do"):
            await run_web_tool(web, "web.post", {"url": "https://example.com/"})

    def test_every_web_tool_in_the_catalogue_has_a_handler(self) -> None:
        from atlas_backend.web.tools import WEB_TOOLS

        declared = {name for name in CATALOG.names() if name.startswith("web.")}

        assert declared == set(WEB_TOOLS)

    def test_both_run_on_the_backend(self) -> None:
        """Not on the laptop. It may be switched off, and fetching addresses
        from inside the owner's own network is the thing not to do."""
        for name in ("web.search", "web.read"):
            assert CATALOG.get(name).runs_on == "backend"

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("web.read", {"url": "file:///C:/Users/serik/.env"}),
            ("web.read", {"url": "ftp://example.com/x"}),
            ("web.read", {"url": "javascript:alert(1)"}),
            ("web.search", {"query": "x"}),
            ("web.search", {"query": "python", "url": "https://example.com/"}),
        ],
    )
    def test_bad_arguments_never_reach_a_handler(self, tool: str, args: dict[str, Any]) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CATALOG.get(tool).validate_args(args)
