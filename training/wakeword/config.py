"""Every parameter of the "Atlas" wake-word training run, in one place.

Kept in the repository because a model you cannot rebuild is a liability: when
the false-accept rate turns out to be wrong in six months, the question will be
*what was it trained on*, and the answer has to be readable rather than
remembered.

The audio datasets and the 16 GB of precomputed negative features are **not**
kept — they are downloadable, they are not ours, and they would dwarf the
repository. `fetch.py` reproduces them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS = REPO_ROOT / ".models"
WORK = REPO_ROOT / ".training" / "wakeword"

# Shared feature stack — the same two graphs the runtime uses. Training against
# different feature extraction than inference is the classic way to produce a
# model that scores beautifully and fails in the room.
MELSPECTROGRAM = MODELS / "oww" / "melspectrogram.onnx"
EMBEDDING = MODELS / "oww" / "embedding_model.onnx"

SAMPLE_RATE = 16_000
#: 16 embeddings of 96 values: exactly what the classifier reads, and therefore
#: exactly what one training sample is.
CLF_WINDOW = 16
EMBEDDING_SIZE = 96
#: Samples that yield precisely CLF_WINDOW embeddings. The melspectrogram graph
#: emits n/160 - 3 frames; 16 embeddings need 76 + 15*8 = 196 of them.
CLIP_SAMPLES = (196 + 3) * 160  # 31_840, just under two seconds


@dataclass(frozen=True)
class Voices:
    """Which synthetic speakers produce the positive examples.

    English gets a genuinely multi-speaker model. Russian has only four
    single-speaker voices published, so the Russian half of the training set is
    thinner in speaker identity and leans harder on prosody and augmentation.
    That imbalance is a known weakness of this run and the reason the acceptance
    test reports «Атлас» separately from "Atlas" rather than averaging them.
    """

    english_multispeaker: str = "en_US-libritts_r-medium"
    english_speaker_count: int = 904
    russian: tuple[str, ...] = (
        "ru_RU-dmitri-medium",
        "ru_RU-denis-medium",
        "ru_RU-irina-medium",
        "ru_RU-ruslan-medium",
    )


@dataclass(frozen=True)
class Phrases:
    """What counts as the wake word, and what must never be mistaken for it."""

    #: Said alone, as a person summoning an assistant does.
    positive_en: tuple[str, ...] = ("Atlas", "Atlas.", "Atlas?", "Hey Atlas")
    positive_ru: tuple[str, ...] = ("Атлас", "Атлас.", "Атлас?", "Эй, Атлас")

    #: Near-misses. These are the expensive negatives: a model that has never
    #: heard "at last" will happily fire on it, and no amount of unrelated
    #: podcast audio teaches it otherwise. «атлас» as an ordinary noun matters
    #: especially — a Russian speaker says it in normal conversation.
    hard_negative_en: tuple[str, ...] = (
        "At last",
        "At last, we are done",
        "Atlanta",
        "The Atlantic",
        "Atlas Shrugged is a novel",
        "A road atlas",
        "Atlassian",
        "That last one",
        "Add less salt",
    )
    hard_negative_ru: tuple[str, ...] = (
        "Атласные ткани",
        "Атлас мира лежал на столе",
        "Атлантика",
        "Атлант расправил плечи",
        "Географический атлас",
        "Атласный бант",
        "Класс",
        "Ананас",
        "Он летал в Атланту",
    )


@dataclass(frozen=True)
class Synthesis:
    """How much the synthetic voices are varied.

    Piper's own defaults produce one careful reading per speaker, and a model
    trained on careful readings hears a careful reading. Real summoning is
    faster, flatter and often half-swallowed.
    """

    #: Speaking rate. Above 1 is slower; the range covers a hurried "Atlas!"
    #: through a deliberate one.
    length_scale: tuple[float, float] = (0.75, 1.35)
    #: Piper's prosody randomness, widened from its 0.667 default.
    noise_scale: tuple[float, float] = (0.4, 0.9)
    #: Phoneme duration randomness, widened from its 0.8 default.
    noise_w: tuple[float, float] = (0.4, 1.1)

    positives_en: int = 12_000
    positives_ru: int = 12_000
    hard_negatives_en: int = 4_000
    hard_negatives_ru: int = 4_000


@dataclass(frozen=True)
class Augmentation:
    """Making synthetic speech survive a real room.

    A clip straight out of a synthesiser is anechoic, evenly loud, and centred
    in the frame. A microphone two metres away in a room with a fan hears none
    of those things.
    """

    #: Fraction of clips convolved with a measured room impulse response.
    reverb_probability: float = 0.6
    #: Fraction mixed with recorded noise, at signal-to-noise ratios spanning
    #: a quiet study through a busy street.
    noise_probability: float = 0.8
    snr_db: tuple[float, float] = (0.0, 25.0)
    #: Overall level, so loudness is never a cue the model can lean on.
    gain_db: tuple[float, float] = (-22.0, -3.0)
    #: Where the word sits inside the two-second window. The classifier should
    #: fire as the word completes, so the end is jittered near — but not at —
    #: the window's end.
    word_end_fraction: tuple[float, float] = (0.72, 0.98)


@dataclass(frozen=True)
class Training:
    batch_size: int = 1024
    epochs: int = 12
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    #: Negatives vastly outnumber positives, which is correct — the world is
    #: mostly not the wake word — but unweighted training then learns to answer
    #: "no" and stop. Positives are oversampled per batch instead of reweighting
    #: the loss, so every batch contains real gradient from both classes.
    positive_fraction: float = 0.25
    #: How many of the 5-million-odd ACAV100M windows to use. All of them is
    #: the point of the full run.
    negative_windows: int | None = None
    seed: int = 20260815

    #: The operating points reported by the acceptance test. Not one "best"
    #: threshold: the trade-off is the user's to make, and it cannot be made
    #: from a single number.
    thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99)


@dataclass(frozen=True)
class Config:
    voices: Voices = field(default_factory=Voices)
    phrases: Phrases = field(default_factory=Phrases)
    synthesis: Synthesis = field(default_factory=Synthesis)
    augmentation: Augmentation = field(default_factory=Augmentation)
    training: Training = field(default_factory=Training)

    @property
    def piper_dir(self) -> Path:
        return MODELS / "piper"

    @property
    def rir_dir(self) -> Path:
        return WORK / "rir"

    @property
    def noise_dir(self) -> Path:
        return WORK / "noise"

    @property
    def negative_features(self) -> Path:
        return WORK / "acav100m_features.npy"

    @property
    def validation_features(self) -> Path:
        return WORK / "validation_features.npy"

    @property
    def clips_dir(self) -> Path:
        return WORK / "clips"

    @property
    def features_dir(self) -> Path:
        return WORK / "features"

    @property
    def output_model(self) -> Path:
        return MODELS / "oww" / "atlas_v1.onnx"

    @property
    def metrics_dir(self) -> Path:
        return Path(__file__).resolve().parent / "metrics"


CONFIG = Config()
