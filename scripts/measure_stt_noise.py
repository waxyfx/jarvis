"""How well the recogniser holds up as a room gets louder.

The M4 plan promised accuracy per signal-to-noise ratio and it was never
measured. This measures it: fixed phrases in both languages, mixed with noise at
several SNRs, scored as word error rate against what was actually said.

**Two kinds of noise, because they break recognition differently.** Pink noise
stands in for the steady things — a fan, an air conditioner, traffic through a
window — and mostly raises the floor. Babble is several people talking at once,
built by summing other synthetic voices, and it is far harder: it has the same
spectrum and the same rhythm as the thing being recognised, so a recogniser
cannot simply subtract it. A system tested only against white noise will look
much better than it is.

**What the numbers are not.** The speech is synthesised and the noise is
generated, so this measures the recogniser's robustness, not this room. Real
rooms add reverberation, which neither of these has, and a real microphone adds
its own colour. Treat the SNR at which accuracy falls off as the interesting
result, rather than any single figure.

    uv run python scripts/measure_stt_noise.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "atlas-voice" / "tests"))

from atlas_voice.audio import SAMPLE_RATE  # noqa: E402
from atlas_voice.engines.whisper import WhisperSettings, WhisperSTT  # noqa: E402
from voicefixtures import PIPER_MULTI, PIPER_RU, say  # noqa: E402

RESULTS = REPO / "docs" / "measurements"

#: Commands rather than prose: what matters is whether an instruction survives,
#: and instructions are short, which is the hard case for a recogniser.
#:
#: Each is written as what should be *spoken*, with the reference being what the
#: system should *produce*. The two differ on purpose for «хром»: the priming
#: vocabulary is supposed to turn it into "Chrome", so scoring against the
#: Cyrillic spelling would count the feature working as a failure. Doing that
#: cost this measurement its first Russian figure, which read 0.42 and was
#: mostly the alias table doing its job.
PHRASES: tuple[tuple[str, str, str, int | None], ...] = (
    ("en", "Open Notepad.", "Open Notepad.", 200),
    ("en", "Show me how much memory is left.", "Show me how much memory is left.", 200),
    (
        "en",
        "Close Chrome and open the second document.",
        "Close Chrome and open the second document.",
        333,
    ),
    ("en", "Remind me to call back before six.", "Remind me to call back before six.", 11),
    ("ru", "Открой блокнот.", "Открой Notepad.", None),
    ("ru", "Покажи, сколько осталось памяти.", "Покажи, сколько осталось памяти.", None),
    (
        "ru",
        "Закрой хром и открой второй документ.",
        "Закрой Chrome и открой второй документ.",
        None,
    ),
    ("ru", "Напомни перезвонить до шести.", "Напомни перезвонить до шести.", None),
)

#: Whisper writes numbers as digits and the references spell them out. That is a
#: difference in transcription convention, not a recognition error, and counting
#: it as one put a floor under every score that contained a number.
_NUMBERS = {
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "шести": "6",
    "шесть": "6",
    "семи": "7",
    "семь": "7",
}

#: Voices summed into babble. Deliberately not the ones being recognised.
BABBLE_VOICES = (470, 640, 830, 150, 90)

SNRS = (30, 20, 10, 5, 0)


def pink(length: int, seed: int = 7) -> np.ndarray:
    """Noise whose power falls with frequency, like most room noise does.

    White noise shaped by a 1/sqrt(f) filter in the frequency domain. Good
    enough for this purpose and it needs no dependency.
    """
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(length)
    spectrum = np.fft.rfft(white)
    frequencies = np.arange(len(spectrum))
    frequencies[0] = 1
    shaped = spectrum / np.sqrt(frequencies)
    out = np.fft.irfft(shaped, n=length).astype(np.float32)
    return out / (np.abs(out).max() + 1e-9)


def babble(length: int) -> np.ndarray:
    """Several people talking at once, from voices not under test."""
    mixed = np.zeros(length, dtype=np.float32)
    for index, speaker in enumerate(BABBLE_VOICES):
        clip = say(
            PIPER_MULTI,
            "The meeting has been moved to Thursday afternoon and the totals need checking.",
            speaker_id=speaker,
        )
        # Offset each voice so they overlap rather than start together, which
        # is what makes babble babble rather than a chorus.
        offset = int(index * 0.37 * SAMPLE_RATE)
        tiled = np.tile(clip, int(length / len(clip)) + 2)[: length + offset][offset:]
        mixed[: len(tiled)] += tiled[: len(mixed)]
    return mixed / (np.abs(mixed).max() + 1e-9)


def at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """Mix noise into speech at a given signal-to-noise ratio.

    Measured on power, over the whole clip. Scaled afterwards only if the sum
    would clip, because clipping is its own distortion and would be reported as
    if it were noise.
    """
    noise = noise[: len(speech)]
    speech_power = float(np.mean(speech**2)) + 1e-12
    noise_power = float(np.mean(noise**2)) + 1e-12
    wanted = speech_power / (10 ** (snr_db / 10))
    mixed = speech + noise * np.sqrt(wanted / noise_power)
    peak = float(np.abs(mixed).max())
    return (mixed / peak * 0.98 if peak > 0.98 else mixed).astype(np.float32)


def words(text: str) -> list[str]:
    return [_NUMBERS.get(word, word) for word in re.findall(r"\w+", text.lower())]


def error_rate(reference: str, produced: str) -> float:
    """Word error rate: edits needed to turn one into the other, over length.

    Plain Levenshtein over words. Above 1.0 is possible and meaningful — it
    means the recogniser invented more than it got right, which is exactly what
    Whisper does when it is given noise and nothing else.
    """
    want, got = words(reference), words(produced)
    if not want:
        return 0.0
    previous = list(range(len(got) + 1))
    for i, expected in enumerate(want, start=1):
        current = [i]
        for j, actual in enumerate(got, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1] / len(want)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snrs", type=int, nargs="*", default=list(SNRS))
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    stt = WhisperSTT(WhisperSettings())
    longest = max(
        len(say(PIPER_MULTI if s else PIPER_RU, t, speaker_id=s)) for _, t, _, s in PHRASES
    )
    noises = {"pink": pink(longest * 2), "babble": babble(longest * 2)}

    rows: list[dict[str, Any]] = []
    print(f"{'noise':8} {'snr':>5} {'lang':>5} {'wer':>7}  transcript")
    for noise_name, noise in noises.items():
        for snr in arguments.snrs:
            for language, text, expected, speaker in PHRASES:
                clip = say(
                    PIPER_MULTI if speaker is not None else PIPER_RU, text, speaker_id=speaker
                )
                transcript = await stt.transcribe(at_snr(clip, noise, snr))
                wer = error_rate(expected, transcript.text)
                rows.append(
                    {
                        "noise": noise_name,
                        "snr_db": snr,
                        "language": language,
                        "spoken": text,
                        "reference": expected,
                        "produced": transcript.text,
                        "detected_language": transcript.language.value,
                        "wer": round(wer, 3),
                    }
                )
                print(
                    f"{noise_name:8} {snr:>5} {language:>5} {wer:>7.2f}  {transcript.text[:52]!r}"
                )

    summary: dict[str, dict[str, float]] = {}
    for noise_name in noises:
        for snr in arguments.snrs:
            for language in ("en", "ru"):
                picked = [
                    row["wer"]
                    for row in rows
                    if row["noise"] == noise_name
                    and row["snr_db"] == snr
                    and row["language"] == language
                ]
                if picked:
                    summary.setdefault(noise_name, {})[f"snr{snr}_{language}"] = round(
                        float(np.mean(picked)), 3
                    )

    print("\nmean word error rate")
    print(f"{'noise':8} {'snr':>5} {'en':>7} {'ru':>7}")
    for noise_name in noises:
        for snr in arguments.snrs:
            english = summary[noise_name].get(f"snr{snr}_en")
            russian = summary[noise_name].get(f"snr{snr}_ru")
            print(f"{noise_name:8} {snr:>5} {english:>7.2f} {russian:>7.2f}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "stt-noise.json"
    path.write_text(
        json.dumps(
            {
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "model": "large-v3 int8_float16 cuda",
                "summary": summary,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
