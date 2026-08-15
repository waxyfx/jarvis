"""Wake-word spotting on openWakeWord's feature stack, through onnxruntime.

Three models in series, all Apache-2.0 and all tiny:

    audio → melspectrogram → speech embedding → per-word classifier → score

The upstream Python package is not a dependency. It requires ``tflite-runtime``,
which publishes no wheel for Python 3.12, so the workspace lock cannot be solved
with it present. Nothing of value is lost: what we need is the three ONNX graphs
and the arithmetic between them, and running them directly removes an entire
inference runtime from the agent.

**Two details are invisible from the ONNX signatures and both are fatal.**

The melspectrogram output must be scaled by ``x / 10 + 2`` before the embedding
model sees it. Measured against the published ``hey_jarvis`` model on
synthesised speech: with the scaling, the phrase scores 0.998 and everything
else scores 0.000; without it, the phrase scores 0.061 and unrelated speech
0.057 — no separation at all, but no error either.

The melspectrogram graph yields ``n / 160 - 3`` frames, so a chunk handed over in
isolation loses three frames to the analysis window. Feeding 1280 fresh samples
therefore produces five frames rather than the eight that continuous audio owes,
and a streaming detector built the obvious way drifts out of step with the
batch behaviour it was tuned against. This module prepends the previous chunk's
final 480 samples so each step contributes exactly eight frames, and a test
asserts streaming and batch agree.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np

from atlas_voice.providers import Detection, VoiceEngineError

__all__ = ["OpenWakeWord"]

#: Audio consumed per step. 80 ms at 16 kHz, openWakeWord's own step size.
_CHUNK = 1280
#: Mel hop, and the three hops of history a chunk needs to yield eight frames.
_HOP = 160
_OVERLAP = 3 * _HOP
#: The embedding model's input window, and how far it advances per embedding.
_EMB_WINDOW = 76
_EMB_STRIDE = 8
#: How many embeddings the classifier reads.
_CLF_WINDOW = 16


class OpenWakeWord:
    """Detects one wake word in a stream. Stateful; feed it every frame."""

    name = "openwakeword"

    def __init__(
        self,
        classifier_path: Path | str,
        *,
        melspectrogram_path: Path | str,
        embedding_path: Path | str,
        threshold: float = 0.5,
        label: str = "atlas",
        refractory_s: float = 1.0,
    ) -> None:
        """
        ``refractory_s`` is how long the detector stays quiet after firing. The
        classifier sees overlapping windows, so one spoken word produces a run
        of high scores; without this, "Atlas" fires four or five times and the
        session opens, closes and reopens underneath the speaker.
        """
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1, exclusive")

        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "onnxruntime is required for wake-word spotting; install atlas-voice[wake]"
            ) from exc

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1

        def load(path: Path | str, what: str) -> ort.InferenceSession:
            resolved = Path(path)
            if not resolved.is_file():
                raise VoiceEngineError(
                    f"{what} model not found at {resolved}. "
                    "Run scripts/fetch_voice_models.ps1 to download it."
                )
            return ort.InferenceSession(
                str(resolved), sess_options=options, providers=["CPUExecutionProvider"]
            )

        self._mel = load(melspectrogram_path, "melspectrogram")
        self._embed = load(embedding_path, "embedding")
        self._classify = load(classifier_path, "wake-word classifier")
        self._classifier_input = self._classify.get_inputs()[0].name

        self._threshold = threshold
        self._label = label
        self._refractory_s = refractory_s

        self._audio = np.zeros(0, dtype=np.float32)
        self._overlap = np.zeros(_OVERLAP, dtype=np.float32)
        self._frames: list[np.ndarray] = []
        self._embeddings: deque[np.ndarray] = deque(maxlen=_CLF_WINDOW)
        self._elapsed = 0.0
        self._quiet_until = 0.0
        #: Highest score seen since the last reset, for threshold tuning against
        #: real recordings rather than by feel.
        self.last_score = 0.0

    # ------------------------------------------------------------------ feed

    def push(self, samples: np.ndarray) -> Detection | None:
        """Feed audio of any length; returns a detection when one fires."""
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        self._audio = np.concatenate([self._audio, samples])

        detection: Detection | None = None
        while len(self._audio) >= _CHUNK:
            chunk, self._audio = self._audio[:_CHUNK], self._audio[_CHUNK:]
            self._elapsed += _CHUNK / 16_000
            fired = self._consume(chunk)
            if fired is not None:
                detection = fired
        return detection

    def _consume(self, chunk: np.ndarray) -> Detection | None:
        window = np.concatenate([self._overlap, chunk])
        self._overlap = chunk[-_OVERLAP:].copy()

        mel = self._mel.run(None, {"input": window.reshape(1, -1)})[0].squeeze()
        # See the module docstring: this scaling is the difference between a
        # detector and a random number generator.
        self._frames.extend(mel / 10.0 + 2.0)

        detection: Detection | None = None
        while len(self._frames) >= _EMB_WINDOW:
            stack = np.stack(self._frames[:_EMB_WINDOW])[None, ..., None].astype(np.float32)
            embedding = self._embed.run(None, {"input_1": stack})[0].reshape(96)
            self._embeddings.append(embedding)
            del self._frames[:_EMB_STRIDE]

            fired = self._score()
            if fired is not None:
                detection = fired
        return detection

    def _score(self) -> Detection | None:
        if len(self._embeddings) < _CLF_WINDOW:
            return None

        window = np.stack(self._embeddings)[None].astype(np.float32)
        score = float(self._classify.run(None, {self._classifier_input: window})[0][0][0])
        self.last_score = score

        if score < self._threshold or self._elapsed < self._quiet_until:
            return None

        self._quiet_until = self._elapsed + self._refractory_s
        return Detection(score=score, at=self._elapsed, label=self._label)

    def reset(self) -> None:
        """Forget buffered audio, so one utterance cannot fire twice."""
        self._audio = np.zeros(0, dtype=np.float32)
        self._overlap = np.zeros(_OVERLAP, dtype=np.float32)
        self._frames.clear()
        self._embeddings.clear()
        self._quiet_until = self._elapsed + self._refractory_s
        self.last_score = 0.0

    # ------------------------------------------------------------- diagnostics

    def scores_with_times(self, samples: np.ndarray) -> list[tuple[float, float]]:
        """``(stream_time, score)`` for every window this audio produces.

        The time is taken from the detector's own clock rather than derived from
        the index. Deriving it means guessing how many chunks the pipeline
        swallows before the first window exists — the classifier needs sixteen
        embeddings, which is nearly two seconds of audio — and guessing it wrong
        produces *negative* latencies, which is how this was found.
        """
        self.reset()
        self._elapsed = 0.0
        self._quiet_until = 0.0
        collected: list[tuple[float, float]] = []
        for offset in range(0, len(samples) - _CHUNK + 1, _CHUNK):
            self.push(samples[offset : offset + _CHUNK])
            if len(self._embeddings) == _CLF_WINDOW:
                collected.append((self._elapsed, self.last_score))
        return collected

    def scores_for(self, samples: np.ndarray) -> list[float]:
        """Every score this audio produces, ignoring the refractory period.

        For measuring false accepts over long recordings, where the question is
        how often the score crosses a threshold rather than what a session would
        have done about it.
        """
        return [score for _, score in self.scores_with_times(samples)]
