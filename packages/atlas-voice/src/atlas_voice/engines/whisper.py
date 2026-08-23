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

**The language is settled before the words are.** The prompt is bilingual, and
handed to a single-pass transcription it can decide the language as well as the
vocabulary: "show me how much memory is left", spoken in English, came back as
«покажи мне, сколько памяти осталось» — fluent, confident, and a translation of
something nobody asked for. It happened intermittently, which is worse than
always. So detection runs first on the audio alone, where no prompt can reach
it, and transcription is then pinned to what it found. Measured over seven
Russian and English utterances, single-pass got the language wrong often enough
to fail a test suite at random and two-pass got none wrong, with the Russian
transcripts unchanged — «Открой Chrome» still comes back with Chrome spelled in
Latin, which is the thing the prompt was there for.

The provider reports the language it settled on *and* how confident it was.
Low confidence is a reason for the assistant to ask rather than guess, and that
decision belongs upstream, not here.

**One Windows detail, or none of the above happens.** CUDA reaches this process
through pip wheels — ``nvidia-cublas-cu12``, ``nvidia-cudnn-cu12`` — which drop
their DLLs under ``site-packages/nvidia/*/bin``, a directory Windows has no
reason to search. CTranslate2 loads them by bare name through ``LoadLibrary``,
which reads ``PATH`` and ignores ``os.add_dll_directory``, so both are set:
the search path for anything that asks politely, and ``PATH`` for CTranslate2.
Without it the model loads, reports a CUDA device, and then fails on the first
utterance with "cublas64_12.dll is not found" — a failure that looks like a
broken GPU and is really a broken search path.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.normalize import Normaliser
from atlas_voice.providers import Transcript, VoiceEngineError

__all__ = ["WhisperSTT", "WhisperSettings"]


def _prepare_cuda_path() -> None:
    """Let the loader find the CUDA libraries pip installed.

    See the module docstring. Harmless off Windows, and harmless when the
    wheels are absent — a CPU-only machine simply has nothing to add.
    """
    if sys.platform != "win32":
        return
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not root.is_dir():
        return

    directories = [str(path) for path in sorted(root.glob("*/bin")) if path.is_dir()]
    if not directories:
        return

    for directory in directories:
        os.add_dll_directory(directory)
    # And PATH as well: CTranslate2 asks for these by bare name, and that path
    # does not consult the directories added above.
    existing = os.environ.get("PATH", "")
    missing = [item for item in directories if item not in existing]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, existing])


#: Whisper's own language codes, mapped onto the project's enum. Anything else
#: it reports is treated as unknown rather than coerced: silently calling
#: Ukrainian "Russian" would make the reply come back in the wrong language.
_LANGUAGES = {"ru": Language.RU, "en": Language.EN, "kk": Language.KK}

#: The programs, named so the recogniser has met them before it meets them
#: mid-sentence. Spelled in Latin in both prompts on purpose: that is what
#: biases Russian decoding towards "Chrome" rather than «Хром».
_PROGRAMS = (
    "JARVIS. Chrome, VS Code, Notepad, Telegram, PowerShell, Explorer, Spotify, Discord, Windows."
)

#: One prompt per language, chosen after the language is settled.
#:
#: A single bilingual prompt was the first design and it cannot work. Whisper
#: does not merely take vocabulary from the prompt, it takes *phrasing*, and a
#: prompt containing «Покажи использование памяти» answers the spoken English
#: "show me how much memory is left" with «покажи мне, сколько памяти
#: осталось» — reproducibly, at language-detection confidence 1.00 for English,
#: with the decoder pinned to English. Pinning the language does not stop it,
#: because nothing prevents an English-pinned decoder emitting Cyrillic. Only
#: removing the Russian sentences from the English prompt does.
#:
#: They cannot simply be dropped, either: measured over four Russian commands,
#: a names-only prompt turned «Закрой блокнот, пожалуйста» into «Здоровый
#: блокнот, пожалуйста» and «Открой Хром» into «Рома». The imperatives are
#: carrying real weight. So each language gets its own.
_VOCABULARY: dict[Language, str] = {
    Language.EN: f"{_PROGRAMS} Open Chrome. Close Notepad. Show me the memory usage.",
    Language.RU: f"{_PROGRAMS} Открой Chrome. Закрой Notepad. Покажи использование памяти.",
}

