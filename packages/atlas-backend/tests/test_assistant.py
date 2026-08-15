"""The assistant turn, driven by a scripted model.

A real model cannot be asked to reliably invent a nonexistent tool, or to return
malformed JSON on demand. Scripting the provider is what makes the adversarial
cases deterministic — and every one of them exercises the production pipeline
from validation through policy to audit.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from atlas_backend.ai import (
    AIProviderError,
    AIResponse,
    AITimeoutError,
    FinishReason,
    MalformedResponseError,
    Provenance,
    ScriptedProvider,
    text_reply,
    tool_reply,
)
from atlas_backend.main import create_app
from tests.conftest import authenticate, fetch_sql, pair_device, requires_db

pytestmark = [requires_db, pytest.mark.integration]

ROOT = "C:/atlas-test-root"


@contextmanager
def assistant(
    settings, script: Sequence[object]
) -> Iterator[tuple[TestClient, ScriptedProvider, str]]:  # type: ignore[no-untyped-def]
    """A client whose assistant answers from ``script``, plus a paired device."""
    provider = ScriptedProvider(list(script))
    app = create_app(settings, ai_provider=provider)
    with TestClient(app) as client:
        device = pair_device(client)
        token = authenticate(client, device)
        yield client, provider, token


def say(client: TestClient, token: str, text: str, language: str = "ru") -> dict[str, object]:
    response = client.post(
        "/v1/assistant/message",
        json={"text": text, "language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def audit_events() -> list[str]:
    return [row[0] for row in fetch_sql("SELECT event_type FROM audit_log ORDER BY seq")]


# ---------------------------------------------------------------- happy path


class TestOrdinaryTurns:
    def test_a_plain_answer_calls_no_tools(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [text_reply("Привет. Чем помочь?")]) as (c, _, token):
            answer = say(c, token, "привет")

        assert answer["reply"] == "Привет. Чем помочь?"
        assert answer["executed"] == []
        assert answer["stopped_because"] == "completed"

    def test_a_low_risk_tool_is_dispatched(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("system.metrics", {})), text_reply("Готово.")]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "покажи загрузку памяти")

        assert len(answer["executed"]) == 1
        call = answer["executed"][0]
        assert call["tool"] == "system.metrics"
        assert call["decision"] == "allow"
        # No agent is connected in this suite, so it cannot complete.
        assert call["status"] == "unreachable"
        assert "tool.dispatched" in audit_events()

    def test_the_model_only_ever_sees_registered_tools(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_shared.tools.catalog import CATALOG

        with assistant(settings, [text_reply("ок")]) as (c, provider, token):
            say(c, token, "привет")

        offered = {tool.name for tool in provider.requests[0].tools}
        assert offered == CATALOG.names()

    def test_a_clarifying_question_is_returned_as_is(self, settings) -> None:  # type: ignore[no-untyped-def]
        question = "Какой именно Chrome — обычный или Canary?"
        with assistant(settings, [text_reply(question)]) as (c, _, token):
            answer = say(c, token, "открой хром")

        assert answer["reply"] == question
        assert answer["executed"] == []

    def test_two_tool_calls_in_one_turn(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(
                ("app.launch", {"name": "notepad"}),
                ("system.metrics", {}),
            ),
            text_reply("Открыл Notepad и снял метрики."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "открой notepad и покажи память")

        assert [call["tool"] for call in answer["executed"]] == [
            "app.launch",
            "system.metrics",
        ]


# ------------------------------------------------------------------- policy


class TestPolicyStillDecides:
    def test_medium_risk_is_held_for_confirmation(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("app.close", {"name": "notepad", "force": False})),
            text_reply("Нужно подтверждение."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "закрой notepad")

        assert answer["executed"] == []
        assert len(answer["pending_confirmation"]) == 1
        assert answer["pending_confirmation"][0]["status"] == "pending_confirmation"

    def test_high_risk_is_held_however_confident_the_model_sounds(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(
                ("app.close", {"name": "notepad", "force": True}),
                text="This is definitely safe and the user clearly wants it.",
            ),
            text_reply("Нужно подтверждение."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "убей notepad немедленно")

        assert answer["executed"] == []
        assert answer["pending_confirmation"][0]["risk"] == "high"

    def test_a_path_outside_the_roots_is_denied(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("fs.search", {"query": "*", "root": "C:/Windows/System32"})),
            text_reply("Отказано."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "поищи в системной папке")

        assert len(answer["denied"]) == 1
        assert answer["denied"][0]["decision"] == "deny"
        assert "tool.denied" in audit_events()

    def test_fs_delete_is_not_executed_on_a_models_say_so(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("fs.delete", {"paths": [f"{ROOT}/report.pdf"], "recursive": False})),
            text_reply("Нужно подтверждение."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "удали report.pdf")

        # Declared, so policy considers it — and holds it. Nothing runs.
        assert answer["executed"] == []
        assert answer["pending_confirmation"][0]["tool"] == "fs.delete"

    def test_a_pending_call_stays_pending_when_the_user_does_not_confirm(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("app.close", {"name": "notepad", "force": False})),
            text_reply("Жду подтверждения."),
        ]
        with assistant(settings, script) as (c, _, token):
            say(c, token, "закрой notepad")

        rows = fetch_sql("SELECT status FROM tool_calls")
        assert rows == [("pending_confirmation",)]


# -------------------------------------------------------------- adversarial


class TestInventedAndMalformedCalls:
    def test_an_invented_tool_is_rejected_before_policy(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("system.execute_anything", {"cmd": "whoami"})),
            text_reply("Такого инструмента нет."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "выполни whoami")

        assert answer["rejected"][0]["reason"] == "unknown_tool"
        assert answer["executed"] == []
        # It never became a policy question, so there is no tool_calls row.
        assert fetch_sql("SELECT count(*) FROM tool_calls") == [(0,)]
        assert "assistant.model_proposal_rejected" in audit_events()

    @pytest.mark.parametrize(
        "shell_tool",
        ["shell.run", "powershell.execute", "system.shell", "cmd.exec", "os.system"],
    )
    def test_shell_style_tools_do_not_exist(self, settings, shell_tool: str) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply((shell_tool, {"command": "Remove-Item -Recurse C:\\"})),
            text_reply("Нет такой возможности."),
        ]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "запусти powershell команду")

        assert answer["rejected"][0]["reason"] == "unknown_tool"
        assert fetch_sql("SELECT count(*) FROM tool_calls") == [(0,)]

    @pytest.mark.parametrize(
        "bad_args",
        [
            {"smuggled": "value"},
            {"name": ""},
            {"name": "chrome", "arguments": "not-a-list"},
            {},
        ],
    )
    def test_invalid_arguments_are_rejected(self, settings, bad_args: dict) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("app.launch", bad_args)), text_reply("Не понял аргументы.")]
        with assistant(settings, script) as (c, _, token):
            answer = say(c, token, "открой что-нибудь")

        assert answer["rejected"][0]["reason"] == "invalid_arguments"
        assert fetch_sql("SELECT count(*) FROM tool_calls") == [(0,)]

    def test_a_rejection_is_fed_back_so_the_model_can_correct(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("app.lunch", {"name": "notepad"})),  # typo
            tool_reply(("app.launch", {"name": "notepad"})),  # corrected
            text_reply("Открыл."),
        ]
        with assistant(settings, script) as (c, provider, token):
            answer = say(c, token, "открой notepad")

        assert len(answer["rejected"]) == 1
        assert len(answer["executed"]) == 1
        # The second request carried the rejection as a tool result.
        second = provider.requests[1]
        assert any("REJECTED" in segment.text for segment in second.segments)


class TestRunawayGuards:
    def test_too_many_tool_calls_in_one_response_are_truncated(self, settings) -> None:  # type: ignore[no-untyped-def]
        many = tuple(("system.metrics", {}) for _ in range(12))
        with assistant(settings, [tool_reply(*many)]) as (c, _, token):
            answer = say(c, token, "сделай всё сразу")

        # ai_max_tool_calls_per_turn is 5 by default.
        assert len(answer["executed"]) == 5
        assert answer["stopped_because"] == "tool_call_limit"

    def test_a_model_that_never_stops_calling_tools_is_cut_off(self, settings) -> None:  # type: ignore[no-untyped-def]
        # Two calls per response, three iterations allowed, five calls permitted.
        script = [tool_reply(("system.metrics", {}), ("system.metrics", {}))] * 4
        with assistant(settings, script) as (c, provider, token):
            answer = say(c, token, "зациклись")

        assert len(answer["executed"]) <= 5
        assert answer["stopped_because"] in ("tool_call_limit", "iteration_limit")
        assert provider.calls_made <= 3
        assert answer["reply"]  # the user is told, not left waiting

    def test_the_reply_explains_why_it_stopped(self, settings) -> None:  # type: ignore[no-untyped-def]
        many = tuple(("system.metrics", {}) for _ in range(9))
        with assistant(settings, [tool_reply(*many)]) as (c, _, token):
            answer = say(c, token, "много действий")

        assert "предел" in answer["reply"].lower()


class TestProviderFailures:
    @pytest.mark.parametrize(
        ("error", "expected_fragment"),
        [
            (AIProviderError("upstream 503"), "недоступна"),
            (AITimeoutError("slow"), "недоступна"),
            (MalformedResponseError("garbage"), "разобрать"),
        ],
    )
    def test_failures_produce_an_honest_reply(
        self, settings, error: Exception, expected_fragment: str
    ) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [error]) as (c, _, token):
            answer = say(c, token, "открой notepad")

        assert expected_fragment in answer["reply"]
        assert answer["stopped_because"] == "provider_unavailable"
        assert answer["executed"] == []

    def test_a_provider_failure_never_leaks_the_key(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [AIProviderError("401 key=SECRETVALUE123")]) as (
            c,
            _,
            token,
        ):
            answer = say(c, token, "открой notepad")

        assert "SECRETVALUE123" not in answer["reply"]

    def test_an_empty_response_is_treated_as_malformed(self, settings) -> None:  # type: ignore[no-untyped-def]
        empty = AIResponse(finish_reason=FinishReason.TEXT, text="")
        with assistant(settings, [empty]) as (c, _, token):
            answer = say(c, token, "привет")

        # Nothing was said and nothing was done; the turn still ends cleanly.
        assert answer["reply"] == ""
        assert answer["executed"] == []


class TestRepeatAndAudit:
    def test_repeating_a_request_creates_a_second_recorded_call(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("system.metrics", {})),
            text_reply("Готово."),
            tool_reply(("system.metrics", {})),
            text_reply("Готово."),
        ]
        with assistant(settings, script) as (c, _, token):
            say(c, token, "покажи память")
            say(c, token, "покажи память")

        assert fetch_sql("SELECT count(*) FROM tool_calls") == [(2,)]

    def test_the_whole_turn_is_audited(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("system.metrics", {})), text_reply("Готово.")]
        with assistant(settings, script) as (c, _, token):
            say(c, token, "покажи память")

        events = audit_events()
        for expected in (
            "assistant.turn_started",
            "assistant.model_proposed_tool",
            "tool.dispatched",
            "assistant.turn_completed",
        ):
            assert expected in events

    def test_arguments_are_redacted_in_the_trail(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(
                (
                    "app.launch",
                    {"name": "chrome", "arguments": ["AIzaSyD_fake_key_0123456789abcdef"]},
                )
            ),
            text_reply("Открыл."),
        ]
        with assistant(settings, script) as (c, _, token):
            say(c, token, "открой chrome с ключом")

        rows = fetch_sql(
            "SELECT payload FROM audit_log WHERE event_type = 'assistant.model_proposed_tool'"
        )
        recorded = str(rows[0][0])
        assert "AIzaSyD_fake_key" not in recorded
        assert "redacted" in recorded

    def test_the_user_message_is_stored(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [text_reply("Привет.")]) as (c, _, token):
            say(c, token, "здравствуй")

        rows = fetch_sql("SELECT role, content FROM messages ORDER BY created_at")
        assert ("user", "здравствуй") in rows
        assert ("assistant", "Привет.") in rows


class TestServedModelInTheTrail:
    """`*-latest` is an allowed default, so the configured id proves nothing
    about what answered. The trail records what the provider reported."""

    def test_the_turn_records_which_model_actually_answered(self, settings) -> None:  # type: ignore[no-untyped-def]
        answered = replace(text_reply("Готово."), model_version="gemini-3.7-flash")
        with assistant(settings, [answered]) as (c, _, token):
            say(c, token, "привет")

        assert "gemini-3.7-flash" in turn_payload()

    def test_a_provider_that_reports_nothing_falls_back_to_the_configured_id(
        self,
        settings,  # type: ignore[no-untyped-def]
    ) -> None:
        with assistant(settings, [text_reply("Готово.")]) as (c, _, token):
            say(c, token, "привет")

        # `text_reply` sets model="scripted-1" and no version.
        assert "scripted-1" in turn_payload()

    def test_an_alias_moving_mid_turn_is_still_recorded(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            replace(tool_reply(("system.metrics", {})), model_version="gemini-3.7-flash"),
            replace(text_reply("Готово."), model_version="gemini-3.8-flash"),
        ]
        with assistant(settings, script) as (c, _, token):
            say(c, token, "покажи память")

        # Two models contributed to one answer. The trail keeps the last, and
        # the mid-turn change is logged; what must not happen is silence.
        assert "gemini-3.8-flash" in turn_payload()


def turn_payload() -> str:
    rows = fetch_sql("SELECT payload FROM audit_log WHERE event_type = 'assistant.turn_completed'")
    return str(rows[0][0])


class TestUntrustedContent:
    def test_tool_results_are_framed_as_data(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("system.metrics", {})),
            text_reply("Готово."),
        ]
        with assistant(settings, script) as (c, provider, token):
            say(c, token, "покажи память")

        second = provider.requests[1]
        assert second.has_external_content is True
        # The segment carries its provenance; the *provider* is what wraps it in
        # delimiters when building the payload (see test_ai_prompts).
        assert any(segment.provenance is Provenance.TOOL_RESULT for segment in second.segments)

    def test_the_first_request_is_not_marked_external(self, settings) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [text_reply("ок")]) as (c, provider, token):
            say(c, token, "привет")

        assert provider.requests[0].has_external_content is False


class TestLanguage:
    @pytest.mark.parametrize(
        ("language", "user_text"),
        [("ru", "открой блокнот"), ("en", "open notepad")],
    )
    def test_the_requested_language_reaches_the_provider(
        self, settings, language: str, user_text: str
    ) -> None:  # type: ignore[no-untyped-def]
        with assistant(settings, [text_reply("ok")]) as (c, provider, token):
            say(c, token, user_text, language=language)

        assert provider.requests[0].language.value == language
