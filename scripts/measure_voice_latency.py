"""How long JARVIS takes to answer, and how much of the machine it holds.

Two questions, and they are not the same one.

**Can it keep up?** The listening path — wake word and voice activity — runs
every waking second, so it must process audio faster than the audio arrives and
must not cost enough to notice. That is measured as a realtime factor and a
share of one core, over a minute of audio.

**How long is the wait?** From the end of a spoken command to the first sound of
the answer. That total is made of parts that behave differently: the segmenter's
end-of-speech wait is a fixed design choice, Whisper scales with how long the
utterance was, and the model is a network call whose spread matters more than
its median. They are reported separately, because a total alone tells you
nothing about which one to attack.

Nothing here asserts. It measures, prints, and writes a JSON record — the point
is a number to argue with, and a rerun after a change that can be compared
against it.

The model's own latency is deliberately not measured here. It is a network
call to someone else's service, it varies by more than everything else combined,
and e2e/test_gemini_live.py already exercises it against the real API. Timing it
in the same run would bury the parts this machine actually controls.

    uv run python scripts/measure_voice_latency.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "atlas-voice" / "tests"))

from atlas_agent.voice_runtime import (  # noqa: E402
    WAKE_KEYWORDS,
    WAKE_THRESHOLD,
    VoiceModels,
)
from atlas_shared.enums import Language  # noqa: E402
from atlas_voice.audio import SAMPLE_RATE, frames_from_array  # noqa: E402
from atlas_voice.engines.piper_tts import (  # noqa: E402
    ACKNOWLEDGEMENTS,
    PiperTTS,
    VoiceChoice,
)
from atlas_voice.engines.sherpa_kws import SherpaKeywordSpotter  # noqa: E402
from atlas_voice.engines.silero import SileroVAD  # noqa: E402
from atlas_voice.engines.whisper import WhisperSettings, WhisperSTT  # noqa: E402
from voicefixtures import PIPER_MULTI, PIPER_RU, say  # noqa: E402

MODELS = VoiceModels(root=REPO / ".models")
RESULTS = REPO / "docs" / "measurements"

#: Commands of different lengths, because Whisper's cost tracks the utterance
#: and a single sample would report one point on a slope as if it were flat.
UTTERANCES: tuple[tuple[str, str, int | None], ...] = (
    ("en_short", "Open Notepad.", 200),
    ("en_medium", "Open Notepad and show me how much memory is left.", 200),
    (
        "en_long",
        "Open Chrome, then show me how much disk space is left on this machine, please.",
        333,
    ),
    ("ru_short", "Открой блокнот.", None),
    ("ru_medium", "Открой блокнот и покажи, сколько осталось памяти.", None),
)


@dataclass
class Samples:
    """Repeated timings for one stage, summarised the way latency deserves."""

    name: str
    unit: str = "ms"
    values: list[float] = field(default_factory=list)

    def add(self, seconds: float) -> None:
        self.values.append(seconds * 1000.0)

    def summary(self) -> dict[str, Any]:
        if not self.values:
            return {"name": self.name, "n": 0}
        ordered = sorted(self.values)
        return {
            "name": self.name,
            "unit": self.unit,
            "n": len(ordered),
            "median": round(statistics.median(ordered), 1),
            # The worst case is what people remember, so it is reported rather
            # than smoothed away into an average.
            "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
            "max": round(ordered[-1], 1),
        }


def audio_for(text: str, speaker: int | None) -> np.ndarray:
    model = PIPER_MULTI if speaker is not None else PIPER_RU
    return say(model, text, speaker_id=speaker)


def measure_listening(seconds: int = 60) -> dict[str, Any]:
    """What the always-on half costs, over a minute of ordinary audio.

    The clip is speech rather than silence on purpose: a detector fed silence
    can be arbitrarily cheap and tells you nothing about the case that matters.
    """
    import psutil

    speech = audio_for("Open Notepad and show me how much memory is left.", 200)
    repeats = int(seconds * SAMPLE_RATE / len(speech)) + 1
    stream = np.tile(speech, repeats)[: seconds * SAMPLE_RATE]

    wake = SherpaKeywordSpotter(MODELS.wake, phrases=WAKE_KEYWORDS, threshold=WAKE_THRESHOLD)
    vad = SileroVAD(MODELS.vad)

    process = psutil.Process()
    process.cpu_percent(None)
    before = process.memory_info().rss

    started = time.perf_counter()
    for frame in frames_from_array(stream):
        wake.push(frame.samples)
        vad.is_speech(frame.samples)
    elapsed = time.perf_counter() - started

    cpu = process.cpu_percent(None)
    return {
        "audio_seconds": seconds,
        "wall_seconds": round(elapsed, 2),
        # Below 1.0 means it keeps up; the margin is the headroom for everything
        # else the machine is doing.
        "realtime_factor": round(elapsed / seconds, 4),
        # The honest figure for "what does listening cost". The models saturate
        # a core while they run and then stop, so the instantaneous reading is
        # near 100% and means almost nothing; what matters is that they run for
        # two seconds out of every sixty.
        "duty_cycle_percent_of_one_core": round(100 * elapsed / seconds, 2),
        "cpu_percent_while_running": round(cpu, 1),
        "rss_growth_mb": round((process.memory_info().rss - before) / 1e6, 1),
    }


async def measure_stages(*, rounds: int) -> dict[str, Any]:
    """Whisper, the model and Piper, timed one utterance at a time."""
    stt = WhisperSTT(WhisperSettings())
    # Warmed exactly as the runtime warms it. Without this the first Russian
    # reply pays 1.9 s to load a voice, which would be reported as the cost of
    # speaking Russian rather than the cost of doing it for the first time.
    tts = PiperTTS(
        VoiceChoice(directory=MODELS.piper_dir),
        cache_phrases=tuple(phrase for phrases in ACKNOWLEDGEMENTS.values() for phrase in phrases),
    )
    await tts.warm()

    per_utterance: dict[str, dict[str, Any]] = {}
    stt_all, tts_all = Samples("stt"), Samples("tts")

    for label, text, speaker in UTTERANCES:
        clip = audio_for(text, speaker)
        language = Language.EN if speaker is not None else Language.RU
        stt_one, tts_one = Samples("stt"), Samples("tts")

        # One untimed pass: the first call on a fresh model pays for warm-up and
        # would otherwise be reported as the cost of the utterance.
        await stt.transcribe(clip)

        for _ in range(rounds):
            started = time.perf_counter()
            transcript = await stt.transcribe(clip)
            stt_one.add(time.perf_counter() - started)

            started = time.perf_counter()
            spoken = await tts.synthesise("Notepad is open, sir.", language=language)
            tts_one.add(time.perf_counter() - started)

        stt_all.values += stt_one.values
        tts_all.values += tts_one.values
        per_utterance[label] = {
            "text": text,
            "audio_seconds": round(len(clip) / SAMPLE_RATE, 2),
            "transcript": transcript.text,
            "language": transcript.language.value,
            "stt": stt_one.summary(),
            "tts": tts_one.summary(),
            "reply_audio_seconds": round(spoken.duration_s, 2),
        }

    return {
        "per_utterance": per_utterance,
        "stt_overall": stt_all.summary(),
        "tts_overall": tts_all.summary(),
    }


def fixed_costs() -> dict[str, Any]:
    """The parts that are decisions rather than measurements.

    Worth printing beside the measured ones: they are usually the largest term
    in the total, and unlike the models they can be changed by editing a number.
    """
    from atlas_voice.segmenter import SegmenterConfig

    config = SegmenterConfig()
    return {
        "end_of_speech_wait_ms": config.end_after_ms,
        "wake_detection_median_ms": 233,
        "note": (
            "The end-of-speech wait is how long silence must last before the "
            "utterance is considered finished. It is paid on every command and "
            "is pure latency; shortening it makes the assistant interrupt people "
            "who pause mid-sentence."
        ),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3, help="timed repeats per utterance")
    parser.add_argument("--listen-seconds", type=int, default=60)
    arguments = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    absent = MODELS.missing()
    if absent:
        print("voice models are missing; run scripts/fetch_voice_models.ps1")
        for item in absent:
            print(f"  - {item}")
        return 1

    print("measuring the always-on path ...")
    listening = measure_listening(arguments.listen_seconds)
    print(
        f"  {listening['audio_seconds']}s of audio in {listening['wall_seconds']}s "
        f"(realtime factor {listening['realtime_factor']}) — "
        f"{listening['duty_cycle_percent_of_one_core']}% of one core, sustained"
    )

    print("\nmeasuring recognition and speech ...")
    stages = await measure_stages(rounds=arguments.rounds)
    for label, row in stages["per_utterance"].items():
        print(
            f"  {label:10} {row['audio_seconds']:>5.2f}s audio  "
            f"stt {row['stt']['median']:>7.1f} ms  tts {row['tts']['median']:>6.1f} ms"
        )

    costs = fixed_costs()
    local = (
        costs["end_of_speech_wait_ms"]
        + stages["stt_overall"]["median"]
        + stages["tts_overall"]["median"]
    )
    record = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "listening": listening,
        "stages": stages,
        "fixed_costs": costs,
        "local_round_trip": {
            "unit": "ms",
            "median": round(local, 1),
            "note": (
                "Everything between the last word spoken and the first word "
                "answered, except the model. The model is a network call and is "
                "measured elsewhere; this is the part the machine owns."
            ),
        },
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "voice-latency.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten to {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
