"""Character, applied to a real turn, and stopping where it should.

The personality layer is tested on its own next door. What is tested here is the
wiring: that it runs at the end of a real assistant turn, that it is the *last*
thing to happen, and — the part worth a whole file — that it cannot change a
single fact the owner will act on.

The rule it enforces is blunt on purpose. Any turn that involved a tool goes out
exactly as the model wrote it. Not "tools that succeeded", not "tools that
changed something": any tool at all. The reason is in
`atlas_backend/personality/turn.py` — the orchestrator's `executed` list means
"attempted", and the stored status is the dispatch lifecycle rather than the
agent's own verdict, so there is no reliable way to tell a success from a
failure at this layer. Decorating only the turns that are certainly harmless is
the conservative reading, and the one that survives being wrong.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
from starlette.testclient import TestClient

from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from atlas_backend.main import create_app
from atlas_backend.personality.engine import RuleBasedPersonality
from tests.conftest import authenticate, fetch_sql, pair_device, requires_db

pytestmark = [requires_db, pytest.mark.integration]


@contextmanager
def assistant(
    settings, script: Sequence[object], **overrides: Any
) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    app = create_app(
        settings.model_copy(update={"personality_enabled": True, **overrides}),
        ai_provider=ScriptedProvider(list(script)),
    )
    with TestClient(app) as client:
        device = pair_device(client)
        yield client, authenticate(client, device)


def say(client: TestClient, token: str, text: str, language: str = "ru") -> dict[str, Any]:
    response = client.post(
        "/v1/assistant/message",
        json={"text": text, "language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestItRuns:
    def test_a_plain_answer_is_addressed(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [text_reply("Свободно 42 гигабайта.")]) as (client, token):
            answer = say(client, token, "привет")

        assert answer["reply"] == "Свободно 42 гигабайта, сэр."

    def test_the_address_goes_where_it_belongs_in_the_sentence(self, settings) -> None:  # type: ignore[no-untyped-def]
        """ "Готово!, сэр" is not a sentence, and "Сэр, Готово!" is not Russian.
        The address slips in front of the punctuation."""
        with assistant(settings, [text_reply("Готово!")]) as (client, token):
            answer = say(client, token, "спасибо")

        assert answer["reply"] == "Готово, сэр!"

    def test_switching_it_off_leaves_the_reply_exactly_as_written(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(
            settings, [text_reply("Свободно 42 гигабайта.")], personality_enabled=False
        ) as (
            client,
            token,
        ):
            answer = say(client, token, "привет")

        assert answer["reply"] == "Свободно 42 гигабайта."

    def test_it_does_not_address_the_owner_every_single_turn(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Said every time, it stops being character and becomes a tic."""
        script = [text_reply("Первое."), text_reply("Второе."), text_reply("Третье.")]

        with assistant(settings, script) as (client, token):
            replies = [say(client, token, f"вопрос {index}")["reply"] for index in range(3)]

        assert replies[0] == "Первое, сэр."
        assert replies[1] == "Второе."
        assert replies[2] == "Третье."


class TestItCannotTouchTheFacts:
    def test_a_turn_with_a_tool_goes_out_verbatim(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The whole point. An answer the owner is about to act on is not
        somewhere to be experimenting with wording."""
        script = [tool_reply(("system.metrics", {})), text_reply("Не удалось, сэр.")]

        with assistant(settings, script) as (client, token):
            answer = say(client, token, "покажи память")

        assert answer["reply"] == "Не удалось, сэр."
        assert answer["executed"][0]["status"] == "unreachable"

    def test_a_refused_tool_call_is_also_left_alone(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("fs.destroy_everything", {})), text_reply("Такого я не умею.")]

        with assistant(settings, script) as (client, token):
            answer = say(client, token, "удали всё")

        assert answer["reply"] == "Такого я не умею."
        assert answer["rejected"][0]["tool"] == "fs.destroy_everything"

    def test_what_is_stored_is_what_was_said(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The transcript and the reply must not disagree. If the owner is told
        one thing and the record keeps another, the record is worthless."""
        with assistant(settings, [text_reply("Привет.")]) as (client, token):
            reply = say(client, token, "здравствуй")["reply"]

        rows = fetch_sql("SELECT role, content FROM messages ORDER BY created_at")
        assert ("assistant", reply) in rows

    def test_a_broken_personality_layer_costs_the_owner_nothing(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The reply is already correct before this layer runs, so a decoration
        that raises is a decoration not applied — not an apology where an answer
        should have been."""

        class Exploding(RuleBasedPersonality):
            def present(self, reply, *, config, history):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        app = create_app(
            settings.model_copy(update={"personality_enabled": True}),
            ai_provider=ScriptedProvider([text_reply("Свободно 42 гигабайта.")]),
            personality=Exploding(),
        )

        with TestClient(app) as client:
            token = authenticate(client, pair_device(client))
            answer = say(client, token, "сколько места")

        assert answer["reply"] == "Свободно 42 гигабайта."
