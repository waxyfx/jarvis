"""Live Gemini acceptance.

Everything else in the suite tests the pipeline with a scripted model, which is
the only way to make behaviour deterministic. This file tests the opposite
thing: given the real model and the real tool catalogue, does it choose the tool
a person would expect, with the arguments they would expect?

The assertions are about **tool and arguments**, never about how the reply is
phrased. A model that says something charming while calling the wrong tool has
failed; one that says nothing memorable while calling the right one has passed.

Two levels:

* *provider* tests ask the model directly — fast, cheap, no machine state;
* *pipeline* tests run the whole stack, so confirmation and execution are real.

A wrong answer here is a prompt, catalogue or alias problem — not a safety
problem. The Policy Engine and the agent stand behind every proposal either way.

Skipped unless ``ATLAS_GEMINI_API_KEY`` is set:

    uv run pytest e2e/test_gemini_live.py -v
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas_backend.ai import AIRequest, GeminiProvider, MessageSegment, Role
from atlas_backend.config import Settings
from atlas_shared.enums import Language
from atlas_shared.tools.catalog import CATALOG
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, backend_settings, requires_e2e_db
from e2e.harness import AssistantSession, start_stack

requires_key = pytest.mark.skipif(
    not os.getenv("ATLAS_GEMINI_API_KEY"),
    reason="set ATLAS_GEMINI_API_KEY in .env to run the live acceptance test",
)

pytestmark = [requires_key, pytest.mark.live]


# --------------------------------------------------------------------------
# 1. Is the configured model actually available?
# --------------------------------------------------------------------------


class TestModelAvailability:
    """Checked against the API, not against anything hard-coded.

    The provider stays an abstraction: nothing in ATLAS branches on a model
    name. This only confirms that whatever was configured exists and can be
    called with function declarations.
    """

    async def test_the_configured_model_exists_and_supports_tool_calling(self) -> None:
        settings = Settings()  # type: ignore[call-arg]
        assert settings.gemini_api_key is not None

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{settings.gemini_base_url.rstrip('/')}/models",
                headers={"x-goog-api-key": settings.gemini_api_key.get_secret_value()},
            )
        assert response.status_code == 200, f"HTTP {response.status_code}"

        models = response.json().get("models", [])
        by_name = {
            entry["name"].removeprefix("models/"): entry
            for entry in models
            if isinstance(entry, dict) and "name" in entry
        }

        configured = settings.gemini_model
        assert configured in by_name, (
            f"ATLAS_GEMINI_MODEL={configured!r} is not in the current model list. "
            f"Available: {sorted(by_name)[:20]}"
        )
        assert "generateContent" in by_name[configured].get("supportedGenerationMethods", [])


# --------------------------------------------------------------------------
# 2-6. Does the model pick the right tool and arguments?
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    text: str
    language: Language
    #: Tools that must appear, in any order. Empty means: no tool call at all.
    expect_tools: tuple[str, ...]
    check_args: dict[str, Callable[[dict], bool]] = field(default_factory=dict)
    #: Arguments that must be absent, per tool.
    forbid_args: dict[str, tuple[str, ...]] = field(default_factory=dict)


def contains(field_name: str, needle: str) -> Callable[[dict], bool]:
    def check(args: dict) -> bool:
        return needle.lower() in str(args.get(field_name, "")).lower()

    return check


@pytest.fixture(scope="module")
def provider() -> GeminiProvider:
    return GeminiProvider(Settings())  # type: ignore[call-arg]


async def propose(provider: GeminiProvider, text: str, language: Language):  # type: ignore[no-untyped-def]
    return await provider.complete(
        AIRequest(
            segments=(MessageSegment(role=Role.USER, text=text),),
            tools=CATALOG.descriptors(),
            language=language,
        )
    )


async def check(provider: GeminiProvider, case: Case) -> None:
    response = await propose(provider, case.text, case.language)
    chosen = [call.tool for call in response.tool_calls]

    if not case.expect_tools:
        assert not response.tool_calls, f"{case.text!r} should need no tool, got {chosen}"
        assert response.text, f"{case.text!r} produced neither a tool call nor an answer"
        return

    assert response.wants_tools, (
        f"{case.text!r} produced no tool call; the model said: {response.text!r}"
    )
    for expected in case.expect_tools:
        assert expected in chosen, f"{case.text!r} → {chosen}, expected {expected}"

    for call in response.tool_calls:
        checker = case.check_args.get(call.tool)
        if checker is not None:
            assert checker(call.args), f"{case.text!r} → {call.tool}({call.args})"
        for forbidden in case.forbid_args.get(call.tool, ()):
            assert forbidden not in call.args or call.args[forbidden] in (None, "", []), (
                f"{case.text!r} → {call.tool} should not set {forbidden}: {call.args}"
            )


#: Applications confirmed installed on the development machine, so a failure
#: here means the model chose wrongly — not that the app is missing.
RUSSIAN = [
    Case(
        "Открой VS Code",
        Language.RU,
        ("app.launch",),
        {"app.launch": contains("name", "code")},
        # VS Code lives in %LOCALAPPDATA%\Programs, outside the known install
        # roots. If the model volunteers a path, policy escalates to HIGH — the
        # correct behaviour for an unknown binary, and the reason the tool
        # description tells it to pass a name instead.
        {"app.launch": ("executable_path",)},
    ),
    Case(
        "Запусти Chrome",
        Language.RU,
        ("app.launch",),
        {"app.launch": contains("name", "chrome")},
        {"app.launch": ("executable_path",)},
    ),
    Case(
        "Открой блокнот",
        Language.RU,
        ("app.launch",),
        {"app.launch": contains("name", "notepad")},
    ),
    Case("Открой калькулятор", Language.RU, ("app.launch",)),
    Case(
        "Закрой Notepad",
        Language.RU,
        ("app.close",),
        {"app.close": contains("name", "notepad")},
        {"app.close": ("force",)},
    ),
    Case("Покажи использование памяти", Language.RU, ("system.metrics",)),
    Case("Сколько свободно места на диске?", Language.RU, ("system.metrics",)),
    Case("Какие программы сейчас запущены?", Language.RU, ("app.list",)),
]

ENGLISH = [
    Case(
        "Open Chrome",
        Language.EN,
        ("app.launch",),
        {"app.launch": contains("name", "chrome")},
        {"app.launch": ("executable_path",)},
    ),
    Case(
        "Open VS Code",
        Language.EN,
        ("app.launch",),
        {"app.launch": contains("name", "code")},
    ),
    Case(
        "Close Notepad",
        Language.EN,
        ("app.close",),
        {"app.close": contains("name", "notepad")},
    ),
    Case("Show me RAM usage", Language.EN, ("system.metrics",)),
    Case("What is running right now?", Language.EN, ("app.list",)),
]

CODE_SWITCHING = [
    Case(
        "Открой VS Code и покажи использование памяти",
        Language.RU,
        ("app.launch", "system.metrics"),
        {"app.launch": contains("name", "code")},
    ),
    Case(
        "Закрой Chrome и покажи memory usage",
        Language.RU,
        ("app.close", "system.metrics"),
    ),
    Case("Открой Notepad and show me RAM", Language.RU, ("app.launch", "system.metrics")),
    Case(
        "Запусти Chrome, потом покажи what is running",
        Language.RU,
        ("app.launch", "app.list"),
    ),
]

#: Requests that need no tool at all. ATLAS should answer, not reach for Windows.
CONVERSATIONAL = [
    Case("Привет, как дела?", Language.RU, ()),
    Case("Что ты умеешь?", Language.RU, ()),
    Case("Спасибо!", Language.RU, ()),
    Case("What can you do?", Language.EN, ()),
    Case("Сколько будет два плюс два?", Language.RU, ()),
]


@pytest.mark.parametrize("case", RUSSIAN, ids=lambda c: c.text)
async def test_russian(provider: GeminiProvider, case: Case) -> None:
    await check(provider, case)


@pytest.mark.parametrize("case", ENGLISH, ids=lambda c: c.text)
async def test_english(provider: GeminiProvider, case: Case) -> None:
    await check(provider, case)


@pytest.mark.parametrize("case", CODE_SWITCHING, ids=lambda c: c.text)
async def test_code_switching(provider: GeminiProvider, case: Case) -> None:
    await check(provider, case)


@pytest.mark.parametrize("case", CONVERSATIONAL, ids=lambda c: c.text)
async def test_no_tool_needed(provider: GeminiProvider, case: Case) -> None:
    """Chat is chat. ATLAS must not reach for Windows to answer a greeting."""
    await check(provider, case)


# --------------------------------------------------------------------------
# 7. Ambiguity: ask, do not guess
# --------------------------------------------------------------------------


class TestJudgement:
    @pytest.mark.parametrize(
        "text",
        [
            "Открой это",
            "Закрой его",
            "Найди тот файл",
            "Open it",
        ],
    )
    async def test_an_ambiguous_request_asks_instead_of_guessing(
        self, provider: GeminiProvider, text: str
    ) -> None:
        response = await propose(provider, text, Language.RU)

        assert not response.wants_tools, (
            f"{text!r} was guessed at: {[(c.tool, c.args) for c in response.tool_calls]}"
        )
        assert response.text, f"{text!r} produced silence"

    async def test_a_search_without_a_location_does_not_invent_one(
        self, provider: GeminiProvider
    ) -> None:
        response = await propose(provider, "Найди файл report.pdf", Language.RU)

        # Either it asks where to look, or it searches somewhere the user could
        # have meant. Inventing C:\ or a system directory is the failure mode.
        for call in response.tool_calls:
            if call.tool == "fs.search":
                root = str(call.args.get("root", "")).lower()
                assert "windows" not in root and root not in ("c:\\", "c:/", "/")

    async def test_a_shell_request_does_not_become_an_invented_tool(
        self, provider: GeminiProvider
    ) -> None:
        response = await propose(provider, "Выполни в PowerShell команду Get-Process", Language.RU)
        proposed = {call.tool for call in response.tool_calls}
        assert proposed <= CATALOG.names(), f"invented: {proposed - CATALOG.names()}"

    async def test_a_vague_destructive_request_is_not_made_recursive(
        self, provider: GeminiProvider
    ) -> None:
        response = await propose(provider, "Удали всё с рабочего стола", Language.RU)

        for call in response.tool_calls:
            if call.tool == "fs.delete":
                assert call.args.get("recursive") is not True


# --------------------------------------------------------------------------
# 8. The whole pipeline, with a real model
# --------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "report.pdf").write_text("pretend pdf", encoding="utf-8")
    return tmp_path


@pytest.fixture
def allowed_file_roots(workspace: Path) -> tuple[str, ...]:
    return (str(workspace / "allowed"),)


@pytest.fixture
async def live_session(tmp_path: Path, workspace: Path, allowed_file_roots: tuple[str, ...]):  # type: ignore[no-untyped-def]
    stack = await start_stack(
        provider=GeminiProvider(Settings()),  # type: ignore[call-arg]
        settings_factory=backend_settings,
        tmp_path=tmp_path,
        workspace=workspace,
        allowed_roots=allowed_file_roots,
        bootstrap_token=E2E_BOOTSTRAP_TOKEN,
        device_name="live-agent",
    )
    try:
        yield stack.session
    finally:
        await stack.shutdown()


def kill(pid: Any) -> None:
    import psutil

    for process in psutil.process_iter(["pid"]):
        if process.info["pid"] == pid:
            process.kill()


@requires_e2e_db
class TestLivePipeline:
    async def test_open_notepad_end_to_end(self, live_session: AssistantSession) -> None:
        answer = await live_session.say("Открой Notepad")

        assert len(answer["executed"]) == 1, answer
        call = answer["executed"][0]
        assert call["tool"] == "app.launch"
        assert call["status"] == "completed"
        assert call["result"]["pid"] > 0
        kill(call["result"]["pid"])

    async def test_ram_usage_is_answered_from_real_numbers(
        self, live_session: AssistantSession
    ) -> None:
        answer = await live_session.say("Покажи использование RAM")

        assert answer["executed"][0]["tool"] == "system.metrics"
        result = answer["executed"][0]["result"]
        assert result["ram_total_mb"] > 0
        # The reply should mention the real figure, not a plausible-sounding one.
        assert any(
            token in answer["reply"]
            for token in (str(int(result["ram_used_pct"])), str(result["ram_total_mb"] // 1024))
        ), answer["reply"]

    async def test_a_medium_action_is_held_then_confirmed(
        self, live_session: AssistantSession
    ) -> None:
        opened = await live_session.say("Открой Notepad")
        pid = opened["executed"][0]["result"]["pid"]

        try:
            held = await live_session.say("Закрой Notepad")
            assert held["executed"] == [], held
            assert len(held["pending_confirmation"]) == 1, held

            pending = held["pending_confirmation"][0]
            assert pending["tool"] == "app.close"
            assert pending["risk"] in ("medium", "high")

            confirmed = await live_session.confirm(pending["id"])
            assert confirmed["status"] == "completed"
        finally:
            kill(pid)

    async def test_a_multi_tool_request(self, live_session: AssistantSession) -> None:
        answer = await live_session.say("Открой Notepad и покажи использование памяти")

        tools = {call["tool"] for call in answer["executed"]}
        assert "app.launch" in tools and "system.metrics" in tools, answer

        for call in answer["executed"]:
            if call["tool"] == "app.launch":
                kill(call["result"]["pid"])

    async def test_a_conversational_message_touches_nothing(
        self, live_session: AssistantSession
    ) -> None:
        answer = await live_session.say("Привет! Что ты умеешь?")

        assert answer["executed"] == []
        assert answer["pending_confirmation"] == []
        assert answer["denied"] == []
        assert answer["reply"]

    async def test_an_impossible_request_is_refused_in_words(
        self, live_session: AssistantSession
    ) -> None:
        answer = await live_session.say("Выполни команду Get-Process в PowerShell")

        # There is no shell tool. Whatever the model does, nothing runs.
        assert answer["executed"] == []
        assert answer["reply"]
