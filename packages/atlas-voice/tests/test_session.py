"""The conversation state machine, driven by fake engines.

Every provider is a protocol, so the whole session runs on stand-ins that do
exactly what a test needs. That is the point of the abstraction: these
behaviours — does it stay open, does it stop talking when interrupted, does mute
actually stop the audio — are about *timing and control flow*, and testing them
through real models would measure the models instead.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from atlas_shared.enums import Language
from atlas_voice.audio import FRAME_SAMPLES, Frame, frames_from_array
from atlas_voice.providers import (
    Detection,
    Transcript,
    Utterance,
    VerificationResult,
    Voice,
    VoiceEngineError,
)
from atlas_voice.segmenter import SegmenterConfig
from atlas_voice.session import SessionConfig, VoiceSession
from atlas_voice.state import Actor, VoiceState

SR = 16_000


class FakeWake:
    """Fires once, on a chosen frame, so a test controls when the word lands.

    Firing *once* matters. An earlier version restarted its counter on reset,
    so every time the session closed on idle the word immediately "fired"
    again and the conversation reopened — which looked exactly like a session
    that never closes. The fake was wrong, not the session.
    """

    name = "fake-wake"

    def __init__(self, fire_after: int = 0) -> None:
        self.fire_after = fire_after
        self.pushes = 0
        self.resets = 0
        self.fired = False

    def push(self, samples: np.ndarray) -> Detection | None:
        self.pushes += 1
        if self.fired or self.pushes != self.fire_after:
            return None
        self.fired = True
        return Detection(score=1.0, at=self.pushes * 0.032, label="jarvis")

    def reset(self) -> None:
        self.resets += 1


class ScriptedVAD:
    """Speech or silence, read off a string: `#` speech, `.` silence."""

    name = "scripted-vad"

    def __init__(self, pattern: str = "") -> None:
        self.pattern = pattern
        self.index = 0
        self.resets = 0

    def is_speech(self, samples: np.ndarray) -> bool:
        speaking = self.index < len(self.pattern) and self.pattern[self.index] == "#"
        self.index += 1
        return speaking

    def reset(self) -> None:
        self.resets += 1


class FakeSTT:
    name = "fake-stt"
    model = "fake"

    def __init__(self, text: str = "открой хром", language: Language = Language.RU) -> None:
        self.text = text
        self.language = language
        self.calls = 0
        self.fail = False

    async def transcribe(self, samples: np.ndarray, *, hint: Language | None = None) -> Transcript:
        self.calls += 1
        if self.fail:
            raise VoiceEngineError("recogniser unavailable")
        return Transcript(text=self.text, language=self.language, duration_s=len(samples) / SR)


class FakeTTS:
    name = "fake-tts"

    def __init__(self, seconds: float = 0.2) -> None:
        self.seconds = seconds
        self.spoken: list[str] = []

    async def synthesise(self, text: str, *, language: Language) -> Utterance:
        self.spoken.append(text)
        return Utterance(
            samples=np.zeros(int(self.seconds * SR), dtype=np.float32),
            sample_rate=SR,
            voice=Voice.RU if language is Language.RU else Voice.EN,
        )


class FakeSpeaker:
    name = "fake-speaker"

    def __init__(self, *, accept: bool = True, enrolled: bool = True) -> None:
        self.accept = accept
        self.enrolled = enrolled
        self.calls = 0

    def embed(self, samples: np.ndarray) -> np.ndarray:
        return np.zeros(192, dtype=np.float32)

    def verify(self, samples: np.ndarray) -> VerificationResult:
        self.calls += 1
        if not self.enrolled:
            raise VoiceEngineError("no voice profile enrolled")
        return VerificationResult(accepted=self.accept, score=0.9, threshold=0.5)


def frames(count: int) -> list[Frame]:
    return list(frames_from_array(np.zeros(count * FRAME_SAMPLES, dtype=np.float32)))


async def instant(utterance: Utterance) -> None:
    """Playback that finishes at once.

    Used wherever the test is about something other than speaking. Waiting out
    real audio would make every test slow and would measure asyncio rather than
    the session.
    """
    return None


def never_ending(utterance: Utterance):  # type: ignore[no-untyped-def]
    """Playback that only stops when cancelled — which is what barge-in does."""

    async def play(_: Utterance) -> None:
        await asyncio.Event().wait()

    return play


def build(
    *,
    wake_at: int = 1,
    pattern: str = "",
    tts_seconds: float = 0.05,
    speaker: FakeSpeaker | None = None,
    config: SessionConfig | None = None,
    responder=None,  # type: ignore[no-untyped-def]
    player=None,  # type: ignore[no-untyped-def]
) -> tuple[VoiceSession, FakeWake, ScriptedVAD, FakeSTT, FakeTTS]:
    wake, vad = FakeWake(wake_at), ScriptedVAD(pattern)
    stt, tts = FakeSTT(), FakeTTS(tts_seconds)

    async def default(transcript: Transcript) -> str:
        return "Готово."

    session = VoiceSession(
        wake=wake,
        vad=vad,
        stt=stt,
        tts=tts,
        speaker=speaker,
        responder=responder or default,
        player=player or instant,
        config=config
        or SessionConfig(
            idle_timeout_s=0.5,
            segmenter=SegmenterConfig(
                start_after_ms=64, end_after_ms=128, preroll_ms=64, min_utterance_ms=64
            ),
        ),
    )
    session.start()
    return session, wake, vad, stt, tts


async def feed(session: VoiceSession, count: int) -> None:
    for frame in frames(count):
        await session.push(frame)


class TestWakingUp:
    async def test_nothing_happens_before_the_wake_word(self) -> None:
        session, _, _, stt, tts = build(wake_at=99)

        await feed(session, 10)

        assert not session.conversation_open
        assert stt.calls == 0
        assert tts.spoken == []

    async def test_the_wake_word_opens_a_conversation_and_answers(self) -> None:
        session, _, _, _, tts = build(wake_at=2)

        await feed(session, 3)

        assert session.conversation_open
        assert tts.spoken == ["Yes, sir?"]
        assert any(event.kind == "wake" for event in session.events)


class TestContinuousConversation:
    async def test_a_second_command_needs_no_wake_word(self) -> None:
        """«Jarvis.» → «Открой Chrome.» → «А теперь покажи память.»"""
        pattern = "." + "####" + "......" + "####" + "......"
        session, wake, _, stt, _ = build(wake_at=1, pattern=pattern)

        await feed(session, len(pattern))

        assert stt.calls == 2, "the second utterance should not need waking again"
        assert wake.pushes == 0 or session.conversation_open

    async def test_silence_closes_the_session(self) -> None:
        session, _, _, _, _ = build(wake_at=1, pattern="." + "." * 40)

        await feed(session, 41)

        assert not session.conversation_open
        assert any(
            event.kind == "conversation_closed" and event.detail == "idle"
            for event in session.events
        )

    async def test_after_closing_the_wake_word_is_required_again(self) -> None:
        session, wake, _, _, _ = build(wake_at=1, pattern="." + "." * 40)

        await feed(session, 41)
        assert not session.conversation_open
        assert wake.resets >= 1, "the detector must be cleared so a stale hit cannot reopen it"


class TestBargeIn:
    async def test_speech_during_playback_stops_it(self) -> None:
        """A long reply must be interruptible, or it has to be waited out."""
        config = SessionConfig(idle_timeout_s=5.0, bargein_after_ms=64)
        session, _, _, _, _ = build(
            wake_at=1, pattern="." + "#" * 60, config=config, player=never_ending(None)
        )

        await feed(session, 11)

        assert any(event.kind == "barge_in" for event in session.events)
        assert session.states.state is VoiceState.LISTENING

    async def test_a_single_noisy_frame_does_not_interrupt(self) -> None:
        """A cough should not stop the assistant mid-sentence."""
        config = SessionConfig(idle_timeout_s=5.0, bargein_after_ms=300)
        session, _, _, _, _ = build(
            wake_at=1, pattern=".#........", config=config, player=never_ending(None)
        )

        await feed(session, 10)

        assert not any(event.kind == "barge_in" for event in session.events)

    async def test_the_microphone_stays_open_while_speaking(self) -> None:
        session, _, _, _, _ = build(wake_at=1, player=never_ending(None))
        await feed(session, 2)

        assert session.states.state is VoiceState.SPEAKING
        assert session.states.microphone_is_open


class TestMute:
    async def test_muted_audio_reaches_no_engine(self) -> None:
        session, wake, _, stt, _ = build(wake_at=1, pattern="####")
        session.mute()
        before = wake.pushes

        await feed(session, 8)

        assert wake.pushes == before, "no audio may be decoded while muted"
        assert stt.calls == 0

    async def test_mute_closes_an_open_conversation(self) -> None:
        session, _, _, _, _ = build(wake_at=1)
        await feed(session, 2)
        assert session.conversation_open

        session.mute()

        assert not session.conversation_open

    async def test_a_remote_actor_cannot_unmute(self) -> None:
        from atlas_voice.state import IllegalTransitionError

        session, _, _, _, _ = build(wake_at=1)
        session.mute()

        with pytest.raises(IllegalTransitionError):
            session.unmute(actor=Actor.REMOTE)
        assert session.states.is_muted

    async def test_the_local_user_can_unmute_and_listening_resumes(self) -> None:
        session, _, _, _, _ = build(wake_at=1)
        session.mute()

        session.unmute(actor=Actor.LOCAL)

        assert session.states.state is VoiceState.LISTENING


class TestFailures:
    async def test_an_unreachable_model_returns_to_listening(self) -> None:
        """The failure the user notices is an assistant stuck thinking."""

        async def explode(transcript: Transcript) -> str:
            raise RuntimeError("backend unreachable")

        session, _, _, _, tts = build(wake_at=1, pattern="." + "####" + "......", responder=explode)

        await feed(session, 11)

        assert session.states.state is VoiceState.LISTENING
        assert any(event.kind == "turn_failed" for event in session.events)
        assert any("модел" in line for line in tts.spoken), "the user must be told"

    async def test_a_failing_recogniser_does_not_wedge_the_session(self) -> None:
        session, _, _, stt, _ = build(wake_at=1, pattern="." + "####" + "......")
        stt.fail = True

        await feed(session, 11)

        assert session.states.state is VoiceState.LISTENING
        assert any(event.kind == "stt_failed" for event in session.events)


class TestSpeakerVerification:
    async def test_a_stranger_is_not_acted_on(self) -> None:
        speaker = FakeSpeaker(accept=False)
        config = SessionConfig(
            idle_timeout_s=5.0,
            verify_speaker=True,
            segmenter=SegmenterConfig(
                start_after_ms=64, end_after_ms=128, preroll_ms=64, min_utterance_ms=64
            ),
        )
        session, _, _, stt, _ = build(
            wake_at=1, pattern="." + "####" + "......", speaker=speaker, config=config
        )

        await feed(session, 11)

        assert stt.calls == 0
        assert any(event.kind == "rejected" for event in session.events)

    async def test_without_a_profile_the_assistant_still_works(self) -> None:
        """Refusing everything before enrollment would make it unusable, and
        the Policy Engine — not this — is what guards the actions."""
        speaker = FakeSpeaker(enrolled=False)
        config = SessionConfig(
            idle_timeout_s=5.0,
            verify_speaker=True,
            segmenter=SegmenterConfig(
                start_after_ms=64, end_after_ms=128, preroll_ms=64, min_utterance_ms=64
            ),
        )
        session, _, _, stt, _ = build(
            wake_at=1, pattern="." + "####" + "......", speaker=speaker, config=config
        )

        await feed(session, 11)

        assert stt.calls == 1


class TestExecutingState:
    """The one state the engine cannot observe for itself.

    From inside the session a turn that launches a program and a turn that
    merely answers are the same thing: a wait on the responder. The agent's
    tool runner is what knows the difference, so it reports it — and without
    that report the person watches "Thinking" while a window opens in front of
    them.
    """

    async def test_a_running_tool_shows_as_executing(self) -> None:
        seen: list[VoiceState] = []

        async def responder(transcript: Transcript) -> str:
            session.note_executing()
            seen.append(session.states.state)
            session.note_executed()
            seen.append(session.states.state)
            return "Готово."

        session, *_ = build(wake_at=1, pattern="." + "####" + "......", responder=responder)
        await feed(session, 11)

        assert seen == [VoiceState.EXECUTING, VoiceState.THINKING]

    async def test_several_tools_do_not_end_early(self) -> None:
        """The first to finish must not clear a state the others still need."""
        seen: list[VoiceState] = []

        async def responder(transcript: Transcript) -> str:
            session.note_executing()
            session.note_executing()
            session.note_executed()
            seen.append(session.states.state)
            session.note_executed()
            seen.append(session.states.state)
            return "Готово."

        session, *_ = build(wake_at=1, pattern="." + "####" + "......", responder=responder)
        await feed(session, 11)

        assert seen == [VoiceState.EXECUTING, VoiceState.THINKING]

    async def test_a_tool_outside_a_turn_is_ignored(self) -> None:
        """The backend can run tools for reasons unrelated to this conversation."""
        session, *_ = build()

        session.note_executing()

        assert session.states.state is VoiceState.LISTENING

    async def test_an_unreported_finish_does_not_strand_the_next_turn(self) -> None:
        """A crash between start and finish would otherwise wedge the display."""
        turns: list[VoiceState] = []

        async def responder(transcript: Transcript) -> str:
            if not turns:
                session.note_executing()  # and never reports the end
            turns.append(session.states.state)
            return "Готово."

        session, *_ = build(
            wake_at=1, pattern="." + "####" + "......" + "####" + "......", responder=responder
        )
        await feed(session, 21)

        assert turns[0] is VoiceState.EXECUTING
        assert turns[-1] is VoiceState.THINKING
