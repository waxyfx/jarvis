"""Running a web tool, once policy has allowed it.

Two tools, and the split matters. ``web.search`` takes words the owner said and
returns a handful of titles with their sources — cheap, and usually enough to
answer "what is the current version of X" or "did that happen yet". ``web.read``
opens one of those pages, and is the only place a URL the model produced becomes
a connection.

That is why ``web.read`` will only open a URL the search step returned. A model
asked to "check the internal dashboard" will happily produce an address; letting
it name any address turns the assistant into a proxy for whatever is reachable
from the backend. Search results are not a strong provenance — anyone can rank —
but they are *somewhere the owner's question led*, and combined with the
address checks in the reader that is the line worth drawing.

Results are compacted for the same reason the tracker's are: whatever ends up in
a result is what the model has read and may recite, and a voice assistant that
recites three pages has stopped answering.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from atlas_backend.logging import get_logger
from atlas_backend.web.reader import PageReader, WebFetchError
from atlas_backend.web.search import SearchError, SearchResult, WebSearch

__all__ = ["WEB_TOOLS", "WebToolError", "WebTools", "run_web_tool"]

log = get_logger(__name__)

#: How long a search result stays openable. Long enough for the model to search,
#: be told the headlines, and be asked to open one; short enough that it is not
#: a standing permission. Measured from when the search answered.
_OPENABLE_FOR_S = 900.0

#: Results per search. Five is what a person scans before picking one, and ten
#: snippets is already more text than the answer that follows them.
_RESULTS = 5


class WebToolError(RuntimeError):
    """Something the owner should be told, rather than something to crash on.

    Every failure out here — no results, a refused address, a page that will
    not load — is ordinary and expected, and the assistant should be able to
    say it in the same breath as the rest of the turn.
    """


@dataclass
class _Openable:
    """URLs a search has returned, and when. See the module docstring.

    Deliberately not unbounded and not permanent: an address stays openable for
    :data:`_OPENABLE_FOR_S` and then is not, so a URL seen once cannot be opened
    an hour into a different conversation.
    """

    seen: dict[str, float] = field(default_factory=dict)

    def offer(self, results: list[SearchResult], *, now: float) -> None:
        for result in results:
            self.seen[result.url] = now
        self._forget(now=now)

    def allows(self, url: str, *, now: float) -> bool:
        self._forget(now=now)
        return url in self.seen

    def _forget(self, *, now: float) -> None:
        for url, when in list(self.seen.items()):
            if now - when > _OPENABLE_FOR_S:
                del self.seen[url]


def _voice_sized(snippet: str) -> str:
    """A snippet the length of a sentence, because it will be spoken."""
    trimmed = " ".join(snippet.split())
    if len(trimmed) <= 180:
        return trimmed
    cut = trimmed[:180].rsplit(" ", 1)[0]
    return f"{cut}…"


class WebTools:
    """The web surface, and the small amount of state it needs.

    An object rather than free functions because of :class:`_Openable`: what
    ``web.read`` may open depends on what ``web.search`` returned, and that
    relationship has to live somewhere. One instance per backend.
    """

    def __init__(
        self,
        *,
        search: WebSearch | None = None,
        reader: PageReader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._search = search or WebSearch()
        self._reader = reader or PageReader()
        self._clock = clock
        self._openable = _Openable()

    async def search_web(self, args: Mapping[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        try:
            results = await self._search.search(query, limit=_RESULTS)
        except SearchError as exc:
            raise WebToolError(str(exc)) from exc

        self._openable.offer(results, now=self._clock())
        log.info("web_search", results=len(results))

        if not results:
            return {"query": query, "total": 0}
        return {
            "query": query,
            "total": len(results),
            "results": [
                {
                    "title": result.title,
                    "source": result.host,
                    "url": result.url,
                    "summary": _voice_sized(result.snippet),
                }
                for result in results
            ],
        }

    async def read_page(self, args: Mapping[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        if not self._openable.allows(url, now=self._clock()):
            # Named plainly, because the model can act on it: search first, then
            # open one of the results.
            raise WebToolError(
                "I only open pages that came back from a search. "
                "Search for it first, then ask me to open one of the results."
            )

        try:
            page = await self._reader.read(url)
        except WebFetchError as exc:
            raise WebToolError(str(exc)) from exc

        log.info("web_read", characters=len(page.text))
        return page.as_result()


Handler = Callable[[WebTools, Mapping[str, Any]], Awaitable[dict[str, Any]]]

#: The whole surface. A tool not in here cannot be run, whatever the catalogue
#: says and whatever the model asks for.
WEB_TOOLS: dict[str, Handler] = {
    "web.search": lambda tools, args: tools.search_web(args),
    "web.read": lambda tools, args: tools.read_page(args),
}


async def run_web_tool(tools: WebTools, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    handler = WEB_TOOLS.get(name)
    if handler is None:
        raise WebToolError(f"{name} is not something I can do on the web")
    return await handler(tools, args)
