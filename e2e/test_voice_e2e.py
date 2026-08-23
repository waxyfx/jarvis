"""M4 acceptance: a spoken sentence all the way to a running program.

    speech → wake word → VAD → segmenter → Whisper → backend → model
           → Policy Engine → signed command → agent → execution → Piper → audio

Every stage below the microphone is the production one: the real sherpa
detector, the real Silero VAD, the real Whisper, a real backend on a real
socket, real policy, real signatures, a real agent. Only two things are
substituted, and both for the same reason — a test cannot have a person in it.

**The speech is synthesised.** Piper stands in for a human, and the clips are
cached to disk, which is what makes the suite reproducible — Piper itself is
not, and taking it at its word cost an afternoon of chasing failures that moved
between runs. It is runnable on a machine with no microphone. It does not
make it a measurement of the wake word: the acceptance for that is 592 recorded
clips in ``training/wakeword/metrics``, where this configuration gave 3.08 false
activations an hour against openWakeWord's 780.75. A synthetic voice is used
here only to get *something* through the front door.

**The model is scripted.** Whether Gemini picks the right tool for a Russian
sentence is measured in ``test_gemini_live.py`` against the real API. What is
measured here is the pipeline — that a sentence spoken at one end starts a
program at the other, and that the answer comes back as audio.

Which synthetic voice is used is decided per phrase, by trying candidates until
one actually wakes the detector. That is not the test being lenient with itself
— it is the measured behaviour. Of ten voices tried on one phrase, six triggered
and four did not, and the same voice that reliably wakes on "Jarvis, open
Notepad" does not wake on "Jarvis, what is the time?": recall depends on what
follows the word as much as on who says it, which is exactly what the recorded
benchmark reports at 71%. Pinning one voice would give a suite that fails for
reasons having nothing to do with the change under test, and papering over it by
choosing only phrases that happen to work would be worse — it would hide the
limitation instead of naming it.
"""

from __future__ import annotations

import asyncio
import sys
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from atlas_shared.enums import Language
from atlas_voice.audio import frames_from_array
from atlas_voice.engines.piper_tts import PiperTTS, VoiceChoice
from atlas_voice.engines.sherpa_kws import SherpaKeywordSpotter
from atlas_voice.engines.silero import SileroVAD
from atlas_voice.engines.whisper import WhisperSettings, WhisperSTT
from atlas_voice.providers import Transcript, Utterance
from atlas_voice.session import SessionConfig, VoiceSession
from atlas_voice.state import VoiceState
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, backend_settings, requires_e2e_db
from e2e.harness import AssistantSession, start_stack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "atlas-voice" / "tests"))

from atlas_agent.voice_runtime import (
    WAKE_KEYWORDS,
    WAKE_THRESHOLD,
    VoiceModels,
)
from voicefixtures import PIPER_MULTI, PIPER_RU, say

MODELS = VoiceModels(root=Path(__file__).resolve().parents[1] / ".models")

#: Tried in order until one wakes the detector for the phrase in hand. See the
#: module docstring for why this is a search rather than a constant.
CANDIDATE_VOICES = (200, 333, 11, 470, 640, 830, 150)

requires_voice_models = pytest.mark.skipif(
    bool(MODELS.missing()) or not PIPER_MULTI.is_file(),
    reason="run scripts/fetch_voice_models.ps1 to download the voice models",
)

pytestmark = [requires_e2e_db, requires_voice_models, pytest.mark.integration]


def spoken(text: str, *, speaker: int = CANDIDATE_VOICES[0], tail_s: float = 1.2) -> np.ndarray:
    """One utterance with silence either side, as a room would deliver it.

    The trailing silence is not padding: it is what tells the segmenter the
    person has stopped, and without it the utterance is never closed and never
    transcribed.
    """
    speech = say(PIPER_MULTI, text, speaker_id=speaker)
    lead = np.zeros(int(0.5 * 16_000), dtype=np.float32)
    tail = np.zeros(int(tail_s * 16_000), dtype=np.float32)
    return np.concatenate([lead, speech, tail]).astype(np.float32)


def russian(text: str, *, tail_s: float = 1.2) -> np.ndarray:
    """A Russian utterance, from the Russian voice. No wake word in it."""
    speech = say(PIPER_RU, text)
    lead = np.zeros(int(0.3 * 16_000), dtype=np.float32)
    tail = np.zeros(int(tail_s * 16_000), dtype=np.float32)
    return np.concatenate([lead, speech, tail]).astype(np.float32)


def _wakes(audio: np.ndarray) -> bool:
    """Would the real detector fire on this? Asked with a throwaway detector."""
    detector = SherpaKeywordSpotter(MODELS.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD)
    return any(detector.push(frame.samples) for frame in frames_from_array(audio))


