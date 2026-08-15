# Training the "Atlas" wake word

A model you cannot rebuild is a liability. When the false-accept rate turns out
to be wrong in six months, the question will be *what was it trained on*, and
the answer has to be readable rather than remembered. That is what this
directory is for.

## What runs, in order

```bash
uv run --group training python training/wakeword/fetch.py
uv run --group training python training/wakeword/positives.py
uv run --group training python training/wakeword/features.py
uv run --group training python training/wakeword/train.py
uv run --group training python training/wakeword/evaluate.py
```

| Step | What it does | Roughly |
|---|---|---|
| `fetch.py` | Downloads voices, impulse responses, noise, and 16 GB of precomputed negative features | long, resumable |
| `positives.py` | Synthesises 24 000 wake words and 8 000 near-misses across 904 English speakers and 4 Russian ones | ~1 h |
| `features.py` | Puts each clip in a room and turns it into one `(16, 96)` classifier input | ~30 min |
| `train.py` | Trains the classifier, exports `.models/oww/atlas_v1.onnx` | ~20 min |
| `evaluate.py` | The acceptance test, writing `metrics/acceptance.json` | ~15 min |

Add `--scale 0.02` to `positives.py` and `--quick` to `evaluate.py` for a smoke
run that finishes in a couple of minutes and proves the wiring.

## Choices worth knowing

**Features are computed with the graphs the runtime uses.** Training against
different feature extraction than inference is the classic way to produce a
model that scores beautifully in a notebook and fails in the room. The
melspectrogram scaling (`x / 10 + 2`) is invisible in the ONNX signature and
silently destroys the model if omitted — measured, it moves the reference
detector from 0.998 on its phrase to 0.061.

**One clip is one training sample.** The window is sized so the feature stack
yields exactly sixteen embeddings — 31 840 samples in, 196 mel frames out — and
the word is placed so it *ends* near the window's end, because the detector must
fire as the word completes rather than whenever it is somewhere in earshot.

**English is multi-speaker; Russian is not.** Piper publishes a 904-speaker
English model and four single-speaker Russian ones. The Russian half of the
training set is therefore thinner in speaker identity and leans harder on
prosody and augmentation. This is a real weakness of the run, and the reason the
acceptance test reports «Атлас» separately from "Atlas" instead of averaging
them into one flattering number.

**Near-misses are generated deliberately.** Unrelated podcast audio never
teaches a model that "at last" is not "Atlas"; only "at last" does. The Russian
list matters more still, because «атлас» is an ordinary noun that a Russian
speaker says in ordinary conversation.

**Batches are balanced; the data is not.** Five million negative windows against
twenty-four thousand positives is the correct ratio for the world, and training
on it unweighted learns to answer "no" and stop.

## Why the acceptance test is separate

Training accuracy is not evidence. With balanced batches it sits near 99% for a
model that fires on every sibilant, and the number that decides whether the
assistant is pleasant to live with — how often it wakes when nobody called it —
cannot be read off it at all.

`evaluate.py` streams audio through the real runtime and reports a **curve**:
threshold against missed activations against false activations per hour. It does
not pick a winner. That choice is a judgement about how annoying a false wake is
against how annoying a missed one is.

**Everything it measures is synthesised speech, and that is a limitation, not a
footnote.** A synthesiser trained on read speech is not a person calling across
a room with their back turned. The final word belongs to recordings of the
owner's own voice.

## What is kept and what is thrown away

Kept: these scripts, `config.py`, and `metrics/`. They are small and they are
the record.

Thrown away by `cleanup.py`: the Hugging Face cache, the synthesised clips, the
intermediate feature arrays and the 16 GB negative set — about 25 GB in total,
all of it reproducible from the scripts above.

Never committed: model weights, datasets, and generated audio. `.models/` and
`.training/` are gitignored.
