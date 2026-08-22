"""Measure sherpa-onnx keyword spotting on the same clips as openWakeWord.

Same audio, same groups, same metrics. ``build_trials`` takes a scorer so the
clips are generated once and handed to whichever engine is being measured —
regenerating them per engine would compare two exams rather than two engines,
a mistake this project already made once.

Two honest caveats travel with the numbers.

**The threshold axis is not the same quantity.** openWakeWord emits a
probability per window and a threshold is applied afterwards, so one pass yields
a whole curve. sherpa decides internally and reports a match, so its per-window
"score" here is 1.0 or 0.0 and the curve is flat by construction. Its own
``keywords_threshold`` was measured and only bites above about 0.9; the sweep
that matters for it is a separate axis, reported below the table.

**Russian is expected to fail.** Neither published model is trained on it. The
figure is measured and recorded rather than omitted, because "we did not test
it" and "it does not work" are different statements.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

import numpy as np
from config import CONFIG, SAMPLE_RATE
from evaluate import (
    build_trials,
    by_group,
    curve,
    summarise_latency,
)

from atlas_voice.audio import FRAME_SAMPLES
from atlas_voice.engines.sherpa_kws import KeywordModel, SherpaKeywordSpotter

MODELS = Path(__file__).resolve().parents[2] / ".models"

GIGASPEECH = KeywordModel(
    directory=MODELS / "kws" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
    encoder="encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    decoder="decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    joiner="joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    tokenisation="bpe",
)
ZH_EN = KeywordModel(
    directory=MODELS / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    encoder="encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    decoder="decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    joiner="joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    tokenisation="phone",
)
FAMILIES = {"gigaspeech": GIGASPEECH, "zh-en": ZH_EN}


def make_scorer(model: KeywordModel, phrases: tuple[str, ...], threshold: float):  # type: ignore[no-untyped-def]
    """A scorer for :func:`build_trials`, streaming in pipeline frames.

    One spotter, a fresh *stream* per clip. The decoder state lives in the
    stream, so resetting it isolates each clip completely — while rebuilding the
    whole spotter would reload three ONNX graphs 592 times and put model loading,
    not detection, on the critical path. The first version did exactly that and
    took longer than ten minutes to get nowhere.
    """
    detector = SherpaKeywordSpotter(model, phrases=phrases, threshold=threshold)

    def score(audio: np.ndarray, word_ends_at: float | None):  # type: ignore[no-untyped-def]
        detector.reset()
        # Stream time keeps running across clips by design, so a detection's
        # `at` has to be read relative to where this clip started. Without the
        # offset the reported latency is the whole accumulated stream: the first
        # run of this harness claimed a median of 166 seconds.
        started_at = detector.elapsed
        scores: list[float] = []
        latency: float | None = None

        for offset in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            detection = detector.push(audio[offset : offset + FRAME_SAMPLES])
            scores.append(1.0 if detection is not None else 0.0)
            if detection is not None and latency is None and word_ends_at is not None:
                latency = (detection.at - started_at) - word_ends_at

        # The transducer needs a little silence to commit; a clip simply ends.
        final = detector.flush()
        if final is not None:
            scores.append(1.0)
            if latency is None and word_ends_at is not None:
                latency = (final.at - started_at) - word_ends_at
        return scores, latency

    return score


_RESOURCE_PROBE = """
import json, sys, time
import numpy as np, psutil
sys.path.insert(0, {training!r})
from pathlib import Path
from atlas_voice.engines.sherpa_kws import KeywordModel, SherpaKeywordSpotter

model = KeywordModel(directory=Path({directory!r}), encoder={encoder!r},
                     decoder={decoder!r}, joiner={joiner!r}, tokenisation={tokenisation!r})
process = psutil.Process()
baseline = process.memory_info().rss
detector = SherpaKeywordSpotter(model, phrases=({phrase!r},))
loaded = process.memory_info().rss

chunk = np.zeros(512, dtype=np.float32)
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


