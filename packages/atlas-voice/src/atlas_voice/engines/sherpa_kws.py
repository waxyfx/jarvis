"""Keyword spotting with sherpa-onnx, behind the :class:`WakeWordProvider` contract.

Free, Apache-2.0, entirely offline, no key and no vendor — which is the whole
reason it is here. It also turned out to be dramatically more precise than the
openWakeWord classifier trained for this project: on the words that made that
model unusable — "Travis", "Jargon", "starve us", «Джаз», «Шарф», «Дарвин»,
«Сервис» — this fires on none of them.

**Open vocabulary.** The wake phrase is a line of tokens in a text file, not a
trained model, so changing it costs nothing. The transducer was trained to
recognise speech in general; the keyword file only says which token sequence to
watch for. Two model families are supported and they tokenise differently:

* ``gigaspeech`` — English BPE, e.g. ``JARVIS`` becomes ``▁JA R VI S``;
* ``zh-en`` — CMU phonemes, e.g. ``JARVIS`` becomes ``JH AA1 R V AH0 S``.

**Measured on this project's acceptance clips**, twelve synthetic speakers each:

    phrase          gigaspeech   zh-en   false positives
    "Hey Jarvis"        12/12    12/12         0 of 14
    "Jarvis"            10/12    11/12         0 of 14
    «Джарвис»            4/12     4/12         0 of 14

The two-word form is the one to use: more phonetic material means more evidence,
and it reaches perfect recall where the bare word does not. Russian is *not*
solved — neither model is trained on it, and 33% is not a wake word. That is a
language-coverage limit, not a defect here.

**A Windows loader trap, which cost an hour.** The sherpa-onnx wheel ships no
ONNX Runtime of its own; its extension links against ``onnxruntime.dll`` by
name. Windows keeps its own copy in ``System32`` — version 1.17 on this machine —
and the loader finds it first, at which point sherpa asks for ORT API 27 and
gets told only 1 through 17 exist. The failure looks like a broken package. The
fix is to put the pip package's ``capi`` directory on the DLL search path
*before* importing sherpa_onnx, which :func:`_prepare_dll_path` does.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.providers import Detection, VoiceEngineError

__all__ = ["KeywordModel", "SherpaKeywordSpotter"]


def _prepare_dll_path() -> None:
    """Make the loader prefer the pip ONNX Runtime over the one in System32.

    See the module docstring. Harmless when the directory is absent or the
    platform is not Windows.
    """
    if sys.platform != "win32":
        return
    capi = Path(sys.prefix) / "Lib" / "site-packages" / "onnxruntime" / "capi"
    if capi.is_dir():
        os.add_dll_directory(str(capi))


@dataclass(frozen=True, slots=True)
class KeywordModel:
    """Where one keyword-spotting model lives, and how it spells words."""

    directory: Path
    encoder: str
    decoder: str
    joiner: str
    #: ``bpe`` uses sentencepiece over text; ``phone`` uses CMU phonemes, which
    #: are written into the keyword file directly.
    tokenisation: str = "bpe"
    bpe_model: str = "bpe.model"

    def path(self, name: str) -> Path:
        resolved = self.directory / name
        if not resolved.is_file():
            raise VoiceEngineError(
                f"{resolved} is missing. Run scripts/fetch_voice_models.ps1 to download the "
                "keyword-spotting model."
            )
        return resolved


class SherpaKeywordSpotter:
    """Detects a spoken phrase in a stream. Stateful; feed it every frame."""

    name = "sherpa-kws"

    def __init__(
        self,
        model: KeywordModel,
        *,
        phrases: tuple[str, ...] = ("HEY JARVIS", "JARVIS"),
        keyword_tokens: tuple[str, ...] = (),
        threshold: float = 0.25,
        num_threads: int = 1,
        label: str = "jarvis",
        refractory_s: float = 1.0,
    ) -> None:
        """
        ``phrases`` are plain words, tokenised for the model. ``keyword_tokens``
        bypasses that and takes already-tokenised lines, which is what the
        phoneme models want when a pronunciation has to be written by hand.

        ``threshold`` only bites above roughly 0.9 in practice: measured across
        0.001 to 0.5 the decision never changed, because the transducer's
        confidence on a match is close to saturated. It is exposed anyway, since
        that observation is about these models rather than about the API.
        """
        _prepare_dll_path()
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "sherpa-onnx is required for keyword spotting; install atlas-voice[wake]"
            ) from exc

        self._model = model
        self._label = label
        self._refractory_s = refractory_s

        lines = list(keyword_tokens) or [self._tokenise(word) for word in phrases]
        if not lines:
            raise VoiceEngineError("no keyword phrases were given")
        self._keyword_file = model.directory / "atlas_keywords.txt"
        self._keyword_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        try:
            self._spotter: Any = sherpa_onnx.KeywordSpotter(
                tokens=str(model.path("tokens.txt")),
                encoder=str(model.path(model.encoder)),
                decoder=str(model.path(model.decoder)),
                joiner=str(model.path(model.joiner)),
                keywords_file=str(self._keyword_file),
                num_threads=num_threads,
                keywords_threshold=threshold,
                provider="cpu",
            )
        except Exception as exc:
            raise VoiceEngineError(
                f"could not start the keyword spotter: {type(exc).__name__}: {exc}"
            ) from exc

        self._stream = self._spotter.create_stream()
        self._elapsed = 0.0
        self._quiet_until = 0.0
        #: Structural parity with the other providers. The spotter reports a
        #: match rather than a probability, so this is 1.0 or 0.0 — saying so
        #: beats implying a precision it does not have.
        self.last_score = 0.0

    def _tokenise(self, phrase: str) -> str:
        text = phrase.upper().strip()
        if self._model.tokenisation == "phone":
            return self._phonemes(text)

        try:
            import sentencepiece as spm
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "sentencepiece is required to tokenise keywords for a BPE model"
            ) from exc

        processor = spm.SentencePieceProcessor()
        processor.load(str(self._model.path(self._model.bpe_model)))
        return " ".join(processor.encode(text, out_type=str))

    def _phonemes(self, text: str) -> str:
        """Look each word up in the model's CMU lexicon.

        A word the lexicon does not know cannot be spelled by guessing: the
        keyword would silently become something the model never matches, which
        is a wake word that never fires and no error to explain it.
        """
        lexicon = self._model.path("en.phone")
        wanted = text.split()
        found: dict[str, str] = {}
        with lexicon.open(encoding="utf-8") as handle:
            for line in handle:
                word, _, phones = line.partition(" ")
                if word in wanted and word not in found:
                    found[word] = phones.strip()
                if len(found) == len(set(wanted)):
                    break

        missing = [word for word in wanted if word not in found]
        if missing:
            raise VoiceEngineError(
                f"{missing} not in {lexicon.name}; supply keyword_tokens with the "
                "pronunciation written out in CMU phonemes"
            )
        return " ".join(found[word] for word in wanted)

    # ---------------------------------------------------------------- feeding

    def push(self, samples: np.ndarray) -> Detection | None:
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        self._elapsed += len(samples) / SAMPLE_RATE
        self._stream.accept_waveform(SAMPLE_RATE, samples)

        detection: Detection | None = None
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if not result:
                continue
            # A match leaves the stream holding the matched tokens; without a
            # reset the same utterance keeps re-reporting.
            self._spotter.reset_stream(self._stream)
            self.last_score = 1.0
            if self._elapsed >= self._quiet_until:
                self._quiet_until = self._elapsed + self._refractory_s
                detection = Detection(score=1.0, at=self._elapsed, label=self._label)
        return detection

    @property
    def elapsed(self) -> float:
        """Seconds of audio fed since construction.

        Deliberately *not* zeroed by :meth:`reset`: this is stream time, and a
        detection's ``at`` is only meaningful against a clock that keeps
        running. A caller measuring one clip at a time has to subtract the value
        it saw at the start — the acceptance harness did not, and reported a
        median latency of 166 seconds, which is 592 clips of accumulated stream
        rather than anything about the detector.
        """
        return self._elapsed

    def reset(self) -> None:
        """Drop the decoder state and be ready immediately.

        It deliberately does *not* start a refractory period. Suppressing a
        repeat is already handled where it belongs — the quiet window is set at
        the moment of a detection — and doing it here as well leaves the
        detector deaf for a second after every reset. Measured cost of that
        mistake: recall on the acceptance suite read 0.629 instead of 0.719,
        because each clip begins with a second of silence and the word landed
        inside the self-imposed deafness.
        """
        self._stream = self._spotter.create_stream()
        self.last_score = 0.0

    def flush(self) -> Detection | None:
        """Tell the spotter the audio ended, and take any final match.

        A phrase at the very end of a clip is still mid-decode when the samples
        run out: the transducer needs a little silence after a keyword before it
        will commit to it. A microphone always supplies that silence; a file
        does not, so a short tail is appended here. Without it the last phrase
        of every recording is lost, which looks exactly like a detector that
        cannot hear.
        """
        tail = np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32)
        detection = self.push(tail)
        self._stream.input_finished()
        return detection or self.push(np.zeros(0, dtype=np.float32))
