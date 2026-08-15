"""Put the synthetic clips in a room, then turn them into classifier inputs.

A clip straight out of a synthesiser is anechoic, evenly loud and perfectly
centred. A microphone two metres away in a room with a fan hears none of those
things, and a model trained without that gap fires in headphones and goes deaf
on a desk.

Each clip becomes exactly **one** training sample. The window is sized so the
feature stack yields precisely sixteen embeddings — 31 840 samples in, 196 mel
frames out, sixteen 76-frame windows at stride eight — which is exactly what
the classifier reads. The word is placed so it *ends* near the end of that
window, because the detector has to fire as the word completes rather than
whenever it happens to be somewhere in earshot.

Features are computed with the same two ONNX graphs the runtime uses. Training
against different feature extraction than inference is the classic way to
produce a model that scores beautifully in a notebook and fails in the room.
"""

from __future__ import annotations

import argparse
import io
import json
import random
from pathlib import Path

import numpy as np
from config import CLF_WINDOW, CLIP_SAMPLES, CONFIG, EMBEDDING, EMBEDDING_SIZE, MELSPECTROGRAM
from tqdm import tqdm

from atlas_voice.audio import SAMPLE_RATE, read_wav, resample

_EMB_WINDOW = 76
_EMB_STRIDE = 8


