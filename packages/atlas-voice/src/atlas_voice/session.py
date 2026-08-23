"""The conversation: from a wake word to a spoken answer, and back again.

This is the part that decides *when* to listen, not *what was said*. Every
engine is behind a protocol, so the whole thing is driven from fake providers in
tests and from real ones in the agent, with no branch to tell them apart.

Three behaviours are the reason it exists.

**Continuous conversation.** Say the wake word once and the session stays open:
«Jarvis.» — "Yes, sir?" — «Открой Chrome.» — «А теперь покажи память.» The
second command needs no wake word. The session closes after a stretch of
silence, and then it does.

**Barge-in.** The microphone stays open while the assistant is speaking. Speech
from the owner stops playback mid-word and starts listening. An assistant that
has to be waited out is worse than one that says less.

**Mute outranks everything.** It is the agent's own switch, it is checked before
any audio is processed, and nothing arriving over the network can lift it. Same
rule as SAFE MODE, for the same reason.

The security boundary is elsewhere and stays there: this produces *text* and
hands it to the M3 endpoint, which is the same path typed input takes. Speaker
verification decides whose speech is listened to, never what may be done — a
MEDIUM action still requires the confirmation it always required.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE, Frame, RingBuffer
from atlas_voice.providers import (
    Detection,
    SpeakerProvider,
    STTProvider,
    Transcript,
    TTSProvider,
    Utterance,
    VADProvider,
    VoiceEngineError,
    WakeWordProvider,
)
from atlas_voice.segmenter import SegmenterConfig, SpeechSegmenter
from atlas_voice.state import Actor, VoiceState, VoiceStateMachine

__all__ = ["SessionConfig", "SessionEvent", "VoiceSession"]

#: Given the text of a command, produce the reply to speak. This is where the
#: M3 pipeline plugs in: backend, Gemini, Policy Engine, agent, result.
Responder = Callable[[Transcript], Awaitable[str]]

#: Sends rendered audio to a device. The default merely waits out the duration,
#: which is enough to drive the state machine; the Windows agent substitutes a
#: real device write. It must be cancellable — that is what barge-in stops.
Player = Callable[[Utterance], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class SessionConfig:
    #: How long the session stays open after the last thing anyone said.
    idle_timeout_s: float = 25.0
    #: Audio kept before the wake word fires, so a command spoken in the same
    #: breath is not lost: "Jarvis, закрой Notepad" is one utterance.
    preroll_s: float = 3.0
    #: Speech must persist this long during playback before it counts as an
    #: interruption. A cough should not stop the assistant mid-sentence.
    bargein_after_ms: float = 200.0
    #: Speaker verification is a filter on whose speech is acted on. Off until a
    #: profile exists, and never a substitute for the Policy Engine.
    verify_speaker: bool = False
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)


@dataclass(frozen=True)
class SessionEvent:
    """Something worth telling the tray, the log or a test about."""

    kind: str
    detail: str = ""
    at: float = 0.0


class VoiceSession:
    """Drives one microphone stream. Feed it frames; it does the rest."""

    def __init__(
        self,
        *,
        wake: WakeWordProvider,
        vad: VADProvider,
        stt: STTProvider,
        tts: TTSProvider,
        responder: Responder,
        speaker: SpeakerProvider | None = None,
        config: SessionConfig | None = None,
        player: Player | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        self._wake = wake
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._speaker = speaker
        self._respond = responder
        self._config = config or SessionConfig()
        self._player = player or _wait_out

        self.states = VoiceStateMachine()
        self._segmenter = SpeechSegmenter(self._config.segmenter)
        self._preroll = RingBuffer(seconds=self._config.preroll_s)
        self._events: list[SessionEvent] = []
        self._on_event = on_event

        self._open = False
        self._last_activity = 0.0
        self._elapsed = 0.0
        self._speech_during_playback = 0
        #: Set while the assistant is talking, so barge-in has something to stop.
        self._playback: asyncio.Task[None] | None = None
        #: How many tools are running. Counted rather than flagged, because one
        #: turn can run several and the first to finish must not clear the state
        #: while the others are still going.
        self._executing = 0
        #: Recent frames with the answer the VAD already gave for each. Kept so
        #: a rewind can replay them without asking Silero twice — it is stateful,
        #: and feeding it the same audio again would corrupt what it thinks it
        #: has heard.
        self._recent: deque[tuple[Frame, bool]] = deque(
            maxlen=max(1, int(self._config.preroll_s * SAMPLE_RATE / FRAME_SAMPLES))
        )
        #: Frames from before the wake word that still have to go through the
        #: segmenter. Held, not pushed immediately, because the acknowledgement
        #: is playing and the segmenter belongs to the listening state.
        self._rewound: list[tuple[Frame, bool]] = []

    # ------------------------------------------------------------------ state

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def conversation_open(self) -> bool:
        return self._open

    def _emit(self, kind: str, detail: str = "") -> None:
        event = SessionEvent(kind=kind, detail=detail, at=self._elapsed)
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def start(self) -> None:
        self.states.to(VoiceState.LISTENING)

    def mute(self, *, actor: Actor = Actor.LOCAL) -> None:
        self.states.mute(actor=actor)
        self._close_conversation("muted")
        self._emit("muted", str(actor))

    def unmute(self, *, actor: Actor = Actor.LOCAL) -> None:
        """Local only. The state machine refuses anything else."""
        self.states.unmute(actor=actor)
        self._reset_listening()
        self._emit("unmuted", str(actor))

    def note_executing(self) -> None:
        """A tool has started running on this machine.

        Called by the agent's tool runner, because the voice engine cannot see
        execution — from here a turn that runs a program and a turn that merely
        answers look identical, and both are simply a wait. Without this the
        Executing state would be unreachable and the person would watch
        "Thinking" while a program opened in front of them.

        Ignored unless a turn is in flight: a tool the backend triggered for
        some other reason must not repaint this conversation.
        """
        if self.states.state is VoiceState.THINKING or self._executing:
            self._executing += 1
            if self.states.state is not VoiceState.EXECUTING:
                self.states.to(VoiceState.EXECUTING)
                self._emit("executing", "")

    def note_executed(self) -> None:
        """That tool has finished. The turn goes back to waiting on the model."""
        if not self._executing:
            return
        self._executing -= 1
        if self._executing == 0 and self.states.state is VoiceState.EXECUTING:
            self.states.to(VoiceState.THINKING)

    # ----------------------------------------------------------------- audio

    async def push(self, frame: Frame) -> None:
        """Feed one 32 ms frame. The only entry point."""
        if self.states.is_muted:
            # Not merely ignored: nothing is buffered, nothing is decoded, and
            # no audio reaches any model while muted.
            return

        self._elapsed = frame.ends_at
        self._preroll.push(frame)
        speaking = self._vad.is_speech(frame.samples)
        self._recent.append((frame, speaking))

        if self.states.state is VoiceState.SPEAKING:
            await self._maybe_interrupt(speaking)
            return

        if not self._open:
            await self._listen_for_wake(frame)
            return

        await self._listen_for_command(frame, speaking)

    async def _listen_for_wake(self, frame: Frame) -> None:
        detection = self._wake.push(frame.samples)
        if detection is None:
            return
        if not self._verified(self._preroll.tail(seconds=1.5), detection):
            # Someone else said the wake word. Worth recording: silence here
            # looks identical to a detector that never fired.
            self._emit("rejected", "speaker not recognised at wake")
            return

        self._open = True
        self._last_activity = self._elapsed
        self._segmenter.reset()
        # Everything back to the start of the sentence being spoken. If the
        # command came in the same breath as the wake word, it is in here.
        cutoff = max(0.0, self._sentence_started_at() - _LEAD_IN_S)
        self._rewound = [pair for pair in self._recent if pair[0].started_at >= cutoff]
        self._emit("wake", f"{detection.label} at {detection.at:.2f}s")
        await self._acknowledge()

    async def _listen_for_command(self, frame: Frame, speaking: bool) -> None:
        if self._rewound:
            rewound, self._rewound = self._rewound, []
            for old, was_speech in rewound:
                chunk = self._segmenter.push(old, is_speech=was_speech)
                if chunk is None:
                    continue
                # The whole command was already spoken by the time the wake word
                # was recognised. Nothing more is coming; handle it now.
                await self._handle(chunk.samples)
                return

        if speaking:
            self._last_activity = self._elapsed

        chunk = self._segmenter.push(frame, is_speech=speaking)
        if chunk is not None:
            await self._handle(chunk.samples)
            return

        if self._elapsed - self._last_activity >= self._config.idle_timeout_s:
            self._close_conversation("idle")

    async def _handle(self, samples: np.ndarray) -> None:
        # Any leftover from a previous turn is stale by now. Without this a tool
        # whose completion was never reported would leave every later turn
        # stuck showing Executing.
        self._executing = 0
        if not self._verified(samples, None):
            self._emit("rejected", "speaker not recognised")
            return

        self.states.to(VoiceState.THINKING)
        try:
            transcript = await self._stt.transcribe(samples)
        except VoiceEngineError as error:
            self.states.to(VoiceState.LISTENING)
            self._emit("stt_failed", type(error).__name__)
            return

        command = _without_wake_word(transcript.text)
        if transcript.is_empty or not command:
            # A transcript that was only the wake word is someone getting the
            # assistant's attention. It was already acknowledged; answering it
            # again would be talking to oneself.
            self.states.to(VoiceState.LISTENING)
            self._emit("empty", transcript.text[:40])
            return

        transcript = replace(transcript, text=command)
        self._emit("heard", transcript.text)
        try:
            reply = await self._respond(transcript)
        except Exception as error:
            # An unreachable model is an ordinary condition for an assistant.
            # The engine says so and goes back to listening rather than hanging
            # in Thinking forever, which is the failure the user would actually
            # notice.
            self.states.to(VoiceState.LISTENING)
            self._emit("turn_failed", type(error).__name__)
            await self._say(_APOLOGY[transcript.language], transcript.language)
            return

        await self._say(reply, transcript.language)

    # -------------------------------------------------------------- speaking

    async def _acknowledge(self) -> None:
        await self._say(_ACKNOWLEDGEMENT[Language.EN], Language.EN, brief=True)

    async def _say(self, text: str, language: Language, *, brief: bool = False) -> None:
        """Start speaking and return immediately.

        Deliberately does *not* wait for playback. The first version awaited it
        here, inside ``push``, which meant no frames were read while the
        assistant talked — so barge-in could not happen at all, however
        carefully the interrupt logic was written. An assistant that cannot be
        interrupted has to be waited out.
        """
        if not text.strip():
            self._reset_listening()
            return

        try:
            utterance = await self._tts.synthesise(text, language=language)
        except VoiceEngineError as error:
            self._emit("tts_failed", type(error).__name__)
            self._reset_listening()
            return

        if self.states.state is not VoiceState.SPEAKING:
            self.states.to(VoiceState.SPEAKING)
        self._speech_during_playback = 0
        self._emit("speaking", text if brief else text[:80])
        self._playback = asyncio.create_task(self._player(utterance))
        # Let the task reach its first await, so a playback that finishes
        # instantly is already done by the next frame rather than leaving the
        # session stuck in Speaking with nothing playing.
        await asyncio.sleep(0)

    async def _finish_speaking(self, *, interrupted: bool) -> None:
        task, self._playback = self._playback, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if interrupted:
            self._emit("barge_in", "")
        self._last_activity = self._elapsed
        self._reset_listening()

    async def _maybe_interrupt(self, speaking: bool) -> None:
        if self._playback is not None and self._playback.done():
            await self._finish_speaking(interrupted=False)
            return

        if not speaking:
            self._speech_during_playback = 0
            return

        self._speech_during_playback += 1
        needed = max(1, round(self._config.bargein_after_ms / (FRAME_SAMPLES / SAMPLE_RATE * 1000)))
        if self._speech_during_playback < needed:
            return

        await self._finish_speaking(interrupted=True)

    # ------------------------------------------------------------- internals

    def _sentence_started_at(self) -> float:
        """When the sentence now being spoken began.

        Rewinding a fixed distance was the first attempt and it does not work:
        the detector reports when it *decided*, which trails the word itself by
        however long it took to be sure, and that varies with the voice. Half a
        second was enough for one synthetic speaker and cut another off
        mid-phrase, handing Whisper a fragment — which it answered by inventing
        a fluent Russian sentence, because that is what Whisper does with
        fragments. Walking back to the last real pause does not care how slow
        the detector was.
        """
        gap_frames = max(1, int(_SENTENCE_GAP_S * SAMPLE_RATE / FRAME_SAMPLES))
        start = self._elapsed
        silent = 0
        for frame, speaking in reversed(self._recent):
            if speaking:
                silent = 0
                start = frame.started_at
                continue
            silent += 1
            if silent > gap_frames:
                break
        return start

    def _verified(self, samples: np.ndarray, detection: Detection | None) -> bool:
        """Whose speech this is. Never whether the command is allowed."""
        if not self._config.verify_speaker or self._speaker is None:
            return True
        try:
            return self._speaker.verify(samples).accepted
        except VoiceEngineError:
            # No profile enrolled. Refusing every command would make the
            # assistant unusable before enrollment; accepting is safe because
            # the Policy Engine is what actually guards the actions.
            return True

    def _reset_listening(self) -> None:
        if self.states.state is not VoiceState.LISTENING:
            self.states.to(VoiceState.LISTENING)
        self._segmenter.reset()
        self._vad.reset()

    def _close_conversation(self, why: str) -> None:
        if not self._open:
            return
        self._open = False
        self._rewound.clear()
        self._segmenter.reset()
        self._wake.reset()
        self._emit("conversation_closed", why)


async def _wait_out(utterance: Utterance) -> None:
    """The default player: occupy exactly as long as the audio lasts."""
    await asyncio.sleep(utterance.duration_s)


#: A pause this long ends a sentence, for the purpose of deciding how far to
#: rewind. Short enough not to swallow the previous thing said, long enough to
#: survive the gap between "Jarvis," and the command that follows it.
_SENTENCE_GAP_S = 0.35
#: Included ahead of the rewind point, so the first phoneme is not clipped.
_LEAD_IN_S = 0.2

#: Stripped from the front of a command. Said in one breath, the wake word is
#: part of the sentence Whisper transcribes, and "Jarvis, open Chrome" should
#: reach the model as "open Chrome". A transcript with nothing else in it was
#: someone getting the assistant's attention, not asking for anything.
_WAKE_WORDS = ("jarvis", "hey jarvis", "джарвис", "эй джарвис")


def _without_wake_word(text: str) -> str:
    """Drop a leading "Jarvis," from a command, keeping everything after it."""
    stripped = text.strip()
    for word in sorted(_WAKE_WORDS, key=len, reverse=True):
        match = re.match(rf"^{word}\b[\s,.!?—-]*", stripped, flags=re.IGNORECASE)
        if match:
            return stripped[match.end() :].strip()
    return stripped


_ACKNOWLEDGEMENT = {Language.EN: "Yes, sir?", Language.RU: "Да, сэр?"}
_APOLOGY = {
    Language.EN: "I could not reach the model, sir.",
    Language.RU: "Не удалось связаться с моделью, сэр.",
}
