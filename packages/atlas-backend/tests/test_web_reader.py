"""Opening a page, and — mostly — refusing to.

``web.read`` is the one tool in the catalogue that turns an address the *model*
produced into a connection, and the backend sits in a network with a database,
a tracker and, on a cloud host, a metadata service that hands out credentials to
anyone who asks over plain HTTP. "Fetch this URL" is the standard way to reach
those from outside.

So the refusals are the subject here, and they are checked against literal
addresses, against names that resolve to them, and against a redirect to one —
the last because a first draft that checks only the address it was given is
defeated by a single `Location` header.
"""

from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from atlas_backend.web.reader import PageReader, WebFetchError

ARTICLE = """
<html><head><title>Status of Python versions</title>
<style>.x{color:red}</style></head>
<body>
<nav><a href="/">Home</a><a href="/downloads">Downloads</a></nav>
<script>window.analytics = 1;</script>
<h1>Status of Python versions</h1>
<p>The main branch is currently the future Python 3.16.</p>
<p>Python&nbsp;3.14 is the latest &mdash; released Oct.&nbsp;7, 2025.</p>
<footer>&copy; Python Software Foundation</footer>
</body></html>
"""


def serving(
    body: str = ARTICLE,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    location: str | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type}
        if location is not None and request.url.host == "start.example":
            return httpx.Response(302, headers={**headers, "location": location})
        return httpx.Response(status, headers=headers, text=body)

    return httpx.MockTransport(handler)


def _answer(address: str, port: int | None) -> list[tuple[object, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, port or 80))]


@pytest.fixture(autouse=True)
def hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real DNS in this file.

    The reader resolves every hop before connecting, so without this the tests
    would depend on `example.com` existing and on invented names not existing —
    two facts about the internet rather than about the code. A literal address
    resolves to itself; anything else resolves somewhere public.
    """

    def fake(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = str(host).strip("[]")
        if name == "localhost":
            return _answer("127.0.0.1", port)
        try:
            return _answer(str(ipaddress.ip_address(name)), port)
        except ValueError:
            return _answer("93.184.216.34", port)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def resolving_to(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every hostname resolve where the test says, DNS included.

    Checking a literal address proves the easy half. The half that matters is a
    perfectly ordinary name that happens to answer with 127.0.0.1.
    """

    def fake(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _answer(address, port)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class TestWhatItWillNotOpen:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8000/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/",
            "http://172.16.4.4/",
            "http://[::1]/",
        ],
    )
    async def test_an_address_on_the_owners_own_network_is_refused(self, url: str) -> None:
        def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError(f"it connected to {request.url}")

        with pytest.raises(WebFetchError, match="private network"):
            await PageReader(transport=httpx.MockTransport(explode)).read(url)

    async def test_a_public_looking_name_that_resolves_inward_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the check resolves rather than pattern-matching the
        host. `internal.example.com` looks like anywhere else."""
        resolving_to("127.0.0.1", monkeypatch)

        def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("it connected")

        with pytest.raises(WebFetchError, match="private network"):
            await PageReader(transport=httpx.MockTransport(explode)).read(
                "https://dashboard.example.com/"
            )

    async def test_a_redirect_into_the_private_network_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`follow_redirects=True` would check the first address and then
        connect wherever the server nominated — the entire defence skipped by
        one header. Redirects are followed by hand so each hop is checked."""
        reached: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reached.append(str(request.url))
            if request.url.host == "start.example":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/token"})
            return httpx.Response(200, text="secrets")  # pragma: no cover

        with pytest.raises(WebFetchError, match="private network"):
            await PageReader(transport=httpx.MockTransport(handler)).read(
                "https://start.example/go"
            )

        assert reached == ["https://start.example/go"], "it must not have followed the redirect"

    @pytest.mark.parametrize("url", ["file:///C:/Users/serik/.env", "ftp://example.com/x"])
    async def test_only_http_is_a_scheme_it_will_open(self, url: str) -> None:
        with pytest.raises(WebFetchError, match="not a scheme I will open"):
            await PageReader().read(url)

    async def test_something_that_is_not_a_page_is_refused_by_type(self) -> None:
        reader = PageReader(transport=serving(content_type="application/pdf"))

        with pytest.raises(WebFetchError, match="application/pdf"):
            await reader.read("https://example.com/report.pdf")

    async def test_an_error_page_is_reported_by_status(self) -> None:
        reader = PageReader(transport=serving(status=404))

        with pytest.raises(WebFetchError, match="404"):
            await reader.read("https://example.com/gone")


class TestWhatItBringsBack:
    async def test_the_prose_survives_and_the_furniture_does_not(self) -> None:
        page = await PageReader(transport=serving()).read("https://devguide.python.org/versions/")

        assert "the future Python 3.16" in page.text
        assert "window.analytics" not in page.text, "scripts are not what the page says"
        assert "Downloads" not in page.text, "navigation is not what the page says"

    async def test_entities_are_decoded(self) -> None:
        page = await PageReader(transport=serving()).read("https://example.com/")

        assert "Python 3.14 is the latest — released Oct. 7, 2025." in page.text

    async def test_blocks_do_not_run_into_each_other(self) -> None:
        """Without a line break at a block end, the heading and the first
        sentence become one word — read aloud that is gibberish."""
        page = await PageReader(transport=serving()).read("https://example.com/")

        assert "versionsThe" not in page.text

    async def test_it_keeps_the_title(self) -> None:
        page = await PageReader(transport=serving()).read("https://example.com/")

        assert page.title == "Status of Python versions"

    async def test_a_page_with_no_title_falls_back_to_where_it_came_from(self) -> None:
        reader = PageReader(transport=serving("<html><body><p>Hello.</p></body></html>"))

        page = await reader.read("https://example.com/x")

        assert page.title == "example.com"

    async def test_a_long_page_is_cut_and_says_so(self) -> None:
        """Whatever is in the result is what the model has read and may recite.
        A cut that is not declared invites it to answer as if it had the rest."""
        long_body = f"<html><body><p>{'word ' * 5000}</p></body></html>"
        reader = PageReader(transport=serving(long_body), max_text=500)

        page = await reader.read("https://example.com/long")

        assert len(page.text) == 500
        assert page.truncated is True
        assert page.as_result()["truncated"] is True

    async def test_a_short_page_is_not_marked_truncated(self) -> None:
        page = await PageReader(transport=serving()).read("https://example.com/")

        assert page.truncated is False
        assert "truncated" not in page.as_result()

    async def test_a_bare_host_is_treated_as_https(self) -> None:
        page = await PageReader(transport=serving()).read("example.com")

        assert page.url.startswith("https://")


class TestFollowingRedirects:
    async def test_an_ordinary_redirect_is_followed_and_the_final_address_reported(self) -> None:
        """Where the answer actually came from is the address worth reporting:
        it is the one the owner would open to check."""
        reader = PageReader(transport=serving(location="https://end.example/article"))

        page = await reader.read("https://start.example/go")

        assert page.url == "https://end.example/article"

    async def test_a_redirect_loop_ends_rather_than_spinning(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://loop.example/next"})

        with pytest.raises(WebFetchError, match="redirected too many times"):
            await PageReader(transport=httpx.MockTransport(handler)).read("https://loop.example/a")