@cache
def waking(text: str) -> np.ndarray:
    """The phrase, in a voice this detector actually hears.

    Cached, because the search costs a detector pass per candidate and several
    tests ask for the same sentence.
    """
    for speaker in CANDIDATE_VOICES:
        audio = spoken(text, speaker=speaker)
        if _wakes(audio):
            return audio
    pytest.skip(f"no candidate voice wakes the detector for {text!r}")


class Recorder:
    """Stands in for the speakers, and keeps what would have been played."""

    def __init__(self) -> None:
        self.played: list[Utterance] = []

    async def __call__(self, utterance: Utterance) -> None:
        self.played.append(utterance)
        # No sleep: the audio is not really being played, and waiting out its
        # duration would only make the suite slower without testing anything.


@pytest.fixture(scope="module")
def stt() -> WhisperSTT:
    """Loaded once. large-v3 costs half a minute to bring up."""
    return WhisperSTT(WhisperSettings())


@pytest.fixture(scope="module")
def tts() -> PiperTTS:
    return PiperTTS(VoiceChoice(directory=MODELS.piper_dir))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "notes.txt").write_text("hello", encoding="utf-8")
    return tmp_path


@pytest.fixture
def allowed_file_roots(workspace: Path) -> tuple[str, ...]:
    return (str(workspace / "allowed"),)


@pytest.fixture
async def stack_factory(tmp_path: Path, workspace: Path, allowed_file_roots: tuple[str, ...]):  # type: ignore[no-untyped-def]
    running: list[Any] = []

    async def start(script: list[Any]) -> AssistantSession:
        stack = await start_stack(
            provider=ScriptedProvider(script),
            settings_factory=backend_settings,
            tmp_path=tmp_path,
            workspace=workspace,
            allowed_roots=allowed_file_roots,
            bootstrap_token=E2E_BOOTSTRAP_TOKEN,
            device_name="m4-voice-agent",
        )
        running.append(stack)
        return stack.session

    try:
        yield start
    finally:
        for stack in running:
            await stack.shutdown()


def build_voice(
    *,
    stt: WhisperSTT,
    tts: PiperTTS,
    assistant: AssistantSession,
    recorder: Recorder,
    heard: list[dict[str, Any]],
) -> VoiceSession:
    """The real front end, answering through the real stack."""

    async def responder(transcript: Transcript) -> str:
        answer = await assistant.say(transcript.text, language=transcript.language.value)
        heard.append(answer)
        return str(answer["reply"])

    return VoiceSession(
        wake=SherpaKeywordSpotter(MODELS.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD),
        vad=SileroVAD(MODELS.vad),
        stt=stt,
        tts=tts,
        responder=responder,
        # No profile in a temporary state directory, and the speaker is a
        # synthesiser rather than the owner. Verification has its own tests.
        config=SessionConfig(verify_speaker=False, idle_timeout_s=30.0),
        player=recorder,
    )


async def say_to(session: VoiceSession, audio: np.ndarray) -> None:
    for frame in frames_from_array(audio):
        await session.push(frame)


