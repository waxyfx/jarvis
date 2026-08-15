"""Download everything the training run needs.

Resumable, and safe to re-run: a 16 GB file over a domestic connection will be
interrupted, and a pipeline that restarts it from zero is a pipeline nobody
finishes.

Nothing here is committed. The datasets are large, they belong to other people,
and their licences travel with them:

    ACAV100M features   16.1 GB  precomputed negative embeddings, 2000 hours
    validation features  0.2 GB  held-out negatives from the same source
    MIT RIR              ~50 MB  270 measured room impulse responses
    UrbanSound8K shards  ~1.5 GB recorded noise for augmenting positives
    libritts_r           ~110 MB 904-speaker English synthesiser
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import CONFIG, MODELS, WORK

PIPER_REPO = "rhasspy/piper-voices"
FEATURES_REPO = "davidscripka/openwakeword_features"
RIR_REPO = "davidscripka/MIT_environmental_impulse_responses"
NOISE_REPO = "danavery/urbansound8K"

#: Four of sixteen shards. The noise is mixed into positives at random offsets,
#: so variety matters more than volume, and a quarter of the corpus already
#: covers every class in it.
NOISE_SHARDS = 4


def fetch(repo: str, filename: str, destination: Path, *, repo_type: str = "model") -> Path:
    from huggingface_hub import hf_hub_download

    if destination.is_file():
        print(f"    {destination.name} already present")
        return destination

    print(f"    fetching {filename} ...", flush=True)
    downloaded = hf_hub_download(
        repo_id=repo,
        filename=filename,
        repo_type=repo_type,
        cache_dir=str(WORK / "hf-cache"),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Copy rather than move: the cache is what makes a re-run cheap, and the
    # cache directory is removed explicitly at the end of the run instead.
    destination.write_bytes(Path(downloaded).read_bytes())
    print(f"    {destination.name}  {destination.stat().st_size / 1e9:.2f} GB")
    return destination


def fetch_voices() -> None:
    print("==> Piper voices")
    name = CONFIG.voices.english_multispeaker
    for extension in (".onnx", ".onnx.json"):
        fetch(
            PIPER_REPO,
            f"en/en_US/libritts_r/medium/{name}{extension}",
            CONFIG.piper_dir / f"{name}{extension}",
        )
    for voice in CONFIG.voices.russian:
        for extension in (".onnx", ".onnx.json"):
            speaker = voice.split("-")[1]
            fetch(
                PIPER_REPO,
                f"ru/ru_RU/{speaker}/medium/{voice}{extension}",
                CONFIG.piper_dir / f"{voice}{extension}",
            )


def fetch_impulse_responses() -> None:
    print("==> Room impulse responses")
    from huggingface_hub import list_repo_files

    CONFIG.rir_dir.mkdir(parents=True, exist_ok=True)
    files = [
        name
        for name in list_repo_files(RIR_REPO, repo_type="dataset")
        if name.startswith("16khz/") and name.endswith(".wav")
    ]
    print(f"    {len(files)} impulse responses")
    for name in files:
        fetch(RIR_REPO, name, CONFIG.rir_dir / Path(name).name, repo_type="dataset")


def fetch_noise() -> None:
    print("==> Recorded noise")
    from huggingface_hub import list_repo_files

    CONFIG.noise_dir.mkdir(parents=True, exist_ok=True)
    shards = sorted(
        name
        for name in list_repo_files(NOISE_REPO, repo_type="dataset")
        if name.endswith(".parquet")
    )[:NOISE_SHARDS]
    for shard in shards:
        fetch(NOISE_REPO, shard, CONFIG.noise_dir / Path(shard).name, repo_type="dataset")


def resumable_download(url: str, destination: Path, *, attempts: int = 40) -> Path:
    """Download with byte-range resume, retrying past stalls.

    ``hf_hub_download`` was used here first and could not finish. Over a
    domestic connection the 16 GB transfer stalls, and on restart it does not
    continue: it opens a *new* temporary file with a fresh random suffix and
    begins again from zero, orphaning 7.4 GB of perfectly good prefix. A
    pipeline whose longest step silently restarts itself never finishes.

    This keeps one ``.partial`` file, asks for ``Range: bytes=N-`` on every
    attempt, and only renames into place once the byte count matches what the
    server declared. Interrupt it as often as you like.
    """
    import httpx

    if destination.is_file():
        print(f"    {destination.name} already present")
        return destination

    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        head = client.head(url)
        head.raise_for_status()
        total = int(head.headers["content-length"])
    print(f"    {destination.name}: {total / 1e9:.2f} GB")

    for attempt in range(1, attempts + 1):
        have = partial.stat().st_size if partial.is_file() else 0
        if have >= total:
            break

        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.Client(follow_redirects=True, timeout=120.0) as client:
                with client.stream("GET", url, headers=headers) as response:
                    if have and response.status_code != 206:
                        raise RuntimeError(
                            f"server ignored the range request (HTTP {response.status_code}); "
                            "refusing to append to a partial file"
                        )
                    response.raise_for_status()
                    with partial.open("ab") as handle:
                        for block in response.iter_bytes(chunk_size=4 * 1024 * 1024):
                            handle.write(block)
        except Exception as error:
            got = partial.stat().st_size if partial.is_file() else 0
            print(
                f"    attempt {attempt}: {type(error).__name__} at "
                f"{got / 1e9:.2f}/{total / 1e9:.2f} GB, resuming",
                flush=True,
            )
            continue

        got = partial.stat().st_size
        print(f"    {got / 1e9:.2f}/{total / 1e9:.2f} GB", flush=True)
        if got >= total:
            break

    final = partial.stat().st_size if partial.is_file() else 0
    if final != total:
        raise RuntimeError(
            f"{destination.name}: got {final} of {total} bytes after {attempts} attempts. "
            "Nothing downstream may run on a truncated corpus."
        )
    partial.replace(destination)
    return destination


def fetch_negative_features(*, skip_large: bool) -> None:
    print("==> Negative features")
    fetch(
        FEATURES_REPO,
        "validation_set_features.npy",
        CONFIG.validation_features,
        repo_type="dataset",
    )
    if skip_large:
        print("    skipping the 16 GB training features (--skip-large)")
        return
    resumable_download(
        f"https://huggingface.co/datasets/{FEATURES_REPO}/resolve/main/"
        "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
        CONFIG.negative_features,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-large",
        action="store_true",
        help="everything except the 16 GB feature file, for a quick smoke run",
    )
    arguments = parser.parse_args()

    if not (MODELS / "oww" / "melspectrogram.onnx").is_file():
        print("The openWakeWord feature stack is missing.", file=sys.stderr)
        print("Run scripts/fetch_voice_models.ps1 first.", file=sys.stderr)
        return 1

    WORK.mkdir(parents=True, exist_ok=True)
    fetch_voices()
    fetch_impulse_responses()
    fetch_noise()
    fetch_negative_features(skip_large=arguments.skip_large)
    print("\nReady.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
