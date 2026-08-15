"""The five provider contracts the voice engine talks through.

Same principle as :class:`~atlas_backend.ai.provider.AIProvider` in M3: the
session state machine knows these protocols and nothing else, so replacing
Whisper with something else, or Kokoro with Azure, is writing one class rather
than editing the pipeline.

**None of these is part of the security boundary.** A wake-word detector that
fires on the television, a recogniser that mishears, a verifier that accepts a
recording — none of them can cause an action. They produce *text*, which travels
the same M3 path as typed text and meets the same Policy Engine. That is why
speaker verification is a filter on whose speech is listened to, and not a
credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np

from atlas_shared.enums import Language

__all__ = [
    "Detection",
    "STTProvider",
    "SpeakerProvider",
    "SpeechChunk",
    "TTSProvider",
    "Transcript",
    "VADProvider",
    "VerificationResult",
    "VoiceEngineError",
    "WakeWordProvider",
]


class VoiceEngineError(RuntimeError):
    """A stage could not do its job. Never contains audio or credentials."""


# ------------------------------------------------------------------ wake word


@dataclass(frozen=True, slots=True)
class Detection:
    """A wake-word hit, before anything has been decided about it."""

    score: float
    #: When the wake word finished, in stream time.
    at: float
    label: str = "atlas"


@runtime_checkable
class WakeWordProvider(Protocol):
    name: str

    def push(self, samples: np.ndarray) -> Detection | None:
        """Feed audio; return a detection when one fires.

        Implementations buffer internally to whatever window they need. They are
        stateful and must be fed every frame in order.
        """
        ...

    def reset(self) -> None:
        """Forget buffered audio, so one utterance cannot fire twice."""
        ...


# ------------------------------------------------------------------------ VAD


@dataclass(frozen=True, slots=True)
class SpeechChunk:
    """A complete utterance, bounded by silence."""

    samples: np.ndarray
    started_at: float
    ended_at: float

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at


@runtime_checkable
class VADProvider(Protocol):
    name: str

    def is_speech(self, samples: np.ndarray) -> bool:
        """Whether this window contains speech."""
        ...

    def reset(self) -> None: ...


# ------------------------------------------------------------------------ STT


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: Language
    #: The recogniser's own confidence in the language it picked, when it
    #: reports one. Low confidence is a reason to ask, not to guess.
    language_confidence: float = 0.0
    #: Text before normalisation, kept for diagnosis when an alias misfires.
    raw_text: str = ""
    duration_s: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class STTProvider(Protocol):
    name: str
    model: str

    async def transcribe(self, samples: np.ndarray, *, hint: Language | None = None) -> Transcript:
        """Turn audio into text, or raise :class:`VoiceEngineError`.

        ``hint`` biases language detection; it must not force it, because the
        whole point of automatic detection is that the user switches languages
        without announcing it.
        """
        ...


# ---------------------------------------------------------------- speaker id


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """How much this audio sounds like the enrolled owner.

    ``accepted`` is a *filter* decision, never an authorisation. It says whose
    speech to listen to. Whether the resulting command runs is the Policy
    Engine's business, exactly as for typed text.
    """

    accepted: bool
    score: float
    threshold: float

    @property
    def margin(self) -> float:
        return self.score - self.threshold


@runtime_checkable
class SpeakerProvider(Protocol):
    name: str

    def embed(self, samples: np.ndarray) -> np.ndarray:
        """A fixed-length vector describing the voice in this audio."""
        ...

    def verify(self, samples: np.ndarray) -> VerificationResult:
        """Compare against the enrolled profile.

        Raises :class:`VoiceEngineError` when no profile is enrolled: silently
        accepting everyone would be the worst possible default.
        """
        ...


# ------------------------------------------------------------------------ TTS


class Voice(StrEnum):
    """Which voice to speak with. The engine maps these to concrete models."""

    EN = "en"
    RU = "ru"


@dataclass(frozen=True, slots=True)
class Utterance:
    """Rendered speech, ready to play."""

    samples: np.ndarray
    sample_rate: int
    voice: Voice
    #: Set when the audio came from a cache rather than a fresh synthesis.
    cached: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    async def synthesise(self, text: str, *, language: Language) -> Utterance:
        """Render text to audio, or raise :class:`VoiceEngineError`."""
        ...
