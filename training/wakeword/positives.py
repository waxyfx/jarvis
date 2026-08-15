"""Synthesise the spoken examples: "Atlas", «Атлас», and their near-misses.

Piper's defaults produce one careful reading per speaker, and a model trained on
careful readings recognises careful readings. Nobody summons an assistant
carefully — the word comes out fast, flat, and half-swallowed. So every clip
draws its own speaking rate and prosody randomness, and English draws its own
speaker from a 904-voice model.

The near-misses are generated here too, and they earn their place. Unrelated
podcast audio never teaches a model that "at last" is not "Atlas"; only "at
last" does. The Russian list matters more still, because «атлас» is an ordinary
noun that a Russian speaker says in ordinary conversation.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from config import CONFIG, SAMPLE_RATE
from tqdm import tqdm

from atlas_voice.audio import resample, write_wav


@dataclass(frozen=True)
class Sample:
    path: Path
    text: str
    language: str
    voice: str
    speaker: int | None
    length_scale: float
    is_positive: bool


def _voice_loader():  # type: ignore[no-untyped-def]
    """Load each Piper voice once. Loading dominates otherwise."""
    from piper import PiperVoice

    cache: dict[str, object] = {}

    def load(name: str):  # type: ignore[no-untyped-def]
        if name not in cache:
            cache[name] = PiperVoice.load(str(CONFIG.piper_dir / f"{name}.onnx"))
        return cache[name]

    return load


def synthesise(voice, text: str, *, speaker: int | None, rng: random.Random) -> np.ndarray:  # type: ignore[no-untyped-def]
    from piper import SynthesisConfig

    tuning = CONFIG.synthesis
    options = SynthesisConfig(
        speaker_id=speaker,
        length_scale=rng.uniform(*tuning.length_scale),
        noise_scale=rng.uniform(*tuning.noise_scale),
        noise_w_scale=rng.uniform(*tuning.noise_w),
    )
    chunks = list(voice.synthesize(text, syn_config=options))
    audio = np.concatenate([chunk.audio_float_array for chunk in chunks]).astype(np.float32)
    return resample(audio, from_rate=chunks[0].sample_rate, to_rate=SAMPLE_RATE)


def trim_silence(audio: np.ndarray, *, threshold: float = 0.01) -> np.ndarray:
    """Cut leading and trailing quiet.

    Where the word *ends* is the alignment anchor for a training window, so the
    synthesiser's variable trailing silence has to go — otherwise the model
    learns the padding rather than the word.
    """
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if len(loud) == 0:
        return audio
    return audio[loud[0] : loud[-1] + 1]


def generate(
    *,
    texts: tuple[str, ...],
    count: int,
    language: str,
    voices: tuple[str, ...],
    speaker_count: int | None,
    is_positive: bool,
    out_dir: Path,
    rng: random.Random,
    load,  # type: ignore[no-untyped-def]
    label: str,
) -> list[Sample]:
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Sample] = []

    for index in tqdm(range(count), desc=label, unit="clip"):
        voice_name = rng.choice(voices)
        speaker = rng.randrange(speaker_count) if speaker_count else None
        text = rng.choice(texts)
        tuning = CONFIG.synthesis
        length_scale = rng.uniform(*tuning.length_scale)

        audio = trim_silence(synthesise(load(voice_name), text, speaker=speaker, rng=rng))
        if len(audio) < SAMPLE_RATE // 10:
            # Under 100 ms is a failed synthesis, not a fast talker.
            continue

        path = out_dir / f"{language}_{index:06d}.wav"
        write_wav(path, audio)
        produced.append(
            Sample(
                path=path,
                text=text,
                language=language,
                voice=voice_name,
                speaker=speaker,
                length_scale=length_scale,
                is_positive=is_positive,
            )
        )

    return produced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale", type=float, default=1.0, help="fraction of the configured counts"
    )
    parser.add_argument(
        "--only",
        default="",
        help=(
            "comma-separated groups to regenerate (e.g. negative_ru); every other "
            "group is kept from the existing manifest instead of being resynthesised"
        ),
    )
    arguments = parser.parse_args()
    wanted = {name.strip() for name in arguments.only.split(",") if name.strip()}

    rng = random.Random(CONFIG.training.seed)
    load = _voice_loader()
    tuning = CONFIG.synthesis
    phrases = CONFIG.phrases
    voices = CONFIG.voices

    def scaled(n: int) -> int:
        return max(1, round(n * arguments.scale))

    groups = [
        (
            "positive_en",
            phrases.positive_en,
            scaled(tuning.positives_en),
            "en",
            (voices.english_multispeaker,),
            voices.english_speaker_count,
            True,
        ),
        (
            "positive_ru",
            phrases.positive_ru,
            scaled(tuning.positives_ru),
            "ru",
            voices.russian,
            None,
            True,
        ),
        (
            "negative_en",
            phrases.hard_negative_en,
            scaled(tuning.hard_negatives_en),
            "en",
            (voices.english_multispeaker,),
            voices.english_speaker_count,
            False,
        ),
        (
            "negative_ru",
            phrases.hard_negative_ru,
            scaled(tuning.hard_negatives_ru),
            "ru",
            voices.russian,
            None,
            False,
        ),
    ]

    manifest: list[dict[str, object]] = []
    existing_path = CONFIG.clips_dir / "manifest.json"
    if wanted and existing_path.is_file():
        kept = [
            entry
            for entry in json.loads(existing_path.read_text(encoding="utf-8"))
            if entry["group"] not in wanted
        ]
        manifest.extend(kept)
        print(f"keeping {len(kept)} clips from groups outside {sorted(wanted)}")

    for name, texts, count, language, voice_names, speakers, positive in groups:
        if wanted and name not in wanted:
            continue
        # A regenerated group must not inherit stragglers from the old one: a
        # shorter list would leave clips the manifest no longer mentions, and a
        # longer one would mix two vocabularies under a single label.
        stale = CONFIG.clips_dir / name
        if stale.is_dir():
            for leftover in stale.glob("*.wav"):
                leftover.unlink()

        samples = generate(
            texts=texts,
            count=count,
            language=language,
            voices=voice_names,
            speaker_count=speakers,
            is_positive=positive,
            out_dir=CONFIG.clips_dir / name,
            rng=rng,
            load=load,
            label=name,
        )
        manifest.extend(
            {
                "path": str(sample.path.relative_to(CONFIG.clips_dir)),
                "text": sample.text,
                "language": sample.language,
                "voice": sample.voice,
                "speaker": sample.speaker,
                "length_scale": round(sample.length_scale, 3),
                "positive": sample.is_positive,
                "group": name,
            }
            for sample in samples
        )

    CONFIG.clips_dir.mkdir(parents=True, exist_ok=True)
    (CONFIG.clips_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    positives = sum(1 for entry in manifest if entry["positive"])
    print(f"\n{len(manifest)} clips: {positives} positive, {len(manifest) - positives} negative")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
