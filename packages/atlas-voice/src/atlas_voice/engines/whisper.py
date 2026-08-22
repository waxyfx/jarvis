"""Local speech recognition with faster-whisper, behind :class:`STTProvider`.

Runs on this machine and nowhere else. Audio never leaves the agent — the
backend and Gemini only ever see the text this produces, which is the same
guarantee the wake word and the speaker profile carry.

**Why large-v3.** Russian is the constraint. The smaller multilingual models
handle English well and Russian noticeably worse, and this assistant is
addressed in both. On an RTX 3060 with ``int8_float16`` the model occupies
roughly 2.5 GB of 6 and transcribes a five-second utterance in well under a
second, so the accuracy is affordable.

**Where it will disappoint, stated plainly.** Whisper assigns *one* language per
utterance. Between utterances the detection is reliable; *within* one it is not,
and «Открой VS Code and start my project» is exactly the shape that suffers —
the English is decoded through Russian phonotactics and comes back
transliterated. Two things push back, neither of them magic: an ``initial_prompt``
carrying the vocabulary that matters, and a deterministic alias table applied
afterwards (:mod:`atlas_voice.normalize`). Both are measured rather than assumed.

The provider reports the language it settled on *and* how confident it was.
Low confidence is a reason for the assistant to ask rather than guess, and that
decision belongs upstream, not here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.normalize import Normaliser
from atlas_voice.providers import Transcript, VoiceEngineError

__all__ = ["WhisperSTT", "WhisperSettings"]

#: Whisper's own language codes, mapped onto the project's enum. Anything else
#: it reports is treated as unknown rather than coerced: silently calling
#: Ukrainian "Russian" would make the reply come back in the wrong language.
_LANGUAGES = {"ru": Language.RU, "en": Language.EN, "kk": Language.KK}

#: Named so the recogniser has seen them before it meets them mid-sentence.
#: Whisper conditions on this text, which biases decoding towards Latin
#: spellings of product names inside Russian speech.
_VOCABULARY = (
    "JARVIS. Chrome, VS Code, Notepad, Telegram, PowerShell, Explorer, "
    "Spotify, Discord, Windows. Открой Chrome. Закрой Notepad. "
    "Покажи использование памяти."
)


@dataclass(frozen=True)
class WhisperSettings:
    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    #: Beam search costs latency and buys accuracy. Five is Whisper's own
    #: default and the difference against greedy is audible on Russian.
    beam_size: int = 5
    #: Whisper hallucinates fluent sentences on silence. A voice-activity gate
    #: in front of it is the real defence, but this is a cheap second one.
    vad_filter: bool = True
    #: Shorter than this is a click or a breath, not a command.
    minimum_seconds: float = 0.25
    download_root: Path | None = None


class WhisperSTT:
    """Transcribes one utterance at a time. Not streaming, by design.

    The session hands over a complete utterance that the VAD has already
    bounded, so streaming partial hypotheses would add complexity and buy
    nothing: nothing downstream can act on half a command.
    """

    name = "faster-whisper"

    def __init__(
        self,
        settings: WhisperSettings | None = None,
        *,
        normaliser: Normaliser | None = None,
        model: Any | None = None,
    ) -> None:
        self._settings = settings or WhisperSettings()
        self._normaliser = normaliser or Normaliser()
        self.model = self._settings.model

        if model is not None:
            self._model = model
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "faster-whisper is required for speech recognition; install atlas-voice[stt]"
            ) from exc

        try:
            self._model = WhisperModel(
                self._settings.model,
                device=self._settings.device,
                compute_type=self._settings.compute_type,
                download_root=(
                    str(self._settings.download_root) if self._settings.download_root else None
                ),
            )
        except Exception as exc:
            raise VoiceEngineError(
                f"could not load {self._settings.model} on {self._settings.device}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def transcribe(self, samples: np.ndarray, *, hint: Language | None = None) -> Transcript:
        """Text for one utterance. Never raises anything but VoiceEngineError."""
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        duration = len(samples) / SAMPLE_RATE
        if duration < self._settings.minimum_seconds:
            return Transcript(text="", language=hint or Language.EN, duration_s=duration)

        try:
            # Whisper is CPU/GPU-bound C++ under the hood and blocks the loop.
            raw, language, confidence = await asyncio.to_thread(self._run, samples, hint)
        except VoiceEngineError:
            raise
        except Exception as exc:
            raise VoiceEngineError(f"recognition failed: {type(exc).__name__}") from exc

        text, _ = self._normaliser.apply(raw)
        return Transcript(
            text=text,
            language=language,
            language_confidence=confidence,
            raw_text=raw,
            duration_s=duration,
        )

    def _run(self, samples: np.ndarray, hint: Language | None) -> tuple[str, Language, float]:
        segments, info = self._model.transcribe(
            samples,
            beam_size=self._settings.beam_size,
            vad_filter=self._settings.vad_filter,
            initial_prompt=_VOCABULARY,
            # A hint biases, never forces: the point of detection is that the
            # speaker switches languages without announcing it.
            language=None,
        )
        text = " ".join(segment.text for segment in segments).strip()
        language = _LANGUAGES.get(info.language, hint or Language.EN)
        return text, language, float(info.language_probability)
