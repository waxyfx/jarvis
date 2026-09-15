"""Fetching one page and keeping only the part a person would have read.

A search result's snippet is two lines chosen by a search engine. When the owner
asks what something actually says, that is not enough, and the page has to be
opened. Opening a page the *model* named is the dangerous half of web access, so
this module is mostly refusals.

**Where it may go.** Only ``http`` and ``https``, only to an address that is on
the public internet. The backend sits in a network with a tracker, a database
and a metadata service in it, and "fetch this URL" is the classic way to reach
those from outside — so every hop is resolved and checked before the connection
is made, redirects included, and a host that resolves to a private, loopback,
link-local or otherwise reserved address is refused by name.

**How much it may bring back.** A bounded read, decoded, stripped of markup and
cut to a few thousand characters. The model gets what the page says, not the
page: no scripts, no navigation, no comment section, and never so much that a
long document could fill the context with someone else's writing.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from atlas_backend.logging import get_logger

__all__ = ["Page", "PageReader", "WebFetchError"]

log = get_logger(__name__)

#: Enough for the substance of an article, nowhere near enough to drown the
#: conversation in one. A page longer than this is summarised by truncation,
#: which is honest as long as the result says so — see :attr:`Page.truncated`.
_MAX_TEXT = 4000

#: Bytes read off the wire before giving up. A page is text; anything of this
#: size is not something to read aloud, and reading it all costs memory here
#: whatever is done with it afterwards.
_MAX_BYTES = 2_000_000

_MAX_REDIRECTS = 3

_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

_TEXTUAL = ("text/html", "text/plain", "application/xhtml", "application/json", "text/markdown")

_DROP = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|form|aside)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_BREAK = re.compile(r"</(p|div|li|h[1-6]|tr|section|article|br)\s*>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\xa0]{2,}")
#: A non-breaking space. Written as an escape because the character itself
#: is invisible in source, which is how it survived a review once already.
_NBSP = "\xa0"

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


class WebFetchError(RuntimeError):
    """The page could not be fetched, and why — in words worth reading aloud."""


@dataclass(frozen=True, slots=True)
class _Fetched:
    """What came back from the last hop, before anything is made of it."""

    url: str
    content_type: str
    text: str


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    title: str
    text: str
    truncated: bool = False

    def as_result(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            **({"truncated": True} if self.truncated else {}),
        }


def _reject_by_address(host: str, addresses: list[str]) -> None:
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global or address.is_multicast:
            # Named rather than silent. "I can't reach that" for the owner's own
            # router reads as a bug; "that address is on your own network" is
            # the truth and is actionable.
            raise WebFetchError(f"{host} is on a private network, so I will not fetch it")


async def _check_public(url: str) -> None:
    """Refuse anything that is not a public http(s) address.

    Resolution happens here rather than being left to the HTTP client because
    the check has to apply to the address actually connected to. A hostname
    that resolves to 169.254.169.254 or 127.0.0.1 looks perfectly ordinary
    until it is resolved.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebFetchError(f"{parsed.scheme or 'that'} is not a scheme I will open")
    host = parsed.hostname
    if not host:
        raise WebFetchError("that is not a web address I can open")

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise WebFetchError(f"I could not find {host}") from exc

    _reject_by_address(host, [str(info[4][0]) for info in infos])


def _decode(response: httpx.Response, body: bytes) -> str:
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _readable(markup: str) -> str:
    """Markup in, prose out.

    Not a parser and not trying to be one. The furniture — scripts, styles,
    navigation, forms — is removed wholesale, block ends become line breaks so
    sentences do not run together, and what is left is the text. It is cruder
    than a readability algorithm and has no dependency, which for "tell me what
    this page says" is the right trade.
    """
    without_furniture = _DROP.sub(" ", markup)
    with_breaks = _BREAK.sub("\n", without_furniture)
    # Non-breaking spaces become ordinary ones. They are a typesetting
    # instruction, they are everywhere in real HTML, and left in they make
    # "Python 3.14" fail to match "Python 3.14" for everything downstream.
    text = html.unescape(_TAGS.sub(" ", with_breaks)).replace(_NBSP, " ")
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line)).strip()


class PageReader:
    """Opens one page, and returns what it says."""

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        max_text: int = _MAX_TEXT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_s
        self._max_text = max_text
        self._transport = transport

    async def read(self, url: str) -> Page:
        url = url.strip()
        if not url:
            raise WebFetchError("there is no address to open")
        if "://" not in url:
            url = f"https://{url}"

        fetched = await self._get(url)
        content_type = fetched.content_type
        if content_type and not any(kind in content_type for kind in _TEXTUAL):
            raise WebFetchError(f"that page is {content_type.split(';')[0]}, which I cannot read")

        final_url = fetched.url
        body = fetched.text
        text = _readable(body)
        title_match = _TITLE.search(body)
        title = html.unescape(_TAGS.sub("", title_match.group(1))).strip() if title_match else ""

        return Page(
            url=final_url,
            title=title or urlparse(final_url).hostname or final_url,
            text=text[: self._max_text],
            truncated=len(text) > self._max_text,
        )

    async def _get(self, url: str) -> _Fetched:
        """Fetch, following redirects by hand so each hop is checked.

        ``follow_redirects=True`` would check the first address and then connect
        to whatever the server nominated, which is the whole of the defence
        skipped by one header.

        Streamed rather than read whole: the size limit has to apply to what
        comes off the wire, not to what is kept afterwards. A client that
        downloads two hundred megabytes and then trims it to four thousand
        characters has not limited anything.
        """
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, follow_redirects=False
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                await _check_public(url)
                try:
                    async with client.stream(
                        "GET",
                        url,
                        headers={"User-Agent": _AGENT, "Accept-Language": "ru,en;q=0.8"},
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            if not location:
                                raise WebFetchError("that page redirected to nowhere")
                            url = str(httpx.URL(url).join(location))
                            continue
                        if response.status_code >= 400:
                            raise WebFetchError(f"that page answered {response.status_code}")

                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) >= _MAX_BYTES:
                                break
                        return _Fetched(
                            url=url,
                            content_type=response.headers.get("content-type", "").lower(),
                            text=_decode(response, bytes(body)),
                        )
                except httpx.TimeoutException as exc:
                    raise WebFetchError("that page did not answer in time") from exc
                except httpx.HTTPError as exc:
                    raise WebFetchError(
                        f"I could not open that page: {type(exc).__name__}"
                    ) from exc

        raise WebFetchError("that page redirected too many times")
