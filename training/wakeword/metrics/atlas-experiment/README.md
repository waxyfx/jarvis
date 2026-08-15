# The "Atlas" wake word — experiment, not production

The assistant is called JARVIS. This directory is the earlier run against the
wake word "Atlas" / «Атлас», kept because it is the measurement that produced
two findings worth remembering, and because the before/after comparison is only
meaningful if the "before" survives.

**This model is not used anywhere.** `.models/oww/atlas_v1.onnx` is a build
artefact of this run; it is gitignored and should be deleted rather than
deployed.

## What it measured

Recall 1.000 on every positive group — "Atlas" across 25 English speakers,
«Атлас» across 4 Russian voices, quiet through loud, near through far, down to
5 dB SNR. English near-misses rejected outright: "at last", "Atlanta",
"Atlassian" all peaked below 0.001. Latency 0.361 s median. Listening cost 2.0%
of one core and 32.7 MB.

And **14 985 false activations per hour** on ordinary street noise, which made
it unusable.

## The two findings

**1. Augmentation that appears only in positives becomes a feature.** The noise
corpus mixed into 80% of positives appeared nowhere in the five million
published negatives, so the model learned that the noise *was* the wake word.
Digital silence scored 0.0001 and unseen white noise never fired — only the
augmentation corpus did. `preflight.py` now catches this shape of error in
seconds, before a full run, by withholding a negative category and checking
whether a probe that never saw it scores it as the wake word.

**2. A wake word that is also a common noun cannot be fixed by training.**
«Атлас мира лежал на столе» and «Географический атлас» both fired at 1.00.
They contain the wake word; acoustically there is nothing to separate the
assistant's name from a book of maps. This is a naming decision, not a modelling
one — and it is one of the reasons the name changed.

«Джарвис» is not a Russian word, so this particular collision does not follow us.