class FeatureStack:
    """melspectrogram → embedding, batched."""

    def __init__(self) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self._mel = ort.InferenceSession(
            str(MELSPECTROGRAM), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._embed = ort.InferenceSession(
            str(EMBEDDING), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def window_for(self, audio: np.ndarray) -> np.ndarray:
        """The single ``(16, 96)`` sample this clip produces."""
        if len(audio) != CLIP_SAMPLES:
            raise ValueError(f"expected {CLIP_SAMPLES} samples, got {len(audio)}")

        mel = self._mel.run(None, {"input": audio.reshape(1, -1).astype(np.float32)})[0].squeeze()
        # The scaling the ONNX signature does not mention, and without which the
        # embedding model receives values it was never trained on.
        mel = mel / 10.0 + 2.0

        windows = np.stack(
            [
                mel[start : start + _EMB_WINDOW]
                for start in range(0, len(mel) - _EMB_WINDOW + 1, _EMB_STRIDE)
            ]
        )
        embeddings = self._embed.run(None, {"input_1": windows[..., None].astype(np.float32)})[0]
        return embeddings.reshape(-1, EMBEDDING_SIZE)[:CLF_WINDOW]


def load_impulse_responses(directory: Path, limit: int | None = None) -> list[np.ndarray]:
    """Measured room responses, read with soundfile rather than :func:`read_wav`.

    The MIT survey ships 24-bit WAVs. ``read_wav`` is deliberately strict — it
    guards test fixtures against silently wrong sample rates — so it rejects
    them, and the first version of this function caught that and moved on. The
    result was a training run with **zero** rooms that reported no error and
    produced a model with no reverberation in its experience at all.

    So: a decoder that handles the format, and a loud failure when nothing
    loads. Silence about missing augmentation is worse than a crash.
    """
    import soundfile as sf

    responses: list[np.ndarray] = []
    failures: list[str] = []
    paths = sorted(directory.glob("*.wav"))[:limit]

    for path in paths:
        try:
            impulse, rate = sf.read(path, dtype="float32")
        except Exception as error:
            failures.append(f"{path.name}: {error}")
            continue
        if impulse.ndim > 1:
            impulse = impulse.mean(axis=1)
        if rate != SAMPLE_RATE:
            impulse = resample(impulse.astype(np.float32), from_rate=rate, to_rate=SAMPLE_RATE)
        peak = float(np.abs(impulse).max())
        if peak > 0:
            responses.append((impulse / peak).astype(np.float32))

    if paths and not responses:
        raise RuntimeError(
            f"none of the {len(paths)} impulse responses in {directory} could be read; "
            f"first failures: {failures[:3]}"
        )
    if failures:
        print(f"    warning: skipped {len(failures)} unreadable impulse responses")
    return responses


def load_noise_pool(directory: Path, *, segments: int, rng: random.Random) -> list[np.ndarray]:
    """Two-second noise segments, decoded from the parquet shards."""
    import pyarrow.parquet as pq
    import soundfile as sf

    pool: list[np.ndarray] = []
    skipped = 0
    for shard in sorted(directory.glob("*.parquet")):
        table = pq.read_table(shard, columns=["audio"])
        rows = table.column("audio").to_pylist()
        rng.shuffle(rows)
        for row in rows:
            if len(pool) >= segments:
                if skipped:
                    print(f"    warning: skipped {skipped} undecodable noise clips")
                return pool
            try:
                audio, rate = sf.read(io.BytesIO(row["bytes"]), dtype="float32")
            except Exception:
                skipped += 1
                continue
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            if rate != SAMPLE_RATE:
                audio = resample(audio.astype(np.float32), from_rate=rate, to_rate=SAMPLE_RATE)
            if len(audio) < CLIP_SAMPLES:
                audio = np.tile(audio, int(np.ceil(CLIP_SAMPLES / max(len(audio), 1))))
            pool.append(audio[:CLIP_SAMPLES].astype(np.float32))

    if skipped:
        print(f"    warning: skipped {skipped} undecodable noise clips")
    if not pool:
        raise RuntimeError(f"no usable noise found in {directory}")
    return pool


def place(word: np.ndarray, *, rng: random.Random) -> np.ndarray:
    """Drop the word into a window, ending near — but not at — the end."""
    buffer = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    word = word[:CLIP_SAMPLES]
    low, high = CONFIG.augmentation.word_end_fraction
    end = int(CLIP_SAMPLES * rng.uniform(low, high))
    start = max(0, end - len(word))
    buffer[start : start + len(word)] = word[: CLIP_SAMPLES - start]
    return buffer


def reverberate(audio: np.ndarray, impulse: np.ndarray) -> np.ndarray:
    from scipy.signal import fftconvolve

    wet = fftconvolve(audio, impulse)[: len(audio)].astype(np.float32)
    peak = np.abs(wet).max()
    return (wet / peak * np.abs(audio).max()).astype(np.float32) if peak > 0 else audio


def mix_noise(audio: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    signal_power = float(np.mean(audio**2))
    noise_power = float(np.mean(noise**2))
    if signal_power <= 0 or noise_power <= 0:
        return audio
    scale = np.sqrt(signal_power / (noise_power * 10 ** (snr_db / 10)))
    return (audio + noise * scale).astype(np.float32)


def augment(
    word: np.ndarray,
    *,
    rng: random.Random,
    impulses: list[np.ndarray],
    noises: list[np.ndarray],
) -> np.ndarray:
    settings = CONFIG.augmentation
    audio = place(word, rng=rng)

    if impulses and rng.random() < settings.reverb_probability:
        audio = reverberate(audio, rng.choice(impulses))

    if noises and rng.random() < settings.noise_probability:
        audio = mix_noise(audio, rng.choice(noises), rng.uniform(*settings.snr_db))

    target = 10 ** (rng.uniform(*settings.gain_db) / 20)
    peak = np.abs(audio).max()
    if peak > 0:
        audio = (audio / peak * target).astype(np.float32)
    return np.clip(audio, -1.0, 1.0)


def background_window(
    *, rng: random.Random, impulses: list[np.ndarray], noises: list[np.ndarray]
) -> np.ndarray:
    """Audio made only of the things used to augment positives — no word.

    The whole point: whatever a positive is dressed in, a negative must be
    dressed in too. Without this the dressing becomes the signal.
    """
    settings = CONFIG.background
    roll = rng.random()

    if roll < settings.silence_fraction:
        # Not literally zeros: a real microphone in a quiet room still has a
        # noise floor, and a model trained on digital silence has not learned
        # anything about quiet rooms.
        audio = (
            np.random.default_rng(rng.randrange(2**31)).standard_normal(CLIP_SAMPLES) * 1e-4
        ).astype(np.float32)
        return audio

    if roll < settings.silence_fraction + settings.quiet_noise_fraction:
        level = rng.uniform(0.001, 0.02)
        base = rng.choice(noises) if noises else np.zeros(CLIP_SAMPLES, dtype=np.float32)
        audio = base[:CLIP_SAMPLES] * (level / max(float(np.abs(base).max()), 1e-9))
        return audio.astype(np.float32)

    audio = (rng.choice(noises) if noises else np.zeros(CLIP_SAMPLES, dtype=np.float32))[
        :CLIP_SAMPLES
    ].astype(np.float32)
    if impulses and rng.random() < 0.5:
        audio = reverberate(audio, rng.choice(impulses))

    target = 10 ** (rng.uniform(*CONFIG.augmentation.gain_db) / 20)
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = (audio / peak * target).astype(np.float32)
    return np.clip(audio, -1.0, 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noise-segments", type=int, default=1500)
    parser.add_argument(
        "--variants",
        type=int,
        default=2,
        help="augmented copies per clip; each is a different room and noise",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated outputs to rebuild: positives, hard, background",
    )
    arguments = parser.parse_args()
    wanted = {name.strip() for name in arguments.only.split(",") if name.strip()}
    unknown = wanted - {"positives", "hard", "background"}
    if unknown:
        parser.error(f"unknown output(s): {sorted(unknown)}")

    rng = random.Random(CONFIG.training.seed + 1)
    manifest = json.loads((CONFIG.clips_dir / "manifest.json").read_text(encoding="utf-8"))

    print("==> loading impulse responses")
    impulses = load_impulse_responses(CONFIG.rir_dir)
    print(f"    {len(impulses)} rooms")

    print("==> loading noise")
    noises = load_noise_pool(CONFIG.noise_dir, segments=arguments.noise_segments, rng=rng)
    print(f"    {len(noises)} noise segments")

    stack = FeatureStack()
    CONFIG.features_dir.mkdir(parents=True, exist_ok=True)

    for positive in (True, False):
        if wanted and ("positives" if positive else "hard") not in wanted:
            continue
        entries = [entry for entry in manifest if entry["positive"] is positive]
        collected = np.empty(
            (len(entries) * arguments.variants, CLF_WINDOW, EMBEDDING_SIZE), dtype=np.float32
        )
        written = 0
        label = "positives" if positive else "hard negatives"
        for entry in tqdm(entries, desc=label, unit="clip"):
            try:
                word = read_wav(CONFIG.clips_dir / str(entry["path"]))
            except (OSError, ValueError):
                continue
            for _ in range(arguments.variants):
                audio = augment(word, rng=rng, impulses=impulses, noises=noises)
                collected[written] = stack.window_for(audio)
                written += 1

        destination = CONFIG.features_dir / ("positives.npy" if positive else "hard_negatives.npy")
        np.save(destination, collected[:written])
        print(f"    {destination.name}: {written} samples")

    if wanted and "background" not in wanted:
        return 0

    count = CONFIG.background.count
    background = np.empty((count, CLF_WINDOW, EMBEDDING_SIZE), dtype=np.float32)
    for index in tqdm(range(count), desc="background", unit="window"):
        background[index] = stack.window_for(
            background_window(rng=rng, impulses=impulses, noises=noises)
        )
    np.save(CONFIG.features_dir / "background.npy", background)
    print(f"    background.npy: {count} samples")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
