"""Speech synthesis with Piper, behind :class:`TTSProvider`.

MIT, entirely local, no key, and fast enough that the reply starts before the
sentence has finished rendering. Nothing spoken here leaves the machine.

**The voice.** Calm, male, unhurried, British — the register the assistant is
meant to have. It is an *original* synthetic voice from the Piper collection,
not a clone of any performer, and nothing here samples or imitates a specific
person.

Russian needs a different voice because the English one cannot pronounce it.
That is a real seam: the timbre changes when the language does. It is the price
of staying local and free, it is audible, and pretending otherwise would be
worse than saying so. The alternative — one cloud voice with a single identity
across both languages — sends every spoken reply to a third party, and the
project's own rule is that what can stay on the machine does.

**Caching the short things.** "Yes, sir?" is said after every wake word and must
feel instant. Synthesising it each time costs a few hundred milliseconds for a
phrase that never changes, so fixed responses are rendered once and kept.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE, resample
from atlas_voice.providers import Utterance, Voice, VoiceEngineError

__all__ = ["PiperTTS", "VoiceChoice"]


@dataclass(frozen=True)
class VoiceChoice:
    """Which Piper voice speaks which language.

    ``en_GB-alan-medium`` is the assistant's English voice: male, measured,
    British, and understated rather than theatrical — sarcasm has to read as dry
    when this speaks it, so a performative voice would fight the writing.
    """

    english: str = "en_GB-alan-medium"
    russian: str = "ru_RU-dmitri-medium"
    #: Piper renders at its own rate; the pipeline runs at 16 kHz.
    directory: Path | None = None
    #: Below 1.0 speaks faster. Slightly slow reads as composed, not sluggish.
    length_scale: float = 1.0

    def for_language(self, language: Language) -> str:
        return self.russian if language is Language.RU else self.english


@dataclass
class _Cached:
    samples: np.ndarray
    rate: int


class PiperTTS:
    """Renders text to speech. One voice per language, loaded on first use."""

    name = "piper"

    def __init__(
        self,
        choice: VoiceChoice | None = None,
        *,
        models_dir: Path | None = None,
        cache_phrases: tuple[str, ...] = (),
    ) -> None:
        self._choice = choice or VoiceChoice()
        directory = models_dir or self._choice.directory
        if directory is None:
            raise VoiceEngineError("PiperTTS needs models_dir, or a VoiceChoice.directory")
        self._dir: Path = Path(directory)
        self._voices: dict[str, Any] = {}
        self._cache: dict[tuple[str, Language], _Cached] = {}
        self._cache_phrases = cache_phrases

    def _voice(self, name: str) -> Any:
        if name in self._voices:
            return self._voices[name]

        path = self._dir / f"{name}.onnx"
        if not path.is_file():
            raise VoiceEngineError(
                f"voice {name} not found at {path}. "
                "Run scripts/fetch_voice_models.ps1 to download it."
            )
        try:
            from piper import PiperVoice
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "piper-tts is required for speech synthesis; install atlas-voice[tts]"
            ) from exc

        self._voices[name] = PiperVoice.load(str(path))
        return self._voices[name]

    async def synthesise(self, text: str, *, language: Language) -> Utterance:
        spoken = text.strip()
        voice = Voice.RU if language is Language.RU else Voice.EN
        if not spoken:
            return Utterance(
                samples=np.zeros(0, dtype=np.float32), sample_rate=SAMPLE_RATE, voice=voice
            )

        key = (spoken, language)
        if (hit := self._cache.get(key)) is not None:
            return Utterance(samples=hit.samples, sample_rate=hit.rate, voice=voice, cached=True)

        try:
            samples, rate = await asyncio.to_thread(self._render, spoken, language)
        except VoiceEngineError:
            raise
        except Exception as exc:
            raise VoiceEngineError(f"synthesis failed: {type(exc).__name__}") from exc

        if spoken in self._cache_phrases:
            self._cache[key] = _Cached(samples=samples, rate=rate)
        return Utterance(samples=samples, sample_rate=rate, voice=voice)

    def _render(self, text: str, language: Language) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig

        voice = self._voice(self._choice.for_language(language))
        chunks = list(
            voice.synthesize(
                text, syn_config=SynthesisConfig(length_scale=self._choice.length_scale)
            )
        )
        if not chunks:
            raise VoiceEngineError("the synthesiser produced no audio")

        audio = np.concatenate([chunk.audio_float_array for chunk in chunks]).astype(np.float32)
        return audio, int(chunks[0].sample_rate)

    async def warm(self) -> None:
        """Render the fixed phrases now, so the first one is not slow.

        "Yes, sir?" is the assistant's whole first impression. Paying three
        hundred milliseconds for it every single time, for a phrase that never
        changes, is the kind of thing nobody notices in a benchmark and
        everybody notices in use.
        """
        for phrase in self._cache_phrases:
            for language in (Language.EN, Language.RU):
                await self.synthesise(phrase, language=language)

    def at_pipeline_rate(self, utterance: Utterance) -> np.ndarray:
        """The audio resampled to 16 kHz, for anything that has to mix it.

        Playback uses the native rate — resampling a reply down to 16 kHz and
        back would throw away quality for nothing — but echo cancellation needs
        the reference signal at the pipeline rate.
        """
        if utterance.sample_rate == SAMPLE_RATE:
            return utterance.samples
        return resample(utterance.samples, from_rate=utterance.sample_rate, to_rate=SAMPLE_RATE)


#: What the assistant says the moment the wake word lands.
ACKNOWLEDGEMENTS: dict[Language, tuple[str, ...]] = {
    Language.EN: ("Yes, sir?",),
    Language.RU: ("Да, сэр?",),
}

DEFAULT_CACHE: tuple[str, ...] = tuple(
    phrase for phrases in ACKNOWLEDGEMENTS.values() for phrase in phrases
)
