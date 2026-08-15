"""The acceptance test: does this model work in a room, not in a notebook.

Training accuracy is not evidence. With balanced batches it sits near 99% for a
model that fires on every sibilant, and the number that matters — how often the
thing wakes up when nobody called it — cannot be read off it at all.

So this measures the model the way it will be used: streamed through the real
runtime, at several thresholds, over audio chosen to be hostile. It reports a
*curve*, not a verdict. Choosing the operating point is a judgement about how
annoying a false wake is against how annoying a missed one is, and that is not
the training script's call to make.

Everything here except the "real voice" section runs on synthesised speech,
which is a genuine limitation and is labelled as one: a synthesiser trained on
read speech is not a person calling across a room. The final word on whether
the wake word is usable belongs to recordings of the actual owner.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from config import CONFIG, EMBEDDING, MELSPECTROGRAM, SAMPLE_RATE
from features import load_impulse_responses, load_noise_pool, mix_noise, reverberate
from tqdm import tqdm

from atlas_voice.audio import resample
from atlas_voice.engines.openwakeword import OpenWakeWord

#: Classifier windows per second of audio: one per 1280-sample step.
WINDOWS_PER_SECOND = SAMPLE_RATE / 1280


def spotter(model: Path, threshold: float = 0.5) -> OpenWakeWord:
    return OpenWakeWord(
        model,
        melspectrogram_path=MELSPECTROGRAM,
        embedding_path=EMBEDDING,
        threshold=threshold,
        label=CONFIG.phrases.spoken_en.lower(),
    )


@dataclass
class Trial:
    """One audio clip, scored once, reusable at every threshold."""

    name: str
    group: str
    contains_wake_word: bool
    duration_s: float
    scores: list[float] = field(default_factory=list)
    #: Seconds from the end of the spoken word to the first window over 0.5.
    latency_s: float | None = None

    @property
    def peak(self) -> float:
        return max(self.scores) if self.scores else 0.0


#: Loading a Piper voice takes about two seconds, and the first version of this
#: file did it once per clip — which put the synthesiser, not the detector, on
#: the critical path of the acceptance run.
_VOICES: dict[str, object] = {}


def _voice(name: str):  # type: ignore[no-untyped-def]
    from piper import PiperVoice

    if name not in _VOICES:
        _VOICES[name] = PiperVoice.load(str(CONFIG.piper_dir / f"{name}.onnx"))
    return _VOICES[name]


def noise_like(noise: np.ndarray, length: int) -> np.ndarray:
    """Noise of exactly ``length`` samples, repeated if the segment is shorter.

    The noise pool holds two-second segments and the padded test clips are
    longer, so slicing alone silently produces a shape mismatch — which is what
    it did, one array at a time, until this run caught it.
    """
    if len(noise) >= length:
        return noise[:length]
    repeats = int(np.ceil(length / max(len(noise), 1)))
    return np.tile(noise, repeats)[:length]


def synthesise(voice_name: str, text: str, *, rng: random.Random, **overrides: float) -> np.ndarray:
    from piper import SynthesisConfig

    voice = _voice(voice_name)
    options = SynthesisConfig(
        speaker_id=overrides.get("speaker_id"),  # type: ignore[arg-type]
        length_scale=overrides.get("length_scale", 1.0),
        volume=overrides.get("volume", 1.0),
    )
    chunks = list(voice.synthesize(text, syn_config=options))
    audio = np.concatenate([chunk.audio_float_array for chunk in chunks]).astype(np.float32)
    return resample(audio, from_rate=chunks[0].sample_rate, to_rate=SAMPLE_RATE)


def pad(audio: np.ndarray, *, before: float = 1.0, after: float = 1.0) -> np.ndarray:
    return np.concatenate(
        [
            np.zeros(int(before * SAMPLE_RATE), dtype=np.float32),
            audio,
            np.zeros(int(after * SAMPLE_RATE), dtype=np.float32),
        ]
    )


def run_trial(
    model: Path, audio: np.ndarray, *, word_ends_at: float | None
) -> tuple[list[float], float | None]:
    """Score a clip and, when the word's end time is known, measure latency."""
    detector = spotter(model, threshold=0.5)
    timed = detector.scores_with_times(audio)

    latency = None
    if word_ends_at is not None:
        for detected_at, score in timed:
            if score >= 0.5:
                # The detector's own clock, not arithmetic on the index. The
                # first version derived the time from the index and produced
                # negative latencies — detections apparently arriving before
                # the word they detect.
                latency = detected_at - word_ends_at
                break
    return [score for _, score in timed], latency