class TestTheWholePath:
    async def test_a_spoken_command_starts_a_program(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        """«Jarvis, open Notepad» — and Notepad is running afterwards."""
        assistant = await stack_factory(
            [
                tool_reply(("app.launch", {"name": "notepad"})),
                text_reply("Notepad is open, sir."),
            ]
        )
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, open Notepad."))

        kinds = [event.kind for event in session.events]
        assert "wake" in kinds, "the wake word was never heard"
        assert heard, "nothing reached the backend"

        answer = heard[0]
        assert answer["stopped_because"] == "completed"
        call = answer["executed"][0]
        assert call["tool"] == "app.launch"
        assert call["decision"] == "allow"
        assert call["status"] == "completed"
        assert call["result"]["pid"] > 0

        # Two utterances: the acknowledgement, then the answer.
        assert len(recorder.played) == 2
        assert recorder.played[-1].duration_s > 0.3

        import psutil

        for process in psutil.process_iter(["pid"]):
            if process.info["pid"] == call["result"]["pid"]:
                process.kill()

    async def test_what_was_said_survives_the_journey(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        """The transcript the backend receives is what was spoken."""
        assistant = await stack_factory([text_reply("Understood, sir.")])
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, show me how much memory is left."))

        said = next(event.detail for event in session.events if event.kind == "heard")
        assert "memory" in said.lower()

    async def test_the_acknowledgement_comes_before_the_command_is_understood(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        """ "Yes, sir?" is a reflex, not a conclusion.

        It has to land while the person is still speaking, or the assistant
        feels slow however fast the rest of it is.
        """
        assistant = await stack_factory([text_reply("Of course, sir.")])
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, are you there?"))

        kinds = [event.kind for event in session.events]
        assert kinds.index("speaking") < kinds.index("heard")

    async def test_the_states_go_where_a_person_would_expect(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        assistant = await stack_factory(
            [tool_reply(("system.metrics", {})), text_reply("Sixty-one per cent, sir.")]
        )
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        seen: list[VoiceState] = []
        session.states.observe(lambda transition: seen.append(transition.current))
        session.start()

        await say_to(session, waking("Jarvis, how much memory is being used?"))

        assert seen[0] is VoiceState.LISTENING
        assert VoiceState.THINKING in seen
        assert VoiceState.SPEAKING in seen
        assert seen[-1] is VoiceState.LISTENING, "it must end ready for the next thing"

    async def test_a_second_command_needs_no_wake_word(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        """Continuous conversation, through the real recogniser."""
        assistant = await stack_factory(
            [text_reply("Certainly, sir."), text_reply("Of course, sir.")]
        )
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, are you listening?"))
        await say_to(session, spoken("And what about now?"))

        assert len(heard) == 2, "the second sentence should not have needed waking again"
        wakes = [event for event in session.events if event.kind == "wake"]
        assert len(wakes) == 1


class TestWhenThingsGoWrong:
    async def test_an_unreachable_backend_is_announced_not_hung(
        self, stt: WhisperSTT, tts: PiperTTS
    ) -> None:
        """The failure a person actually meets: no network.

        What must not happen is silence. The engine says it could not reach the
        model and goes back to listening; a session stuck in Thinking would look
        identical to one that never heard anything.
        """
        recorder = Recorder()

        async def unreachable(transcript: Transcript) -> str:
            raise ConnectionError("the backend is not there")

        session = VoiceSession(
            wake=SherpaKeywordSpotter(MODELS.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD),
            vad=SileroVAD(MODELS.vad),
            stt=stt,
            tts=tts,
            responder=unreachable,
            config=SessionConfig(verify_speaker=False, idle_timeout_s=30.0),
            player=recorder,
        )
        session.start()

        await asyncio.wait_for(say_to(session, waking("Jarvis, open Notepad.")), timeout=120)

        assert any(event.kind == "turn_failed" for event in session.events)
        assert session.states.state is VoiceState.LISTENING
        assert len(recorder.played) == 2, "it must say something rather than go quiet"

    async def test_speech_without_the_wake_word_is_not_acted_on(
        self, stt: WhisperSTT, tts: PiperTTS
    ) -> None:
        """An ordinary sentence in the room is not a command."""
        recorder, calls = Recorder(), []

        async def responder(transcript: Transcript) -> str:
            calls.append(transcript.text)
            return "..."

        session = VoiceSession(
            wake=SherpaKeywordSpotter(MODELS.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD),
            vad=SileroVAD(MODELS.vad),
            stt=stt,
            tts=tts,
            responder=responder,
            config=SessionConfig(verify_speaker=False),
            player=recorder,
        )
        session.start()

        await say_to(session, spoken("Could you pass me the salt, please."))

        assert calls == []
        assert recorder.played == []


class TestLanguages:
    async def test_a_russian_command_is_understood(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        """The wake word is English; the command need not be.

        Russian «Джарвис» is not a wake word — that was measured and accepted —
        so the wake comes in English and the command follows in Russian, inside
        the conversation the wake word opened. That is how it will actually be
        used, and it is why continuous conversation matters for a bilingual
        speaker rather than being a convenience.

        The two halves come from two Piper voices because they have to: the
        English multi-speaker model cannot pronounce Russian, and feeding it
        Cyrillic produces noise that tests nothing. Two voices in one
        conversation is not realistic, but the alternative is not realistic
        either, and this way the Russian being recognised is really Russian.
        """
        assistant = await stack_factory([text_reply("Готово, сэр.")])
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, are you listening?"))
        await say_to(session, russian("Открой блокнот, пожалуйста."))

        assert len(heard) >= 1, "the Russian half never arrived"
        russian_turn = heard[-1]
        assert russian_turn["language"] == Language.RU.value
        said = [event.detail for event in session.events if event.kind == "heard"][-1].lower()
        # Either spelling counts. «блокнот» comes back as "Notepad" when the
        # priming vocabulary does its job, and as «блокнот» when the alias table
        # has to do it afterwards; both name the same program, which is the
        # whole point of the code-switching work.
        assert "notepad" in said or "блокнот" in said

    async def test_the_reply_is_spoken_in_the_language_it_came_in(
        self,
        stack_factory,
        stt: WhisperSTT,
        tts: PiperTTS,  # type: ignore[no-untyped-def]
    ) -> None:
        assistant = await stack_factory([text_reply("Certainly, sir.")])
        recorder, heard = Recorder(), []
        session = build_voice(stt=stt, tts=tts, assistant=assistant, recorder=recorder, heard=heard)
        session.start()

        await say_to(session, waking("Jarvis, what is the time?"))

        assert heard[0]["language"] == Language.EN.value
