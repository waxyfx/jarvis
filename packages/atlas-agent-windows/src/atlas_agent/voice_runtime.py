"""The voice runtime: a microphone at one end, JARVIS at the other.

This is where the M4 engines meet the M3 stack. Everything below has been
exercised on its own — the wake word against 592 clips, the segmenter against
synthesised speech, the session against fakes — and this file is the wiring that
turns those pieces into something a person can talk to:

    microphone → wake word → VAD → segmenter → speaker check → Whisper
               → backend → Gemini → Policy Engine → agent → action
               → Piper → speakers

**The security boundary does not move.** What the voice path produces is *text*,
handed to the same ``/v1/assistant/message`` endpoint that typed input uses. It
gets no privileges typing does not have, and it cannot confirm anything: an
action the Policy Engine holds is reported aloud as waiting and stays waiting.
Speaker verification decides whose speech is listened to, never what may be done.

**Nothing about the owner's voice leaves the machine.** The profile stays local,
the audio stays local, and what crosses the network is the transcript — the same
sentence the person could have typed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from atlas_agent.backend import BackendClient
from atlas_agent.config import AgentSettings
from atlas_agent.identity import DeviceIdentity
from atlas_agent.logging import get_logger
from atlas_shared.enums import Language
from atlas_voice.capture import Microphone
from atlas_voice.engines.piper_tts import ACKNOWLEDGEMENTS, PiperTTS, VoiceChoice
from atlas_voice.engines.sherpa_kws import KeywordModel, SherpaKeywordSpotter
from atlas_voice.engines.silero import SileroVAD
from atlas_voice.engines.speaker import SherpaSpeaker
from atlas_voice.engines.whisper import WhisperSettings, WhisperSTT
from atlas_voice.playback import Loudspeaker
from atlas_voice.profile import VoiceProfileStore
from atlas_voice.providers import Transcript
from atlas_voice.session import SessionConfig, SessionEvent, VoiceSession

__all__ = ["BackendVoice", "VoiceModels", "VoiceRuntime", "build_runtime"]

log = get_logger(__name__)

#: The wake-word configuration that was measured, not a fresh guess. The zh-en
#: phoneme model at keywords_threshold 0.25 gave 3.08 false activations an hour
#: against openWakeWord's 780.75 on the same 592 clips, 0.0 on background noise,
#: and a median 0.233 s to fire. Changing any of it invalidates that acceptance.
WAKE_KEYWORDS: tuple[str, ...] = ("HEY JARVIS", "JARVIS")
WAKE_THRESHOLD = 0.25


@dataclass(frozen=True)
class VoiceModels:
    """Where the downloaded models live, and whether they are all there."""

    root: Path

    @property
    def wake(self) -> KeywordModel:
        return KeywordModel(
            directory=self.root / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
            encoder="encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            decoder="decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
            joiner="joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            tokenisation="phone",
        )

    @property
    def vad(self) -> Path:
        return self.root / "silero_vad.onnx"

    @property
    def speaker(self) -> Path:
        return self.root / "speaker" / "eres2net_base_sv.onnx"

    @property
    def piper_dir(self) -> Path:
        return self.root / "piper"

    def missing(self) -> tuple[str, ...]:
        """Which files are absent, so one message can name all of them.

        Reporting the first missing file and stopping means finding out about
        the next one only after a download, four times over.
        """
        wanted = {
            "Silero VAD": self.vad,
            "speaker embedding": self.speaker,
            "wake word": self.wake.directory,
            "English voice": self.piper_dir / f"{VoiceChoice().english}.onnx",
            "Russian voice": self.piper_dir / f"{VoiceChoice().russian}.onnx",
        }
        return tuple(f"{label} ({path})" for label, path in wanted.items() if not path.exists())


class BackendVoice:
    """Sends a transcript through the whole stack and returns what to say.

    The reply is whatever the backend says happened, which is deliberately not
    the same as what was asked for: an action the Policy Engine held is reported
    as waiting. Voice cannot confirm it — saying "yes" to a microphone is not
    the confirmation step, and making it one would move a security boundary that
    this file has no business moving.
    """

    def __init__(
        self,
        settings: AgentSettings,
        identity: DeviceIdentity,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._timeout = timeout_s
        self._client = BackendClient(settings)
        self._token: str | None = None
        self.last: dict[str, object] | None = None

    async def _authenticate(self) -> str:
        self._token = await self._client.authenticate(self._identity)
        return self._token

    async def __call__(self, transcript: Transcript) -> str:
        token = self._token or await self._authenticate()
        try:
            answer = await self._post(transcript, token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 401:
                raise
            # Expired rather than wrong: a fresh token is cheap. Retried once,
            # because a second failure is a real problem and looping on it would
            # hide it behind a stutter.
            answer = await self._post(transcript, await self._authenticate())

        self.last = answer
        return _spoken_reply(answer, transcript.language)

    async def _post(self, transcript: Transcript, token: str) -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url=self._settings.backend_url,
            timeout=self._timeout,
            verify=self._settings.verify_tls,
        ) as client:
            response = await client.post(
                "/v1/assistant/message",
                json={"text": transcript.text, "language": transcript.language.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return dict(response.json())


def _spoken_reply(answer: dict[str, object], language: Language) -> str:
    """What to read out, given what the backend reported.

    The model's own words carry the answer. The one thing added is the state of
    anything left waiting, because that is invisible over audio: on screen a
    pending action is a row with a button, and spoken aloud it is silence.
    """
    reply = str(answer.get("reply", "")).strip()
    pending = answer.get("pending_confirmation")
    if isinstance(pending, list) and pending:
        note = _AWAITING[language].format(count=len(pending))
        reply = f"{reply} {note}".strip()
    return reply


_AWAITING = {
    Language.EN: "That needs your confirmation, sir — it is waiting.",
    Language.RU: "Это требует вашего подтверждения, сэр — ожидает.",
}


@dataclass
class VoiceRuntime:
    """A microphone, a session and a pair of speakers, wired together."""

    session: VoiceSession
    microphone: Microphone
    loudspeaker: Loudspeaker
    models: VoiceModels
    events: list[SessionEvent] = field(default_factory=list)

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        """Pump frames until told to stop. The whole runtime, one loop.

        Frames arrive on the sound library's thread and are read here through a
        queue, so the work is on this side of it: the callback stays short and a
        slow model shows up as dropped blocks rather than a corrupted stream.
        """
        stop = stop or asyncio.Event()
        self.session.start()
        frames = self.microphone.frames()

        try:
            while not stop.is_set():
                frame = await asyncio.to_thread(next, frames, None)
                if frame is None:
                    break
                await self.session.push(frame)
        finally:
            self.microphone.stop()
            self.loudspeaker.stop()
            if self.microphone.dropped_blocks:
                log.warning("voice_dropped_blocks", count=self.microphone.dropped_blocks)

    def stop(self) -> None:
        self.microphone.stop()
        self.loudspeaker.stop()


async def build_runtime(
    *,
    settings: AgentSettings,
    identity: DeviceIdentity,
    store: VoiceProfileStore,
    models: VoiceModels,
    input_device: int | None = None,
    output_device: int | None = None,
    responder: Callable[[Transcript], Awaitable[str]] | None = None,
    config: SessionConfig | None = None,
    on_event: Callable[[SessionEvent], None] | None = None,
) -> VoiceRuntime:
    """Load every model and connect it up.

    Slow, and deliberately so: every load happens here rather than on first
    use, because the alternative is a wake word that fires and then waits
    several seconds for Whisper to appear.
    """
    absent = models.missing()
    if absent:
        raise FileNotFoundError(
            "voice models are missing; run scripts/fetch_voice_models.ps1 — " + ", ".join(absent)
        )

    speaker = SherpaSpeaker(models.speaker, store=store)
    has_profile = store.exists()
    # One device, shared: the runtime's stop() has to reach the same stream
    # the session is playing through, or a shutdown mid-sentence keeps talking.
    loudspeaker = Loudspeaker(device=output_device)

    tts = PiperTTS(
        VoiceChoice(directory=models.piper_dir),
        # "Yes, sir?" is said on every single wake. Rendering it fresh each time
        # puts a Piper load between the wake word and the acknowledgement, which
        # is the one place in the whole path where delay is felt.
        cache_phrases=tuple(phrase for phrases in ACKNOWLEDGEMENTS.values() for phrase in phrases),
    )
    await tts.warm()

    session = VoiceSession(
        wake=SherpaKeywordSpotter(models.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD),
        vad=SileroVAD(models.vad),
        stt=WhisperSTT(WhisperSettings()),
        tts=tts,
        speaker=speaker,
        responder=responder or BackendVoice(settings, identity),
        config=config
        or SessionConfig(
            # Off with no profile enrolled: refusing every command until someone
            # has registered would make the assistant unusable before it can be
            # set up, and the Policy Engine is what actually guards the actions.
            verify_speaker=has_profile
        ),
        player=loudspeaker.play,
        on_event=on_event,
    )

    return VoiceRuntime(
        session=session,
        microphone=Microphone(device=input_device),
        loudspeaker=loudspeaker,
        models=models,
    )