#: Run in a fresh interpreter so the memory figure describes the detector and
#: not this process. Measured in-process it read 2.6 GB, which is torch, Piper
#: and the feature arrays — a number that would have been quoted as the cost of
#: listening and been wrong by three orders of magnitude.
_RESOURCE_PROBE = """
import json, sys, time
import numpy as np, psutil
sys.path.insert(0, {training!r})
from atlas_voice.engines.openwakeword import OpenWakeWord

process = psutil.Process()
baseline = process.memory_info().rss
detector = OpenWakeWord({model!r}, melspectrogram_path={mel!r},
                        embedding_path={embed!r}, label="wake")
loaded = process.memory_info().rss

chunk = np.zeros(1280, dtype=np.float32)
steps = {steps}
process.cpu_percent(interval=None)
started = time.perf_counter()
for _ in range(steps):
    detector.push(chunk)
elapsed = time.perf_counter() - started
cpu = process.cpu_percent(interval=None)

print(json.dumps({{
    "audio_seconds_processed": {seconds},
    "wall_seconds": round(elapsed, 2),
    "realtime_factor": round(elapsed / {seconds}, 4),
    "cpu_percent_of_one_core": round(cpu, 1),
    "rss_at_start_mb": round(baseline / 1e6, 1),
    "rss_after_loading_models_mb": round(loaded / 1e6, 1),
    "rss_after_listening_mb": round(process.memory_info().rss / 1e6, 1),
    "models_cost_mb": round((loaded - baseline) / 1e6, 1),
}}))
"""


