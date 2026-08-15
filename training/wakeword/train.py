"""Train the "Atlas" classifier and export it as ONNX.

The model is small on purpose. It reads sixteen 96-value embeddings that the
shared feature stack has already done the hard work of producing, so the
classifier's whole job is to draw a boundary in that space. Anything larger
would overfit twenty-four thousand synthetic clips and cost CPU in the one
component that runs every eighty milliseconds, forever.

Two choices deserve stating.

**Batches are balanced, the data is not.** Five million negative windows against
twenty-four thousand positives is the correct ratio for the world — most sound
is not the wake word — but unweighted training on it learns to answer "no" and
stops. Positives are oversampled into every batch instead, so both classes
produce gradient in every step.

**Hard negatives are oversampled too.** "At last" and «атласный» are a rounding
error by count and most of the risk by consequence. Unrelated podcast audio
never teaches a model that they are not the wake word; only they do.

Selection is not by validation accuracy. With balanced batches accuracy is
close to meaningless — it would sit at 99% for a model that fires on every
sibilant — so the checkpoint is chosen by *recall at a fixed false-accept rate*
measured on held-out negatives, which is the quantity that decides whether the
thing is usable.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch
from config import CLF_WINDOW, CONFIG, EMBEDDING_SIZE
from torch import nn

INPUT_SIZE = CLF_WINDOW * EMBEDDING_SIZE


class WakeWordClassifier(nn.Module):
    """Sixteen embeddings in, one probability out."""

    def __init__(self, hidden: int = 128) -> None:
        super().__init__()
        self.stack = nn.Sequential(
            nn.Flatten(),
            nn.Linear(INPUT_SIZE, hidden),
            nn.LayerNorm(hidden),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(hidden, hidden),
            nn.LayerNorm(hidden),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(hidden, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.stack(x)


class Exported(nn.Module):
    """The shape the runtime expects: probability, not logit."""

    def __init__(self, model: WakeWordClassifier) -> None:
        super().__init__()
        self.model = model

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.model(x))


class NegativePool:
    """Classifier inputs drawn from a published feature file, whatever its shape.

    The two files openWakeWord publishes are laid out differently and nothing
    announces it. The 17 GB training corpus is already cut into windows —
    ``(5 625 000, 16, 96)`` of float16 — while the validation set is a *flat*
    ``(481 345, 96)`` sequence of consecutive embeddings, as a detector produces
    while listening. Assuming either layout for both breaks on the other, so
    this reads the shape and adapts.

    Both are memory-mapped. Seventeen gigabytes does not fit in fifteen of RAM
    alongside anything else, and it does not need to: training reads scattered
    rows, which is exactly what a memory map is for.
    """

    def __init__(self, path: Path, *, limit: int | None = None) -> None:
        array = np.load(path, mmap_mode="r")
        if array.ndim == 3 and array.shape[1:] == (CLF_WINDOW, EMBEDDING_SIZE):
            self.windowed = True
        elif array.ndim == 2 and array.shape[1] == EMBEDDING_SIZE:
            self.windowed = False
        else:
            raise ValueError(
                f"{path.name}: expected ({CLF_WINDOW}, {EMBEDDING_SIZE}) windows or a flat "
                f"(n, {EMBEDDING_SIZE}) sequence, got {array.shape}"
            )
        self.array = array[:limit] if limit else array
        self.name = path.name

    def __len__(self) -> int:
        """How many distinct classifier inputs this file can yield."""
        return len(self.array) if self.windowed else max(0, len(self.array) - CLF_WINDOW)

    @property
    def hours(self) -> float:
        """Roughly how much audio this represents, at 12.5 windows a second."""
        steps = len(self.array) if not self.windowed else len(self.array) * CLF_WINDOW
        return steps / (16_000 / 1280) / 3600

    def take(self, starts: np.ndarray) -> np.ndarray:
        """``(n, 16, 96)`` float32, whichever way the file is stored."""
        if self.windowed:
            return np.asarray(self.array[starts], dtype=np.float32)
        return np.stack(
            [np.asarray(self.array[s : s + CLF_WINDOW], dtype=np.float32) for s in starts]
        )

    def sample(self, rng: np.random.Generator, count: int) -> np.ndarray:
        # Sorted indices turn scattered access into something closer to a
        # sequential read of the memory map.
        return self.take(np.sort(rng.integers(0, len(self), count)))

    def evenly_spaced(self, *, count: int) -> np.ndarray:
        """A fixed, representative slice, for comparable epoch-to-epoch scores."""
        return self.take(np.linspace(0, len(self) - 1, min(count, len(self))).astype(np.int64))

    def describe(self) -> str:
        layout = "pre-cut windows" if self.windowed else "flat sequence"
        return f"{self.array.shape} {self.array.dtype} ({layout}) ≈ {self.hours:,.0f} h"


def false_accept_rate(scores: np.ndarray, threshold: float) -> float:
    return float((scores >= threshold).mean())


def threshold_for_rate(negative_scores: np.ndarray, target: float) -> float:
    """The threshold at which negatives fire at ``target`` rate."""
    return float(np.quantile(negative_scores, 1.0 - target))


@torch.no_grad()
def score_all(model: nn.Module, features: np.ndarray, *, batch: int = 8192) -> np.ndarray:
    model.eval()
    out = np.empty(len(features), dtype=np.float32)
    for start in range(0, len(features), batch):
        chunk = np.asarray(features[start : start + batch], dtype=np.float32)
        logits = model(torch.from_numpy(chunk))
        out[start : start + len(chunk)] = torch.sigmoid(logits).squeeze(-1).numpy()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=CONFIG.training.epochs)
    parser.add_argument("--steps-per-epoch", type=int, default=600)
    parser.add_argument("--negative-limit", type=int, default=None)
    arguments = parser.parse_args()

    settings = CONFIG.training
    torch.manual_seed(settings.seed)
    rng = np.random.default_rng(settings.seed)

    print("==> loading features")
    # A truncated corpus must never be trained on quietly. `fetch.py` only
    # renames its `.partial` into place once the byte count matches what the
    # server declared, so the file existing is the guarantee — but a leftover
    # `.partial` beside it means a transfer is unfinished, and saying so beats
    # producing a model nobody can account for.
    unfinished = CONFIG.negative_features.with_suffix(CONFIG.negative_features.suffix + ".partial")
    if not CONFIG.negative_features.is_file():
        held = unfinished.stat().st_size / 1e9 if unfinished.is_file() else 0.0
        print(
            f"The negative corpus is not complete ({held:.2f} GB downloaded).\n"
            "Run fetch.py until it finishes. Training on a partial corpus would "
            "change what the model learned without changing what it is called.",
        )
        return 1
    if arguments.negative_limit:
        print(
            f"    !! --negative-limit {arguments.negative_limit:,} — this is NOT the "
            "full corpus, and the resulting metrics are not comparable to a full run"
        )

    positives = np.load(CONFIG.features_dir / "positives.npy")
    hard = np.load(CONFIG.features_dir / "hard_negatives.npy")
    negatives = NegativePool(CONFIG.negative_features, limit=arguments.negative_limit)
    validation = NegativePool(CONFIG.validation_features)
    print(f"    positives      {positives.shape}")
    print(f"    hard negatives {hard.shape}")
    print(f"    negatives      {negatives.describe()}")
    print(f"    validation     {validation.describe()}")

    # Hold out a fifth of the positives. Synthetic clips from the same speaker
    # are near-duplicates, so a random split flatters; splitting by index at
    # least keeps whole generation batches together.
    split = int(len(positives) * 0.8)
    train_positives, held_positives = positives[:split], positives[split:]
    hard_split = int(len(hard) * 0.8)
    train_hard, held_hard = hard[:hard_split], hard[hard_split:]

    model = WakeWordClassifier()
    optimiser = torch.optim.AdamW(
        model.parameters(), lr=settings.learning_rate, weight_decay=settings.weight_decay
    )
    loss_fn = nn.BCEWithLogitsLoss()

    positives_per_batch = int(settings.batch_size * settings.positive_fraction)
    hard_per_batch = int(settings.batch_size * 0.15)
    easy_per_batch = settings.batch_size - positives_per_batch - hard_per_batch

    history: list[dict[str, float]] = []
    best_recall = -1.0
    best_state: dict[str, torch.Tensor] | None = None
    started = time.perf_counter()

    for epoch in range(1, arguments.epochs + 1):
        model.train()
        running = 0.0
        for _ in range(arguments.steps_per_epoch):
            pos = train_positives[rng.integers(0, len(train_positives), positives_per_batch)]
            hrd = train_hard[rng.integers(0, len(train_hard), hard_per_batch)]
            # Sorting the scattered indices turns random access into something
            # closer to a sequential read of the memory map.
            easy = negatives.sample(rng, easy_per_batch)

            features = np.concatenate([pos, hrd, easy]).astype(np.float32)
            labels = np.concatenate([np.ones(len(pos)), np.zeros(len(hrd) + len(easy))]).astype(
                np.float32
            )

            optimiser.zero_grad()
            logits = model(torch.from_numpy(features)).squeeze(-1)
            loss = loss_fn(logits, torch.from_numpy(labels))
            loss.backward()
            optimiser.step()
            running += float(loss)

        held_scores = score_all(model, held_positives)
        hard_scores = score_all(model, held_hard)
        # A fixed, evenly spaced slice: the same windows every epoch, so the
        # threshold and recall figures below are comparable across epochs
        # rather than drifting with the sample.
        negative_scores = score_all(model, validation.evenly_spaced(count=120_000))

        # One false accept per hour, at roughly 12.5 classifier windows a
        # second, is one in 45 000 windows.
        threshold = threshold_for_rate(negative_scores, 1 / 45_000)
        recall = float((held_scores >= threshold).mean())
        hard_fa = false_accept_rate(hard_scores, threshold)

        record = {
            "epoch": epoch,
            "loss": running / arguments.steps_per_epoch,
            "threshold_at_1_fa_per_hour": threshold,
            "recall_at_that_threshold": recall,
            "hard_negative_false_accepts": hard_fa,
        }
        history.append(record)
        print(
            f"    epoch {epoch:2}  loss {record['loss']:.4f}  "
            f"thr {threshold:.4f}  recall {recall:.3f}  hard-FA {hard_fa:.4f}"
        )

        # Recall at a fixed false-accept rate, with near-misses breaking ties.
        score = recall - hard_fa
        if score > best_recall:
            best_recall = score
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}

    assert best_state is not None
    model.load_state_dict(best_state)
    elapsed = time.perf_counter() - started

    CONFIG.output_model.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        Exported(model).eval(),
        torch.zeros(1, CLF_WINDOW, EMBEDDING_SIZE),
        str(CONFIG.output_model),
        input_names=["x"],
        output_names=["score"],
        # The TorchScript exporter, chosen explicitly rather than by default.
        # It warns that it is legacy, and it is; it also produces exactly the
        # `[1, 16, 96] → [1, 1]` graph the runtime expects, which was verified
        # by loading the export back through OpenWakeWord before this run. When
        # it is finally removed, re-verify the dynamo export the same way rather
        # than assuming the shapes survived.
        dynamo=False,
    )
    print(f"\nwrote {CONFIG.output_model}")

    CONFIG.metrics_dir.mkdir(parents=True, exist_ok=True)
    (CONFIG.metrics_dir / "training.json").write_text(
        json.dumps(
            {
                "config": {
                    "training": asdict(CONFIG.training),
                    "synthesis": asdict(CONFIG.synthesis),
                    "augmentation": asdict(CONFIG.augmentation),
                },
                "shapes": {
                    "positives": list(positives.shape),
                    "hard_negatives": list(hard.shape),
                    "negatives": negatives.describe(),
                    "negative_hours": round(negatives.hours, 1),
                },
                "elapsed_seconds": round(elapsed, 1),
                "history": history,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
