"""Catch augmentation that has become a feature, before spending an hour on it.

This exists because of a specific failure. The v1 model scored 93.6% recall in
training and looked healthy; the acceptance test then found 14 985 false
activations per hour on plain street noise. The cause was that the noise corpus
used to make positives sound realistic appeared *only* inside positives, so
"this noise" became evidence for the wake word.

Nothing in the training loop could have noticed. Both the training and the
validation negatives came from a different corpus entirely, so the model was
never asked the question it was about to get wrong.

The check is a leave-one-category-out probe. A small classifier is trained on
positives against negatives **with one category deliberately withheld**, and
then shown that category. If a model that has never seen background audio scores
it as the wake word, background audio is out of distribution *and* correlated
with the positive label — which is exactly the v1 bug, detectable in seconds
rather than after a full run.

    uv run --group training python training/wakeword/preflight.py

Exits non-zero when the correlation is present. ``train.py`` runs it first, so
it cannot be skipped by forgetting.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import torch
from config import CLF_WINDOW, CONFIG, EMBEDDING_SIZE
from torch import nn

#: A held-out category scoring above this is treated as correlated with the
#: positive label. It is deliberately loose: the v1 model produced a median of
#: 0.94 here, and a healthy model produces something near zero. Anything in
#: between deserves a human looking at it.
MEDIAN_SCORE_LIMIT = 0.25
FIRING_FRACTION_LIMIT = 0.05


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
def scores(model: nn.Module, features: np.ndarray, *, limit: int = 8000) -> np.ndarray:
    sample = np.asarray(features[:limit], dtype=np.float32)
    return torch.sigmoid(model(torch.from_numpy(sample)).squeeze(-1)).numpy()


def load(name: str) -> np.ndarray | None:
    path = CONFIG.features_dir / name
    return np.load(path, mmap_mode="r") if path.is_file() else None


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    positives = load("positives.npy")
    if positives is None:
        print("no positives.npy — run features.py first", file=sys.stderr)
        return 2

    categories = {
        "hard_negatives": load("hard_negatives.npy"),
        "background": load("background.npy"),
    }
    present = {name: array for name, array in categories.items() if array is not None}
    if len(present) < 2:
        print(
            "preflight needs at least two negative categories to leave one out; "
            f"found {sorted(present)}",
            file=sys.stderr,
        )
        return 2

    findings: list[dict[str, object]] = []
    failed = False

    for held_out, array in present.items():
        others = [other for name, other in present.items() if name != held_out]
        negatives = np.concatenate([np.asarray(o[:12_000], dtype=np.float32) for o in others])

        model = probe(np.asarray(positives[:12_000], dtype=np.float32), negatives)
        held_scores = scores(model, array)

        median = float(np.median(held_scores))
        firing = float((held_scores > 0.5).mean())
        bad = median > MEDIAN_SCORE_LIMIT or firing > FIRING_FRACTION_LIMIT
        failed = failed or bad

        findings.append(
            {
                "held_out": held_out,
                "median_score": round(median, 4),
                "fraction_over_0.5": round(firing, 4),
                "verdict": "CORRELATED" if bad else "ok",
            }
        )
        print(
            f"  withheld {held_out:16} median={median:.4f}  "
            f"fires={firing * 100:5.1f}%  {'CORRELATED' if bad else 'ok'}"
        )

    CONFIG.metrics_dir.mkdir(parents=True, exist_ok=True)
    (CONFIG.metrics_dir / "preflight.json").write_text(
        json.dumps(
            {
                "limits": {
                    "median_score": MEDIAN_SCORE_LIMIT,
                    "fraction_over_0.5": FIRING_FRACTION_LIMIT,
                },
                "findings": findings,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    if failed:
        print(
            "\nA category the probe never saw scores as the wake word. That means "
            "it is out of distribution *and* correlated with the positive label — "
            "whatever was done to the positives was not also done to any negative. "
            "Training now would produce a model that fires on it.",
            file=sys.stderr,
        )
        return 1

    print("\npreflight ok: no withheld category reads as the wake word.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