def measure_resources(model: Path, seconds: int = 60) -> dict[str, float]:
    """CPU and memory while listening to silence, which is the normal case."""
    import subprocess

    source = _RESOURCE_PROBE.format(
        training=str(Path(__file__).resolve().parent),
        model=str(model),
        mel=str(MELSPECTROGRAM),
        embed=str(EMBEDDING),
        steps=int(seconds * WINDOWS_PER_SECOND),
        seconds=seconds,
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return {"error": completed.stderr.strip()[-300:]}  # type: ignore[dict-item]
    return dict(json.loads(completed.stdout))


def curve(trials: list[Trial], thresholds: tuple[float, ...]) -> list[dict[str, object]]:
    """The trade-off, at every operating point."""
    positives = [t for t in trials if t.contains_wake_word]
    negatives = [t for t in trials if not t.contains_wake_word]
    negative_windows = sum(len(t.scores) for t in negatives)
    negative_hours = negative_windows / WINDOWS_PER_SECOND / 3600

    rows: list[dict[str, object]] = []
    for threshold in thresholds:
        detected = sum(1 for t in positives if t.peak >= threshold)
        false_windows = sum(int(np.sum(np.array(t.scores) >= threshold)) for t in negatives)
        rows.append(
            {
                "threshold": threshold,
                "detected": detected,
                "of": len(positives),
                "recall": round(detected / max(len(positives), 1), 4),
                "missed_percent": round(100 * (1 - detected / max(len(positives), 1)), 2),
                "false_windows": false_windows,
                "false_activations_per_hour": round(false_windows / max(negative_hours, 1e-9), 2),
            }
        )
    return rows


def by_group(trials: list[Trial], threshold: float) -> dict[str, dict[str, object]]:
    groups: dict[str, list[Trial]] = {}
    for trial in trials:
        groups.setdefault(trial.group, []).append(trial)

    summary: dict[str, dict[str, object]] = {}
    for name, members in sorted(groups.items()):
        wake = members[0].contains_wake_word
        if wake:
            hit = sum(1 for t in members if t.peak >= threshold)
            summary[name] = {
                "clips": len(members),
                "detected": hit,
                "recall": round(hit / len(members), 3),
                "median_peak": round(float(np.median([t.peak for t in members])), 4),
            }
        else:
            fired = sum(1 for t in members if t.peak >= threshold)
            windows = sum(len(t.scores) for t in members)
            hours = windows / WINDOWS_PER_SECOND / 3600
            summary[name] = {
                "clips": len(members),
                "clips_that_fired": fired,
                "hours": round(hours, 3),
                "false_activations_per_hour": round(
                    sum(int(np.sum(np.array(t.scores) >= threshold)) for t in members)
                    / max(hours, 1e-9),
                    2,
                ),
                "median_peak": round(float(np.median([t.peak for t in members])), 4),
            }
    return summary


def build_trials(model: Path, *, rng: random.Random, quick: bool) -> list[Trial]:
    trials: list[Trial] = []
    impulses = load_impulse_responses(CONFIG.rir_dir)
    noises = load_noise_pool(CONFIG.noise_dir, segments=200 if quick else 800, rng=rng)

    def add(name: str, group: str, audio: np.ndarray, *, wake: bool, ends_at: float | None) -> None:
        scores, latency = run_trial(model, audio, word_ends_at=ends_at)
        trials.append(
            Trial(
                name=name,
                group=group,
                contains_wake_word=wake,
                duration_s=len(audio) / SAMPLE_RATE,
                scores=scores,
                latency_s=latency,
            )
        )

    wake_en = CONFIG.phrases.spoken_en
    wake_ru = CONFIG.phrases.spoken_ru
    english = CONFIG.voices.english_multispeaker
    speakers = [
        rng.randrange(CONFIG.voices.english_speaker_count) for _ in range(6 if quick else 25)
    ]
    russian_voices = CONFIG.voices.russian

    # --- the wake word, said plainly, in both languages -------------------
    for speaker in tqdm(speakers, desc="en wake", unit="clip"):
        word = synthesise(english, wake_en, rng=rng, speaker_id=speaker)
        add(
            f"en_wake_s{speaker}",
            "wake_word_english",
            pad(word),
            wake=True,
            ends_at=1.0 + len(word) / SAMPLE_RATE,
        )

    for voice in tqdm(russian_voices, desc="ru wake", unit="voice"):
        for _ in range(2 if quick else 7):
            word = synthesise(voice, wake_ru, rng=rng)
            add(
                f"ru_wake_{voice}",
                "wake_word_russian",
                pad(word),
                wake=True,
                ends_at=1.0 + len(word) / SAMPLE_RATE,
            )

    # --- quiet, ordinary and loud ----------------------------------------
    for label, volume in (("quiet", 0.12), ("normal", 0.7), ("loud", 1.0)):
        for speaker in speakers[:4]:
            word = synthesise(english, wake_en, rng=rng, speaker_id=speaker, volume=volume)
            add(
                f"en_wake_{label}_s{speaker}",
                f"loudness_{label}",
                pad(word),
                wake=True,
                ends_at=1.0 + len(word) / SAMPLE_RATE,
            )

    # --- distance, approximated by reverberation and level ----------------
    for label, gain, wet in (("near", 0.7, False), ("mid", 0.25, True), ("far", 0.06, True)):
        for speaker in speakers[:4]:
            word = synthesise(english, wake_en, rng=rng, speaker_id=speaker)
            audio = pad(word)
            if wet and impulses:
                audio = reverberate(audio, rng.choice(impulses))
            peak = np.abs(audio).max()
            if peak > 0:
                audio = (audio / peak * gain).astype(np.float32)
            add(
                f"en_wake_{label}_s{speaker}",
                f"distance_{label}",
                audio,
                wake=True,
                ends_at=1.0 + len(word) / SAMPLE_RATE,
            )

    # --- the wake word over background noise ------------------------------
    for snr in (20.0, 10.0, 5.0):
        for speaker in speakers[:4]:
            word = synthesise(english, wake_en, rng=rng, speaker_id=speaker)
            audio = pad(word)
            if noises:
                audio = mix_noise(audio, noise_like(rng.choice(noises), len(audio)), snr)
            add(
                f"en_wake_snr{int(snr)}_s{speaker}",
                f"noisy_snr{int(snr)}",
                audio,
                wake=True,
                ends_at=1.0 + len(word) / SAMPLE_RATE,
            )

    # --- near-misses -------------------------------------------------------
    for text in CONFIG.phrases.hard_negative_en:
        for speaker in speakers[: 2 if quick else 5]:
            audio = synthesise(english, text, rng=rng, speaker_id=speaker)
            add(
                f"en_near_{text[:20]}_s{speaker}",
                "near_miss_english",
                pad(audio),
                wake=False,
                ends_at=None,
            )

    for text in CONFIG.phrases.hard_negative_ru:
        for voice in russian_voices[: 2 if quick else 4]:
            audio = synthesise(voice, text, rng=rng)
            add(
                f"ru_near_{text[:20]}_{voice}",
                "near_miss_russian",
                pad(audio),
                wake=False,
                ends_at=None,
            )

    # --- ordinary speech, both languages ----------------------------------
    english_background = [
        "Could you open the second document and check the totals for me please.",
        "I was thinking we should probably leave before the traffic gets bad.",
        "The meeting has been moved to Thursday afternoon, which suits everyone.",
        "At the last minute they added another item to the agenda.",
    ]
    russian_background = [
        "Мне кажется, нам стоит выехать пораньше, пока нет пробок.",
        "Совещание перенесли на четверг, и это всех устраивает.",
        "Он положил на стол атлас и начал листать страницы.",
        "В последний момент добавили ещё один пункт в повестку.",
    ]
    for text in english_background:
        add(
            f"en_bg_{text[:16]}",
            "background_speech_english",
            pad(synthesise(english, text, rng=rng, speaker_id=speakers[0])),
            wake=False,
            ends_at=None,
        )
    for text in russian_background:
        add(
            f"ru_bg_{text[:16]}",
            "background_speech_russian",
            pad(synthesise(russian_voices[0], text, rng=rng)),
            wake=False,
            ends_at=None,
        )

    # --- non-speech background: music and street noise --------------------
    for index, noise in enumerate(noises[: 20 if quick else 120]):
        stretched = np.tile(noise, 3)[: SAMPLE_RATE * 6]
        add(f"noise_{index}", "background_noise", stretched, wake=False, ends_at=None)

    return trials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=CONFIG.output_model)
    parser.add_argument("--quick", action="store_true", help="a smaller sweep, for a smoke test")
    parser.add_argument("--report-threshold", type=float, default=0.5)
    arguments = parser.parse_args()

    if not arguments.model.is_file():
        print(f"no model at {arguments.model}")
        return 1

    rng = random.Random(CONFIG.training.seed + 2)
    trials = build_trials(arguments.model, rng=rng, quick=arguments.quick)

    latencies = [t.latency_s for t in trials if t.latency_s is not None]
    report = {
        "model": str(arguments.model.name),
        "clips": len(trials),
        "operating_points": curve(trials, CONFIG.training.thresholds),
        "by_group_at_threshold": {
            "threshold": arguments.report_threshold,
            "groups": by_group(trials, arguments.report_threshold),
        },
        "latency_seconds": {
            "measured_on": len(latencies),
            "median": round(float(np.median(latencies)), 3) if latencies else None,
            "p90": round(float(np.percentile(latencies, 90)), 3) if latencies else None,
            "max": round(float(np.max(latencies)), 3) if latencies else None,
        },
        "resources_while_listening": measure_resources(arguments.model),
        "caveat": (
            "Synthesised speech throughout. A synthesiser trained on read speech is "
            "not a person calling across a room; the owner's own recordings decide "
            "whether this is usable."
        ),
    }

    CONFIG.metrics_dir.mkdir(parents=True, exist_ok=True)
    destination = CONFIG.metrics_dir / "acceptance.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'threshold':>10} {'recall':>8} {'missed %':>9} {'false/hour':>11}")
    for row in report["operating_points"]:  # type: ignore[index]
        print(
            f"{row['threshold']:>10.2f} {row['recall']:>8.3f} "  # type: ignore[index]
            f"{row['missed_percent']:>9.2f} {row['false_activations_per_hour']:>11.2f}"  # type: ignore[index]
        )
    print(f"\nwrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
