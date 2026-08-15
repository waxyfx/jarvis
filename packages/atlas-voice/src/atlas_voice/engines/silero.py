"""Silero VAD v5, through onnxruntime.

MIT, about two megabytes, and roughly a millisecond per frame on one CPU core —
there is no serious competitor at this size, so this is the one stage of the
pipeline with no alternative implementation planned.

The model is recurrent: it carries a state tensor between calls, which is why it
must be fed every frame in order and why :meth:`reset` exists. Feeding it
out-of-order audio does not raise anything; it just quietly gets worse, so the
frame-size check below is deliberately strict.

**It also wants 64 samples of the previous frame prepended.** The published
wrapper does this internally, so the requirement is invisible if you go by the
ONNX signature alone — which accepts any length and returns a confident-looking
number regardless. Without the context the model scored *0.003 on speech and
0.004 on silence*: not merely degraded, but blind, while looking like it worked.
With it, 90% of speech frames clear the threshold and silence stays at 0.004.

That is why this module is tested against real synthesised speech and not only
against silence. A test that feeds silence and asserts "not speech" passes
perfectly against a VAD that has been broken into always answering no.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from atlas_voice.providers import VoiceEngineError

if TYPE_CHECKING:
    import onnxruntime as ort

__all__ = ["SileroVAD"]

#: The window Silero v5 expects at 16 kHz. It is also the pipeline frame size,
#: which is not a coincidence — the frame size was chosen to match.
_WINDOW = 512
#: Samples of the *previous* window handed back to the model with this one.
#: Undocumented in the ONNX signature; see the module docstring.
_CONTEXT = 64


class SileroVAD:
    """Voice activity detection. Answers one question about 32 ms of audio."""

    name = "silero"

    def __init__(
        self,
        model_path: Path | str,
        *,
        threshold: float = 0.5,
        session: ort.InferenceSession | None = None,
    ) -> None:
        """
        ``threshold`` is the probability above which a window counts as speech.
        0.5 is Silero's own default and is a reasonable starting point; noisy
        rooms want it higher, and a quiet talker wants it lower. It is exposed
        rather than hard-coded because it is the knob people actually turn.
        """
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1, exclusive")
        self._threshold = threshold

        if session is not None:
            self._session = session
        else:
            path = Path(model_path)
            if not path.is_file():
                raise VoiceEngineError(
                    f"Silero VAD model not found at {path}. "
                    "Run scripts/fetch_voice_models.ps1 to download it."
                )
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise VoiceEngineError(
                    "onnxruntime is required for the VAD; install atlas-voice[vad]"
                ) from exc

            options = ort.SessionOptions()
            # One thread is faster here than several: the model is tiny, and
            # thread hand-off costs more than the work itself. It also keeps the
            # always-on stage from taking a core away from everything else.
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._sample_rate = np.array(SAMPLE_RATE, dtype=np.int64)
        #: The last probability, for logging and for tuning the threshold
        #: against real recordings rather than by feel.
        self.last_probability = 0.0

    def is_speech(self, samples: np.ndarray) -> bool:
        return self.probability(samples) >= self._threshold

    def probability(self, samples: np.ndarray) -> float:
        """The raw score, which is what you want when choosing a threshold."""
        if len(samples) != _WINDOW:
            raise ValueError(
                f"Silero v5 needs exactly {_WINDOW} samples at {SAMPLE_RATE} Hz, got {len(samples)}"
            )
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        outputs: list[Any] = self._session.run(
            None,
            {
                "input": np.concatenate([self._context, samples]).reshape(1, -1),
                "state": self._state,
                "sr": self._sample_rate,
            },
        )
        probability, self._state = outputs[0], outputs[1]
        self._context = samples[-_CONTEXT:].copy()
        self.last_probability = float(probability[0][0])
        return self.last_probability

    def reset(self) -> None:
        """Forget the recurrent state *and* the carried context.

        Called between utterances and after mute: both describe audio that is
        no longer adjacent to what comes next.
        """
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self.last_probability = 0.0


assert FRAME_SAMPLES == _WINDOW, "the pipeline frame size must match Silero's window"
