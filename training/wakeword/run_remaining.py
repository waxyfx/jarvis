"""Wait for the corpus and the features, then train and evaluate.

The download and the feature extraction finish at different times and neither is
predictable, so this waits for both and chains the rest. It refuses to start
training on anything partial — the corpus file only takes its final name once
its byte count matches what the server declared, so waiting for that name *is*
the completeness check.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from config import CONFIG

HERE = Path(__file__).resolve().parent
POLL_SECONDS = 60


def ready() -> tuple[bool, str]:
    missing = []
    if not CONFIG.negative_features.is_file():
        partial = CONFIG.negative_features.with_suffix(CONFIG.negative_features.suffix + ".partial")
        have = partial.stat().st_size / 1e9 if partial.is_file() else 0.0
        missing.append(f"corpus {have:.2f}/16.09 GB")
    for name in ("positives.npy", "hard_negatives.npy"):
        if not (CONFIG.features_dir / name).is_file():
            missing.append(name)
    return not missing, ", ".join(missing)


def run(script: str, *arguments: str) -> int:
    print(f"\n{'=' * 70}\n== {script} {' '.join(arguments)}\n{'=' * 70}", flush=True)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(HERE / script), *arguments],
        cwd=str(HERE),
        check=False,
    )
    return completed.returncode


def main() -> int:
    waited = 0
    while True:
        done, missing = ready()
        if done:
            break
        if waited % 600 == 0:
            print(f"    waiting ({waited // 60} min): {missing}", flush=True)
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS

    print(f"\nall inputs present after {waited // 60} min", flush=True)

    for script in ("train.py", "evaluate.py"):
        code = run(script)
        if code != 0:
            print(f"\n{script} failed with {code}; stopping.", flush=True)
            return code

    print("\ntraining and acceptance complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
