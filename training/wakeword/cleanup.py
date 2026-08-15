"""Remove the training scratch, keeping what makes the run reproducible.

About 25 GB of downloads and intermediates exist only to produce a 1 MB ONNX
file. Once it exists they are dead weight, and all of it comes back from
``fetch.py`` and the scripts beside it.

What survives: the scripts, ``config.py`` and ``metrics/`` — the record of what
was trained and how it scored. And ``.models/oww/atlas_v1.onnx``, which is the
point of the exercise.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from config import CONFIG, WORK


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-features",
        action="store_true",
        help="keep the 16 GB negative set, for retraining without re-downloading",
    )
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    targets: list[Path] = [
        WORK / "hf-cache",
        CONFIG.clips_dir,
        CONFIG.features_dir,
        CONFIG.rir_dir,
        CONFIG.noise_dir,
    ]
    if not arguments.keep_features:
        targets += [CONFIG.negative_features, CONFIG.validation_features]

    freed = 0
    for target in targets:
        if not target.exists():
            continue
        bytes_here = size_of(target)
        freed += bytes_here
        print(
            f"    {'would remove' if arguments.dry_run else 'removing'} "
            f"{target.name}  {bytes_here / 1e9:.2f} GB"
        )
        if not arguments.dry_run:
            if target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target, ignore_errors=True)

    print(f"\n{'would free' if arguments.dry_run else 'freed'} {freed / 1e9:.2f} GB")

    if CONFIG.output_model.is_file():
        print(f"kept {CONFIG.output_model} ({CONFIG.output_model.stat().st_size / 1e6:.2f} MB)")
    print(f"kept {CONFIG.metrics_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
