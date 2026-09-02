"""The wiring between the voice engines and the backend.

No models here — those are measured in atlas-voice. What this file is about is
the part that talks to the network and the part that decides what gets said
aloud, both of which fail in ways a person notices and a model test would not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas_agent.config import AgentSettings
from atlas_agent.voice_runtime import BackendVoice, VoiceModels, _spoken_reply
from atlas_shared.enums import Language
from atlas_voice.providers import Transcript


def settings(tmp_path: Path, url: str = "http://127.0.0.1:9") -> AgentSettings:
    return AgentSettings(
        backend_url=url,
        identity_path=tmp_path / "identity.json",
        mode_state_path=tmp_path / "mode.json",
        allow_plaintext_key=True,
    )


def transcript(text: str = "open notepad", language: Language = Language.EN) -> Transcript:
    return Transcript(text=text, language=language, duration_s=1.5)


class FakeBackendClient:
    """Hands out tokens without a handshake. Records how often it was asked."""

    def __init__(self, *tokens: str) -> None:
        self.tokens = list(tokens) or ["token"]
        self.calls = 0

    async def authenticate(self, identity: Any) -> str:
        self.calls += 1
        return self.tokens[min(self.calls - 1, len(self.tokens) - 1)]


def voice_for(
    tmp_path: Path,
    handler: Any,
    *,
    tokens: tuple[str, ...] = ("token",),
    timeout_s: float = 5.0,
) -> tuple[BackendVoice, FakeBackendClient]:
    client = FakeBackendClient(*tokens)
    return (
        BackendVoice(
            settings(tmp_path),
            object(),  # type: ignore[arg-type] - no test here reaches the signing path
            timeout_s=timeout_s,
            client=client,
            transport=httpx.MockTransport(handler),
        ),
        client,
    )


class TestWhatGetsSaidAloud:
    def test_the_reply_is_read_out_as_it_came(self) -> None:
        assert _spoken_reply({"reply": "Notepad is open, sir."}, Language.EN) == (
            "Notepad is open, sir."
        )

    def test_a_held_action_is_announced(self) -> None:
        """On screen a pending action is a row with a button. Aloud it is
        silence, and silence reads as "done"."""
        spoken = _spoken_reply(
            {"reply": "I can delete that.", "pending_confirmation": [{"tool": "fs.delete"}]},
            Language.EN,
        )

        assert spoken.startswith("I can delete that.")
        assert "confirmation" in spoken

    def test_the_announcement_follows_the_language(self) -> None:
        spoken = _spoken_reply(
            {"reply": "Готово.", "pending_confirmation": [{"tool": "app.close"}]}, Language.RU
        )

        assert "подтверждения" in spoken

    def test_nothing_pending_adds_nothing(self) -> None:
        assert _spoken_reply({"reply": "Готово.", "pending_confirmation": []}, Language.RU) == (
            "Готово."
        )

    def test_a_missing_reply_does_not_become_the_word_none(self) -> None:
        assert _spoken_reply({}, Language.EN) == ""


class TestTalkingToTheBackend:
    async def test_the_transcript_is_sent_as_typed_input_would_be(self, tmp_path: Path) -> None:
        """Voice gets no special endpoint and no special privileges."""
        captured: dict[str, Any] = {}

        async def record(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"reply": "ok"})

        voice, _ = voice_for(tmp_path, record)

        await voice(transcript("открой блокнот", Language.RU))

        assert captured["url"].endswith("/v1/assistant/message")
        assert captured["body"] == {"text": "открой блокнот", "language": "ru"}

    async def test_an_expired_token_is_refreshed_once(self, tmp_path: Path) -> None:
        """401 means "ask again with a new one", not "you may not"."""
        seen: list[str] = []

        async def unauthorised_then_fine(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Authorization"])
            if len(seen) == 1:
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(200, json={"reply": "Done, sir."})

        voice, client = voice_for(tmp_path, unauthorised_then_fine, tokens=("stale", "fresh"))

        assert await voice(transcript()) == "Done, sir."
        assert seen == ["Bearer stale", "Bearer fresh"]
        assert client.calls == 2

    async def test_a_second_rejection_is_not_retried_forever(self, tmp_path: Path) -> None:
        """Looping on a real problem hides it behind a stutter."""
        attempts = 0

        async def always_unauthorised(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401, json={"detail": "no"})

        voice, _ = voice_for(tmp_path, always_unauthorised, tokens=("stale", "fresh"))

        with pytest.raises(httpx.HTTPStatusError):
            await voice(transcript())
        assert attempts == 2

    async def test_a_server_error_is_not_mistaken_for_an_expired_token(
        self, tmp_path: Path
    ) -> None:
        async def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        voice, client = voice_for(tmp_path, broken)

        with pytest.raises(httpx.HTTPStatusError):
            await voice(transcript())
        assert client.calls == 1, "a 500 is not a reason to fetch a new token"

    async def test_the_last_answer_is_kept_for_the_tray(self, tmp_path: Path) -> None:
        async def fine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"reply": "Done.", "executed": [{"tool": "x"}]})

        voice, _ = voice_for(tmp_path, fine)
        await voice(transcript())

        assert voice.last is not None
        assert voice.last["executed"] == [{"tool": "x"}]


class TestWhenTheBackendGoesQuiet:
    """The failure that matters is not refusal, it is silence.

    A refused connection returns at once and the engine apologises for it. A
    backend that accepts the request and then never answers is the one that
    leaves someone staring at "Thinking" — they have no scrollbar to check and
    nothing to read, and silence is indistinguishable from an assistant that
    did not hear them. A real socket is used rather than a mock transport
    because httpx enforces its timeouts in the transport, so a mock would prove
    the timeout works by never testing it.
    """

    async def test_a_hanging_backend_gives_up_rather_than_waiting(self, tmp_path: Path) -> None:
        # Held open by an event rather than a sleep, so tearing the server down
        # does not itself become a wait: Server.wait_closed() blocks until every
        # handler has returned, and a handler sleeping out the clock turns a
        # half-second test into a half-minute one.
        release = asyncio.Event()

        async def accept_and_say_nothing(reader: Any, writer: Any) -> None:
            try:
                await release.wait()
            finally:
                writer.close()

        server = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()

        try:
            voice = BackendVoice(
                settings(tmp_path, f"http://127.0.0.1:{port}"),
                object(),  # type: ignore[arg-type]
                timeout_s=0.4,
                client=FakeBackendClient(),
            )
            started = loop.time()
            with pytest.raises(httpx.TimeoutException):
                await asyncio.wait_for(voice(transcript()), timeout=10)
            waited = loop.time() - started
        finally:
            release.set()
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=10)

        assert waited < 5, f"gave up after {waited:.1f}s, which is not giving up"

    def test_the_default_is_bounded_and_short_enough_to_notice(self) -> None:
        """A default measured in minutes is the same as no default at all."""
        from atlas_agent.voice_runtime import DEFAULT_TURN_TIMEOUT_S

        assert 10.0 <= DEFAULT_TURN_TIMEOUT_S <= 60.0


class TestFindingTheModels:
    def test_every_missing_file_is_named_at_once(self, tmp_path: Path) -> None:
        """Naming the first one means finding out about the next after a
        download, four times over."""
        absent = VoiceModels(root=tmp_path).missing()

        assert len(absent) == 5
        assert any("Silero" in item for item in absent)
        assert any("wake word" in item for item in absent)

    def test_the_paths_say_where_to_look(self, tmp_path: Path) -> None:
        assert all(str(tmp_path) in item for item in VoiceModels(root=tmp_path).missing())
