"""Speech fixtures, synthesised once and cached.

Every model-backed test in this package needs real speech, because the failure
mode that matters is a stage that looks alive while answering nothing useful.
Silero scored 0.003 on speech and 0.004 on silence when it was fed the wrong
input shape — a suite that only asserted "silence is not speech" would have gone
green on a blind detector.

Piper produces the speech. It is *not* deterministic — see :func:`say` — so
what makes these fixtures reproducible is the cache, not the synthesiser: each
clip is generated once and read from disk forever after. They need no
microphone and cover both languages. They are cached under ``.models/fixtures``
because synthesis costs about two seconds per phrase; the directory is
gitignored, since these are generated artefacts and not source.

The module is named for itself rather than living in ``tests/__init__.py``:
``atlas-backend`` already publishes a ``tests`` package, and a second one
collides on import when the whole suite runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from atlas_voice.audio import SAMPLE_RATE, read_wav, resample, write_wav

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS = REPO_ROOT / ".models"
FIXTURES = MODELS / "fixtures"

SILERO = MODELS / "silero_vad.onnx"
PIPER_EN = MODELS / "piper" / "en_GB-alan-medium.onnx"
PIPER_RU = MODELS / "piper" / "ru_RU-dmitri-medium.onnx"

#: Phrases the whole M4 suite reuses: the wake word alone, the wake word with a
#: command attached, and near-misses that must *not* trigger it.
PHRASES: dict[str, tuple[Path, str]] = {
    "en_wake": (PIPER_EN, "Atlas."),
    "en_wake_command": (PIPER_EN, "Atlas, close Notepad."),
    "en_command": (PIPER_EN, "Open Chrome and show me memory usage."),
    "en_near_miss": (PIPER_EN, "At last, the Atlanta flight has landed."),
    "ru_wake": (PIPER_RU, "Атлас."),
    "ru_wake_command": (PIPER_RU, "Атлас, закрой блокнот."),
    "ru_command": (PIPER_RU, "Открой хром и покажи использование памяти."),
    "ru_near_miss": (PIPER_RU, "Атласные ткани и атлас мира лежали на столе."),
}

requires_silero = pytest.mark.skipif(
    not SILERO.is_file(),
    reason="run scripts/fetch_voice_models.ps1 to download the Silero VAD model",
)

requires_piper = pytest.mark.skipif(
    not (PIPER_EN.is_file() and PIPER_RU.is_file()),
    reason="run scripts/fetch_voice_models.ps1 to download the Piper voices",
)


#: A 904-speaker English model, so a test can ask "does this work for more than
#: one voice" — which for a wake word is most of the question.
PIPER_MULTI = MODELS / "piper" / "en_US-libritts_r-medium.onnx"

#: Loading a Piper voice costs about two seconds, and these are used per test.
_LOADED: dict[Path, object] = {}


def _voice(model: Path):  # type: ignore[no-untyped-def]
    from piper import PiperVoice

    if model not in _LOADED:
        _LOADED[model] = PiperVoice.load(str(model))
    return _LOADED[model]


def _synthesise(model: Path, text: str, *, speaker_id: int | None = None) -> np.ndarray:
    from piper import SynthesisConfig

    voice = _voice(model)
    options = SynthesisConfig(speaker_id=speaker_id) if speaker_id is not None else None
    chunks = list(voice.synthesize(text, syn_config=options))
    audio = np.concatenate([chunk.audio_float_array for chunk in chunks]).astype(np.float32)
    return resample(audio, from_rate=chunks[0].sample_rate, to_rate=SAMPLE_RATE)


def speech(name: str) -> np.ndarray:
    """Synthesised speech for ``name``, generated on first use and cached."""
    if name not in PHRASES:
        raise KeyError(f"unknown phrase {name!r}; known: {sorted(PHRASES)}")

    cached = FIXTURES / f"{name}.wav"
    if cached.is_file():
        return read_wav(cached)

    model, text = PHRASES[name]
    audio = _synthesise(model, text)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    write_wav(cached, audio)
    return audio


def say(model: Path, text: str, *, speaker_id: int | None = None) -> np.ndarray:
    """Any phrase, synthesised once and then read from disk forever after.

    Use this rather than :func:`_synthesise` in anything that asserts. Piper is
    **not deterministic**: the same sentence, the same voice and the same
    speaker produced clips of 27121, 28421, 26564 and 27121 samples on four
    consecutive calls, no two of them identical. VITS samples its own phoneme
    durations, and nothing in the API seeds that.

    The consequences were not subtle. The wake word fired on one rendition of a
    sentence and not the next, so an end-to-end suite passed and failed on the
    same code; Whisper transcribed one rendition as English and another as
    Russian. Both looked like bugs in the thing under test and neither was.
    Caching to disk is what makes a green run mean something — and it is also
    why a fixture that starts failing after a model upgrade should be deleted
    and regenerated rather than argued with.
    """
    stem = hashlib.sha256(f"{model.stem}|{speaker_id}|{text}".encode()).hexdigest()[:16]
    cached = FIXTURES / f"say_{stem}.wav"
    if cached.is_file():
        return read_wav(cached)

    audio = _synthesise(model, text, speaker_id=speaker_id)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    write_wav(cached, audio)
    return audio