def measure_resources(model: KeywordModel, phrase: str, seconds: int = 60) -> dict[str, float]:
    """In a clean interpreter, so the figure is the detector and not the harness."""
    source = _RESOURCE_PROBE.format(
        training=str(Path(__file__).resolve().parent),
        directory=str(model.directory),
        encoder=model.encoder,
        decoder=model.decoder,
        joiner=model.joiner,
        tokenisation=model.tokenisation,
        phrase=phrase,
        steps=int(seconds * SAMPLE_RATE / FRAME_SAMPLES),
        seconds=seconds,
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return {"error": completed.stderr.strip()[-300:]}  # type: ignore[dict-item]
    return dict(json.loads(completed.stdout))


def threshold_sweep(model: KeywordModel, phrases: tuple[str, ...]) -> list[dict[str, object]]:
    """sherpa's own threshold, on a small fixed probe set.

    Separate from the main table because it is a different axis: this parameter
    changes the detector, where openWakeWord's threshold filters a score the
    detector already produced.
    """
    from evaluate import pad, synthesise

    rng = random.Random(11)
    english = CONFIG.voices.english_multispeaker
    speakers = [11, 120, 200, 333, 470, 555, 640, 700, 777, 830, 900, 42]
    positives = [pad(synthesise(english, "Hey Jarvis", rng=rng, speaker_id=s)) for s in speakers]
    negatives = [
        pad(synthesise(english, text, rng=rng, speaker_id=11))
        for text in ("Travis", "Jargon", "Service", "starve us of detail", "Harvey")
    ]

    rows: list[dict[str, object]] = []
    for threshold in (0.10, 0.25, 0.50, 0.80, 0.90, 0.95, 0.99):
        score = make_scorer(model, phrases, threshold)
        heard = sum(any(s >= 0.5 for s in score(a, None)[0]) for a in positives)
        false = sum(any(s >= 0.5 for s in score(a, None)[0]) for a in negatives)
        rows.append(
            {
                "keywords_threshold": threshold,
                "heard": heard,
                "of": len(positives),
                "false": false,
                "negatives": len(negatives),
            }
        )
        print(
            f"    threshold {threshold:.2f}  heard {heard}/{len(positives)}  "
            f"false {false}/{len(negatives)}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=sorted(FAMILIES), default="zh-en")
    parser.add_argument("--phrase", default="HEY JARVIS")
    parser.add_argument("--also-bare", action="store_true", help="add bare JARVIS as a keyword")
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--report-threshold", type=float, default=0.5)
    parser.add_argument("--out", type=Path, default=None)
    arguments = parser.parse_args()

    model = FAMILIES[arguments.family]
    if not model.directory.is_dir():
        print(f"no model at {model.directory}; run scripts/fetch_voice_models.ps1")
        return 1

    phrases: tuple[str, ...] = (arguments.phrase,)
    if arguments.also_bare:
        phrases = (*phrases, "JARVIS")

    print(f"==> {arguments.family}, keywords {phrases}, threshold {arguments.threshold}")
    rng = random.Random(CONFIG.training.seed)
    trials = build_trials(
        Path("unused"),
        rng=rng,
        quick=arguments.quick,
        scorer=make_scorer(model, phrases, arguments.threshold),
    )

    thresholds = (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.99)
    report = {
        "engine": "sherpa-onnx",
        "family": arguments.family,
        "keywords": list(phrases),
        "keywords_threshold": arguments.threshold,
        "clips": len(trials),
        "operating_points": curve(trials, thresholds),
        "by_group_at_threshold": {
            "threshold": arguments.report_threshold,
            "groups": by_group(trials, arguments.report_threshold),
        },
        "latency_seconds": summarise_latency(trials),
        "resources_while_listening": measure_resources(model, phrases[0]),
    }

    print(f"\n{'threshold':>9} {'recall':>7} {'missed %':>9} {'false/hour':>11}")
    for row in report["operating_points"]:  # type: ignore[union-attr]
        print(
            f"{row['threshold']:>9.2f} {row['recall']:>7.3f} "  # type: ignore[index]
            f"{row['missed_percent']:>9.2f} {row['false_activations_per_hour']:>11.2f}"  # type: ignore[index]
        )

    print("\n==> sherpa's own keywords_threshold (a different axis)")
    report["keywords_threshold_sweep"] = threshold_sweep(model, phrases)

    destination = arguments.out or (
        CONFIG.metrics_dir / f"acceptance_sherpa_{arguments.family}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
