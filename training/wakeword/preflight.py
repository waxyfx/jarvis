"""Catch augmentation that has become a feature, before spending hours on it.

This exists because of a specific failure. The Atlas model scored 93.6% recall
in training and looked healthy; the acceptance test then found 14 985 false
activations per hour on plain street noise. The cause was that the noise corpus
used to make positives sound realistic appeared *only* inside positives, so
"this noise" became evidence for the wake word. Nothing in the training loop
could have noticed, because both the training and validation negatives came
from a different corpus entirely.

Two checks, in order.

**Coverage.** Every augmentation applied to a positive must also appear in audio
labelled negative. This is the rule Atlas broke, and the version of it that
matters is blunt: the ``background`` category — the noise and the rooms, with no
word in them — must exist and be substantial. Atlas had no such category at all,
so this check alone would have stopped it.

**A dry run of the real mix.** A small probe is trained on positives against the
negatives *as training will actually combine them*, then shown held-out slices
of each negative category. Every category must score low. This catches the case
coverage cannot: a background set that exists but is too small or too narrow to
teach anything.

    uv run --group training python training/wakeword/preflight.py

Exits non-zero when either check fails. ``train.py`` runs it first, so it cannot
be skipped by forgetting.

### Why this is not leave-one-out

The first version withheld one negative category, trained on the rest, and
scored the withheld one. It failed here, and it was wrong to. Withholding
*every speech negative* leaves a probe that has only ever been taught "wake word
versus noise" — so it calls all speech the wake word, and reports 71% firing on
near-misses that the real mix scores at 6%. That is an artefact of the
withholding, not a property of the data: measured, a probe trained on
background alone scores held-out speech at median 0.99, while the same data
under the real mix scores it at 0.007.

Categories are not substitutes for one another. The question worth asking is not
"could we do without this category" — we could not, which is why it is there —
but "given everything we have, does any category still read as the wake word".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
from config import CLF_WINDOW, CONFIG, EMBEDDING_SIZE
from torch import nn

#: A negative category whose median score passes this, or which fires this
#: often, is being read as the wake word. Atlas's background sat at a median of
#: 0.94 and fired on essentially every window, so these limits catch it by a
#: wide margin without flagging the ordinary imprecision of a 400-step probe.
MEDIAN_SCORE_LIMIT = 0.20
FIRING_FRACTION_LIMIT = 0.20

#: Categories that must exist at all. `background` is the one Atlas lacked.
REQUIRED_CATEGORIES = ("hard_negatives", "background")
#: Below this a category is present in name only and cannot teach anything.
MINIMUM_SAMPLES = 2_000


def probe(
    positives: np.ndarray,
    negatives: np.ndarray,
    *,
    steps: int = 400,
    batch: int = 512,
    seed: int = 0,
) -> nn.Module:
    """A deliberately small, quickly trained stand-in for the real classifier."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    model = nn.Sequential(
        nn.Flatten(),
        nn.Linear(CLF_WINDOW * EMBEDDING_SIZE, 64),
        nn.ReLU(),
        nn.Linear(64, 1),
    )
    optimiser = torch.optim.AdamW(model.parameters(), lr=2e-3)
    loss_fn = nn.BCEWithLogitsLoss()

    for _ in range(steps):
        pos = positives[rng.integers(0, len(positives), batch // 2)]
        neg = negatives[rng.integers(0, len(negatives), batch // 2)]
        features = np.concatenate([pos, neg]).astype(np.float32)
        labels = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))]).astype(np.float32)

        optimiser.zero_grad()
        loss = loss_fn(model(torch.from_numpy(features)).squeeze(-1), torch.from_numpy(labels))
        loss.backward()
        optimiser.step()

    return model.eval()


@torch.no_grad()
def scores(model: nn.Module, features: np.ndarray) -> np.ndarray:
    sample = np.array(features, dtype=np.float32)
    return torch.sigmoid(model(torch.from_numpy(sample)).squeeze(-1)).numpy()


