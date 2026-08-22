"""Speaker verification with sherpa-onnx, behind :class:`SpeakerProvider`.

Chosen over SpeechBrain's ECAPA, which was the original plan, for reasons that
only became apparent once sherpa was already here: it is the *same* runtime the
wake word uses, so it costs no new dependency, no torch, and no second ONNX
stack. The model is 38 MB and runs on CPU in tens of milliseconds.

**This is a filter, not a credential.** It decides whose speech the assistant
acts on. It never decides what may be done: a MEDIUM action still goes through
the Policy Engine and still needs its confirmation, exactly as for typed input.

**It will accept a recording of you played through a speaker.** The model
compares timbre; it has no idea whether the sound came from a throat or a
loudspeaker. Every speaker-verification model without a dedicated anti-spoofing
stage behaves this way. That is measured and recorded rather than glossed over,
and it is the reason the paragraph above matters.

Measured on synthetic voices: the same speaker on unseen phrases scores 0.57 to
0.70 against its profile, while other speakers score 0.16 to 0.51. The overlap
is real — voices from one synthesiser share a great deal — so the default
threshold is deliberately permissive and the honest calibration waits for
recordings of the actual owner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.profile import VoiceProfile, VoiceProfileStore
from atlas_voice.providers import VerificationResult, VoiceEngineError

__all__ = ["DEFAULT_THRESHOLD", "SherpaSpeaker"]

#: Cosine similarity above which a voice is treated as the owner's. Set low on
#: purpose: a missed match asks the person to repeat themselves, while a false
#: match only decides *whose* speech is listened to — the Policy Engine still
#: stands behind every action. Calibrate against real recordings before
#: tightening it.
DEFAULT_THRESHOLD = 0.55

#: Below this there is not enough voice to characterise. Verifying a quarter of
#: a second of audio produces a confident number about nothing.
MINIMUM_SECONDS = 0.8


def _prepare_dll_path() -> None:
    """Same Windows loader trap as the keyword spotter; see that module."""
    if sys.platform != "win32":
        return
    capi = Path(sys.prefix) / "Lib" / "site-packages" / "onnxruntime" / "capi"
    if capi.is_dir():
        os.add_dll_directory(str(capi))


class SherpaSpeaker:
    """Embeds a voice, and compares it against the enrolled profile."""

    name = "sherpa-speaker"

    def __init__(
        self,
        model_path: Path,
        *,
        store: VoiceProfileStore | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        num_threads: int = 1,
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1, exclusive")

        resolved = Path(model_path)
        if not resolved.is_file():
            raise VoiceEngineError(
                f"speaker embedding model not found at {resolved}. "
                "Run scripts/fetch_voice_models.ps1 to download it."
            )

        _prepare_dll_path()
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError(
                "sherpa-onnx is required for speaker verification; install atlas-voice[wake]"
            ) from exc

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(resolved), num_threads=num_threads, provider="cpu"
        )
        self._extractor: Any = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._store = store
        self._threshold = threshold
        self.model = resolved.name
        self._cached: VoiceProfile | None = None

    @property
    def dimensions(self) -> int:
        return int(self._extractor.dim)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        """A unit-length vector describing the voice in this audio."""
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        if len(samples) < MINIMUM_SECONDS * SAMPLE_RATE:
            raise VoiceEngineError(
                f"need at least {MINIMUM_SECONDS:.1f}s of speech to characterise a voice, "
                f"got {len(samples) / SAMPLE_RATE:.2f}s"
            )

        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        vector = np.array(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise VoiceEngineError("the embedding came back empty")
        return vector / norm

    def profile(self) -> VoiceProfile | None:
        if self._cached is None and self._store is not None:
            self._cached = self._store.load()
        return self._cached

    def forget(self) -> None:
        """Drop the cached profile, so a re-enrollment takes effect at once."""
        self._cached = None

    def verify(self, samples: np.ndarray) -> VerificationResult:
        profile = self.profile()
        if profile is None:
            raise VoiceEngineError("no voice profile is enrolled; run enrollment before verifying")
        if profile.dimensions != self.dimensions:
            # A profile from another embedding model is not merely stale: the
            # vectors would still compare, and the number would mean nothing.
            raise VoiceEngineError(
                f"the stored profile has {profile.dimensions} dimensions and this model "
                f"produces {self.dimensions}; re-enrollment is required"
            )

        score = float(np.dot(profile.embedding, self.embed(samples)))
        return VerificationResult(
            accepted=score >= self._threshold, score=score, threshold=self._threshold
        )