#: Used only when detection could not settle on a language. Both, because
#: guessing one and being wrong is worse than a weaker prompt.
_VOCABULARY_EITHER = f"{_VOCABULARY[Language.EN]} {_VOCABULARY[Language.RU]}"


def _bare(text: str) -> str:
    """Lower-cased words only, for comparing what was said with what was primed."""
    return " ".join(re.findall(r"\w+", text.lower()))


#: Whisper reads ``initial_prompt`` as context and will, given almost no audio,
#: simply hand it back — fluently, punctuated, and indistinguishable from a real
#: transcript. It surfaced here as «Покажи использование памяти.» in answer to
#: someone saying "Jarvis, open Notepad": the prompt, verbatim, with total
#: confidence. Anything that is merely a piece of the priming text is therefore
#: treated as nothing having been said.
_PRIMED = tuple(_bare(text) for text in _VOCABULARY.values())


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
    #: Settle the language from the audio before decoding. Costs one encoder
    #: pass; see the module docstring for what it buys.
    detect_language_first: bool = True
    #: Below this the detection is not worth pinning to, and Whisper is left to
    #: make its own mind up rather than being held to a coin toss.
    language_confidence_floor: float = 0.6
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

        _prepare_cuda_path()
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

    def _detect(self, samples: np.ndarray) -> tuple[str | None, float]:
        """Which language, decided on the audio and nothing else."""
        if not self._settings.detect_language_first:
            return None, 0.0
        try:
            code, probability, _ = self._model.detect_language(
                audio=samples, vad_filter=self._settings.vad_filter
            )
        except Exception:
            # Not fatal: an older model object without this method, or a clip
            # too short to judge. Falling through to one-pass is worse, not
            # broken.
            return None, 0.0
        if probability < self._settings.language_confidence_floor:
            return None, float(probability)
        if code not in _LANGUAGES:
            # Something we do not speak. Pinning it would make Whisper commit to
            # a language the assistant cannot answer in.
            return None, float(probability)
        return str(code), float(probability)

    def _run(self, samples: np.ndarray, hint: Language | None) -> tuple[str, Language, float]:
        settled, detected_probability = self._detect(samples)
        prompt = _VOCABULARY[_LANGUAGES[settled]] if settled in _LANGUAGES else _VOCABULARY_EITHER
        segments, info = self._model.transcribe(
            samples,
            beam_size=self._settings.beam_size,
            vad_filter=self._settings.vad_filter,
            initial_prompt=prompt,
            # Each utterance stands alone. Carrying the last one forward makes
            # Whisper finish sentences nobody started, and the segmenter has
            # already decided where the boundaries are.
            condition_on_previous_text=False,
            # Settled above where the prompt cannot reach, or left open when the
            # audio was not clear enough to say.
            language=settled,
        )
        text = " ".join(segment.text for segment in segments).strip()
        if self._is_echo(text):
            return "", _LANGUAGES.get(info.language, hint or Language.EN), 0.0

        language = _LANGUAGES.get(info.language, hint or Language.EN)
        confidence = detected_probability or float(info.language_probability)
        return text, language, confidence

    @staticmethod
    def _is_echo(text: str) -> bool:
        """Did it hand the priming text back instead of transcribing?

        Only whole-phrase matches count. A command that genuinely contains a
        primed word — "open Chrome" — must survive, so the test is whether
        everything that was said sits inside the prompt, not whether anything
        does.
        """
        spoken = _bare(text)
        return bool(spoken) and any(spoken in primed for primed in _PRIMED)
