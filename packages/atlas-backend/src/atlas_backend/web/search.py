"""Searching the actual internet, so the model stops answering from memory.

A model asked "what is the latest version of X" answers confidently from
training data that is months old, and nothing in the answer says so. That is the
failure this exists to fix: when the owner says "найди в интернете", something
must actually go and look.

**DuckDuckGo Lite, by POST.** Free, no key, no account. The HTML endpoint and
the instant-answer API both return 202 to a plain client — bot detection — and
the Lite endpoint answers 200 but only to a POST, which is what its own form
does. Its markup is a table of `<a class="result-link">`, which is stable enough
to parse with a regular expression and simple enough that a change is obvious
rather than subtle.

**Results are data, never instructions.** A search result is a stranger's
writing, chosen by a ranking nobody here controls. It reaches the model marked
as external content, which is what the orchestrator already uses to tighten
policy, and it is never executed, never followed, never treated as a command.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from atlas_backend.logging import get_logger

__all__ = ["SearchError", "SearchResult", "WebSearch"]

log = get_logger(__name__)

#: The Lite endpoint is the one that answers a plain client. See the module
#: docstring for what the alternatives do instead.
_ENDPOINT = "https://lite.duckduckgo.com/lite/"

#: A browser string, because the endpoint refuses an obviously scripted client.
#: Not a disguise: the request is a search, exactly what the page performs.
_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

_RESULT = re.compile(
    r'<a[^>]+href="(?P<url>[^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET = re.compile(
    r'class=[\'"]result-snippet[\'"][^>]*>(?P<text>.*?)</td>', re.DOTALL | re.IGNORECASE
)
_TAGS = re.compile(r"<[^>]+>")


class SearchError(RuntimeError):
    """The search could not be done. Never contains anything from a result."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    @property
    def host(self) -> str:
        """Where it came from, which is most of how a person judges a result."""
        return urlparse(self.url).hostname or ""

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _clean(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment)).strip()


def _direct(url: str) -> str:
    """Unwrap DuckDuckGo's redirector so the model sees the real address.

    A `//duckduckgo.com/l/?uddg=...` link tells nobody where the answer came
    from, and where it came from is most of how a person decides whether to
    believe it.
    """
    if "duckduckgo.com/l/" not in url:
        return url if url.startswith("http") else f"https:{url}"
    target = parse_qs(urlparse(url).query).get("uddg")
    return unquote(target[0]) if target else url


class WebSearch:
    """Asks the web a question and returns what it found."""

    name = "duckduckgo"

    def __init__(
        self,
        *,
        timeout_s: float = 12.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_s
        self._transport = transport

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        query = query.strip()
        if not query:
            raise SearchError("there is nothing to search for")

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=True
            ) as client:
                # POST, not GET: the endpoint answers a GET with the form and no
                # results at all, which looks like "nothing found" rather than
                # "asked wrongly".
                response = await client.post(
                    _ENDPOINT,
                    data={"q": query},
                    headers={"User-Agent": _AGENT, "Accept-Language": "ru,en;q=0.8"},
                )
        except httpx.TimeoutException as exc:
            raise SearchError("the search did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"could not reach the search engine: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise SearchError(f"the search engine answered {response.status_code}")

        return self._parse(response.text, limit=limit)

    @staticmethod
    def _parse(body: str, *, limit: int) -> list[SearchResult]:
        snippets = [_clean(match.group("text")) for match in _SNIPPET.finditer(body)]
        results: list[SearchResult] = []

        for index, match in enumerate(_RESULT.finditer(body)):
            title = _clean(match.group("title"))
            url = _direct(match.group("url"))
            if not title or not url.startswith("http"):
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippets[index] if index < len(snippets) else "",
                )
            )
            if len(results) >= limit:
                break

        return results