def load(name: str, limit: int) -> np.ndarray | None:
    path = CONFIG.features_dir / name
    if not path.is_file():
        return None
    return np.array(np.load(path, mmap_mode="r")[:limit], dtype=np.float32)


def check(
    *, skip_corpus: bool = False, exclude: tuple[str, ...] = ()
) -> tuple[bool, list[dict[str, object]]]:
    """Run both checks. ``exclude`` drops a category, for testing this test."""
    findings: list[dict[str, object]] = []
    positives = load("positives.npy", 16_000)
    if positives is None:
        raise SystemExit("no positives.npy — run features.py first")

    # ---- coverage -------------------------------------------------------
    categories: dict[str, np.ndarray] = {}
    failed = False
    for name in REQUIRED_CATEGORIES:
        if name in exclude:
            data = None
        else:
            data = load(f"{name}.npy", 16_000)
        enough = data is not None and len(data) >= MINIMUM_SAMPLES
        findings.append(
            {
                "check": "coverage",
                "category": name,
                "samples": 0 if data is None else len(data),
                "verdict": "ok" if enough else "MISSING",
            }
        )
        print(
            f"  coverage  {name:16} "
            f"{'—' if data is None else len(data):>8}  {'ok' if enough else 'MISSING'}"
        )
        if not enough:
            failed = True
        elif data is not None:
            categories[name] = data

    if failed:
        return False, findings

    # ---- dry run of the real mix ----------------------------------------
    parts = list(categories.values())
    if not skip_corpus and CONFIG.negative_features.is_file():
        from train import NegativePool

        pool = NegativePool(CONFIG.negative_features, limit=400_000)
        parts.append(pool.sample(np.random.default_rng(0), 12_000))

    held = {name: data[-4_000:] for name, data in categories.items()}
    trained_on = np.concatenate([part[:-4_000] for part in parts if len(part) > 4_000])
    model = probe(positives[:-2_000], trained_on)

    for name, data in held.items():
        result = scores(model, data)
        median = float(np.median(result))
        firing = float((result > 0.5).mean())
        bad = median > MEDIAN_SCORE_LIMIT or firing > FIRING_FRACTION_LIMIT
        failed = failed or bad
        findings.append(
            {
                "check": "dry_run",
                "category": name,
                "median_score": round(median, 4),
                "fraction_over_0.5": round(firing, 4),
                "verdict": "READS AS WAKE WORD" if bad else "ok",
            }
        )
        print(
            f"  dry run   {name:16} median={median:.4f}  fires={firing * 100:5.1f}%  "
            f"{'READS AS WAKE WORD' if bad else 'ok'}"
        )

    return not failed, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exclude",
        default="",
        help="drop a negative category, to confirm this check still fails without it",
    )
    parser.add_argument("--skip-corpus", action="store_true", help="ignore the 17 GB negatives")
    arguments = parser.parse_args()
    exclude = tuple(name.strip() for name in arguments.exclude.split(",") if name.strip())

    passed, findings = check(skip_corpus=arguments.skip_corpus, exclude=exclude)

    CONFIG.metrics_dir.mkdir(parents=True, exist_ok=True)
    Path(CONFIG.metrics_dir / "preflight.json").write_text(
        json.dumps(
            {
                "limits": {
                    "median_score": MEDIAN_SCORE_LIMIT,
                    "fraction_over_0.5": FIRING_FRACTION_LIMIT,
                    "minimum_samples": MINIMUM_SAMPLES,
                },
                "excluded_for_this_run": list(exclude),
                "findings": findings,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    if not passed:
        print(
            "\nA negative category is missing, or still reads as the wake word. Whatever "
            "was done to the positives was not also done to anything labelled negative, "
            "so training now would produce a model that fires on it — which is exactly "
            "what happened to the Atlas run.",
            file=sys.stderr,
        )
        return 1

    print("\npreflight ok: every negative category is present and reads as a negative.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
