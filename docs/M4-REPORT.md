# M4 — Voice Engine: report

What was built, what was measured, and what is still wrong with it.

The plan is in [M4-PLAN.md](M4-PLAN.md). This is the answer to it. Where a
number appears here it was measured on this machine and the measurement is
reproducible; where something was not measured, it says so rather than
implying otherwise.

---

## 1. What works

A sentence spoken at the microphone reaches Gemini, comes back through the
Policy Engine as a signed command, opens the program, and is answered aloud:

```
microphone → wake word → VAD → segmenter → speaker check → Whisper
           → backend → Gemini → Policy Engine → agent → action
           → Piper → speakers
```

`e2e/test_voice_e2e.py` drives that whole path — thirteen scenarios, all
passing — with the real detector, the real VAD, the real Whisper, a real backend
on a real socket, real policy, real signatures and a real agent. Two things are stood in
for, both because a test cannot contain a person: the speaker is Piper, and the
model is scripted. Neither substitution is hidden and each is argued for in the
file's own docstring.

Started with:

```
jarvis.bat
```

Voice runs **inside** the agent process rather than beside it. Two processes
would be two connections for one device identity, and the backend displaces the
older one, so whichever started first would quietly stop working.

---

## 2. The security boundary did not move

This is the part worth reading twice, because a voice interface is exactly the
kind of feature that erodes a boundary by accident.

| | |
|---|---|
| What voice produces | **Text**, handed to the same `/v1/assistant/message` endpoint typed input uses |
| Privileges it gains | **None.** It is the same endpoint, the same policy, the same signatures |
| Can it confirm a held action? | **No.** A MEDIUM action is reported aloud as waiting and stays waiting |
| Speaker verification decides | *Whose speech is listened to.* Never what may be done |
| Where the profile lives | `%LOCALAPPDATA%\ATLAS\voice_profile.bin`, DPAPI-encrypted, never sent anywhere |
| What crosses the network | The transcript. The same sentence the person could have typed |

Saying "yes" to a microphone is not the confirmation step and M4 did not make it
one. Confirmation stays where it was.

Mute is the agent's own switch: checked before any audio is processed, and
nothing arriving over the network can lift it. Same rule as SAFE MODE, for the
same reason.

---

## 3. Wake word

Decided by measurement against 592 recorded clips, not by preference.

| | sherpa-onnx (chosen) | openWakeWord (trained, rejected) |
|---|---|---|
| False activations / hour | **3.08** | 780.75 |
| On background noise (120 clips) | **0.0 /h** | — |
| On Russian near-misses (180 clips) | **0.0 /h** | — |
| On English near-misses (195 clips) | 10.13 /h | — |
| Recall, English "Jarvis" | 70.8 % | — |
| Median time to fire | **0.233 s** (p90 0.393, max 0.5) | — |

Configuration: the zh-en phoneme model, keywords `HEY JARVIS` and `JARVIS`,
`keywords_threshold` 0.25. Changing any of those invalidates the acceptance,
which is why they live in one place with that sentence written next to them.

**Recall is 70.8 % and that is the honest headline.** Roughly three in ten
attempts do not wake it, and the misses are not random: whether it fires depends
on what follows the word as much as on who says it. The same synthetic voice
that reliably wakes on "Jarvis, open Notepad" does not wake on "Jarvis, what is
the time?". Two openWakeWord training cycles were spent trying to beat this and
both were worse; a third was ruled out in advance.

Russian «Джарвис» sits at about 21 % and is **not** a wake word. That was
measured, reported and accepted — English "Jarvis" is the only one.

Free and local throughout. Picovoice/Porcupine was rejected on the grounds that
it is a paid service requiring an AccessKey.

---

## 4. Latency and cost

Measured by `scripts/measure_voice_latency.py`; the record is in
[measurements/voice-latency.json](measurements/voice-latency.json).

### While listening — the part that runs every waking second

| | |
|---|---|
| 60 s of audio processed in | **1.82 s** |
| Realtime factor | **0.030** |
| Sustained cost | **3 % of one core** |
| Memory the models add | ~51 MB |

The instantaneous CPU reading during those 1.8 seconds is 98 %, and quoting that
would be dishonest: the models saturate a core and then stop. What matters is
that they run for under two seconds out of every sixty.

### After the last word — the wait before an answer

| Stage | Median | Note |
|---|---|---|
| End-of-speech wait | **700 ms** | A configured number, not a model cost |
| Whisper large-v3 | **850 ms** | 683 ms short → 960 ms for a 3.9 s utterance |
| Piper | **83 ms** | p90 91 ms, max 96 ms |
| **Local total** | **1 633 ms** | Everything except the model |
| Gemini | not measured here | A network call; `e2e/test_gemini_live.py` exercises it |

**The largest single term is the one that is not a model.** The 700 ms
end-of-speech wait is 43 % of the local latency and more than Whisper costs. It
is how long silence must last before an utterance counts as finished, and
shortening it makes the assistant interrupt people who pause mid-sentence. It is
a product decision sitting in `SegmenterConfig`, and it is the first place to
look if the assistant feels slow.

Piper's first synthesis in a new language costs 1.9 s to load the voice. The
runtime warms both voices at startup, so nobody pays it; this was only visible
because an early version of the measurement script did not warm them and
reported it as the cost of speaking Russian.

---

## 5. Speaker verification — enrolled, measured, and set at 0.57

The owner's voice was registered through the Windows enrollment window. It took
two attempts, and the first one is the more instructive.

**The first profile agreed with itself and not with its owner.** Twelve phrases,
all accepted, cohesion 0.8433 — nominally the top band. Then it was tested:

| Condition | First profile | Second profile |
|---|---|---|
| Normal, English | 0.70 | **0.74** |
| Russian | 0.75 | 0.67 |
| **Quiet** | **0.54** | **0.65** |
| From across the room | 0.66 | 0.66 |
| **Owner's worst** | **0.54** | **0.65** |
| Nearest synthetic stranger | 0.443 | 0.497 |
| **Gap** | **0.097** | **0.153** |

The threshold was 0.55. The owner speaking quietly scored 0.54 — the profile did
not recognise the person it was built from, and nothing in its own quality score
gave any warning.

The cause was not the threshold. Twelve phrases recorded in one sitting, at one
distance, at one volume describe *one way of speaking*, and cohesion — which
measures how tightly the takes agree — rewards exactly that narrowness. A high
score there means a consistent sitting, not a good profile.

So the enrollment script now asks for variation: two takes quiet, two at a
distance, spread through the session and never first, with the instruction shown
above the phrase because people start reading the moment they see words. The
profile records which manners it heard, taken from the takes that survived
rather than from the script, and quality is judged on coverage as well as
cohesion — a profile that heard one manner cannot be rated strong however
tightly it agrees with itself.

The second profile reads *strong, cohesion 0.77, heard distant, normal, quiet*.
Cohesion fell and that is the improvement: the profile got wider, not worse. The
owner's floor rose by 0.11 and his spread narrowed from 0.21 to 0.09.

### The threshold

**0.57**, and it is configuration rather than a constant —
`ATLAS_AGENT_VOICE_SPEAKER_THRESHOLD`. It sits 0.08 below the owner's worst
measurement and 0.073 above the nearest stranger.

The margins are uneven on purpose. Four measurements do not describe a voice: a
morning voice, a cold, a headset instead of the onboard microphone all sit
somewhere below 0.65 and none of them have been measured. The strangers are
synthetic and their relationship to a real impostor is unknown in the other
direction. More room goes to the side that is more poorly sampled.

The evidence is in
[measurements/speaker-calibration.json](measurements/speaker-calibration.json),
and a test fails if the constant drifts away from it — a number that looks
justified while no longer matching its data is worse than one that was guessed.

### Whether it actually turns strangers away

Four synthetic voices, chosen because they *do* wake the detector so a refusal
is a decision about whose voice it is rather than a detector that heard nothing,
said "Jarvis, open Notepad" to the real profile at the deployed threshold. All
four were refused at the gate, and nothing reached the model — the responder
wired in for that check raises if it is ever called.

A synthetic voice is a weak stand-in for a real impostor and this is not an
impostor rate. What it establishes is that the gate is reached, that it closes,
and that closing leaves a trace rather than silence: silence there is
indistinguishable from a detector that never fired, and only one of those is
fixed by speaking louder.

Two limits stated rather than discovered later:

- **No real impostor has been measured.** No recordings of other people exist.
- **Verification is not authentication.** It filters whose speech is acted on.
  Every MEDIUM and HIGH action still goes through the Policy Engine exactly as
  before, and the voice path cannot confirm anything.

## 6. Speech recognition

faster-whisper `large-v3`, `int8_float16`, on the RTX 3060. Russian and English
from the first version, with code-switching.

**Language is settled before the words are.** Detection runs first on the audio
alone, then transcription is pinned to what it found and given a prompt in that
language. This is not architecture for its own sake — see §8, defect 2.

Code-switching works through a priming prompt carrying the program names in
Latin, plus a deterministic alias table afterwards. «Открой блокнот,
пожалуйста» comes back as *Открой Notepad, пожалуйста*: the Russian sentence
kept, the program named the way the tool catalogue names it.

Where it will still disappoint: Whisper assigns one language per utterance.
Between utterances detection is reliable; within one it is not.

### Accuracy under noise

The plan promised accuracy per signal-to-noise ratio and it had never been
measured. `scripts/measure_stt_noise.py` does it: eight commands, mixed with
pink noise and with babble — five other voices talking at once — at five ratios,
scored as word error rate. The record is in
[measurements/stt-noise.json](measurements/stt-noise.json).

| SNR | English | Russian (synthetic) |
|---|---|---|
| 30 dB | **0.00** | 0.31 |
| 20 dB | **0.00** | 0.31 |
| 10 dB | **0.00** | 0.06 – 0.31 |
| 5 dB | 0.00 – 0.04 | 0.25 |
| 0 dB | 0.07 | 0.25 |

**English is essentially unaffected by noise down to 5 dB**, where the noise is
as loud as the speech within a factor of three. That is a real result and the
mixer was verified before it was believed: asking for 0 dB produces a measured
1.9 dB, the difference being the anti-clipping rescale.

**The Russian figure is flat across every ratio, which means noise is not what
is causing it.** The errors are the same in near silence as in babble. They come
from the pairing of this recogniser with the synthetic Russian voice, and the
worst of them is not a small one:

| Spoken | Produced |
|---|---|
| «Открой блокнот.» | «Кропок нод.» — 9 times in 10 |
| «Открой блокнот, пожалуйста.» | «**Закрой** Notepad, пожалуйста.» |
| «Закрой блокнот.» | «Закрой Back Note.» |

The second row is the one to look at. *Open* became *close*. Padding the clip
with silence does not help and neither does making it longer, so the first
explanation — that these utterances are simply too short — was tested and is
wrong.

**What this does not establish.** The speaker is `ru_RU-dmitri-medium`, a
medium-quality synthesiser, not a person. A voice that pronounces «блокнот» in a
way this recogniser hears as «бэкнот» tells you about that pairing and not about
Whisper's Russian. These numbers are a lower bound. The honest next step is to
measure against recordings of the actual owner, and until then no tuning should
be done against them — fitting decoder settings to a synthesiser's artefacts is
the same mistake as fitting a wake-word threshold to a pretty number.

**What already contains it.** A misheard verb reaches the model as text and the
model proposes a tool; `app.close` is MEDIUM and needs confirmation, so a
"close" invented out of an "open" stops and asks. That is the Policy Engine
doing exactly the job it exists for, and it is why recognition accuracy is a
quality problem here rather than a safety one.

---

## 7. Conversation

| Behaviour | State |
|---|---|
| Continuous conversation | Working. Second command needs no wake word — verified end to end |
| Idle timeout | 25 s, then the wake word is required again |
| Barge-in | Speech during playback aborts the stream mid-word |
| Explicit states | `Listening / Thinking / Executing / Speaking / Muted` — all reachable |
| Acknowledgement | "Yes, sir?" — cached, spoken before the command is understood |
| Backend refused | Announced aloud, returns to Listening |
| Backend hangs | Bounded at 45 s, then announced. See §8, defect 4 |

The acknowledgement lands while the person is still speaking. That is asserted,
because an assistant that answers only after understanding feels slow however
fast it is.

---

## 8. Defects found, and how

Every one of these was found by running the thing. None would have been caught
by a unit test, and several were being actively hidden by one.

**1. Whisper had never once run.** CTranslate2 loads cuBLAS and cuDNN by bare
name through `LoadLibrary`, which reads `PATH`; the pip wheels put them under
`site-packages/nvidia/*/bin`, which Windows has no reason to search, and
`os.add_dll_directory` does not help because that is not the search
`LoadLibrary` performs. The model loaded, reported a CUDA device, and failed on
the first utterance — a broken search path wearing the costume of a broken GPU.

**2. The bilingual prompt was translating, not transcribing.** "show me how much
memory is left", spoken in English, came back as «покажи мне, сколько памяти
осталось» — reproducibly, at language-detection confidence 1.00 *for English*,
with the decoder pinned to English. Whisper takes phrasing from `initial_prompt`,
not only vocabulary, and the prompt contained a near-identical Russian sentence.
The Russian sentences could not simply be deleted: measured over four Russian
commands, a names-only prompt turned «Закрой блокнот» into «Здоровый блокнот»
and «Открой Хром» into «Рома». Each language now gets its own prompt, chosen
after detection.

**3. The command spoken in the same breath was thrown away.** The detector
reports when it *decided*, which trails the word by however long it took to be
sure. "Jarvis, open Notepad" reached Whisper as the single word "Notepad", which
it answered by reciting its own priming text. Rewinding a fixed distance does
not work — the lag varies with the voice, and half a second was enough for one
speaker and cut another off mid-phrase. The session now walks back to the last
real pause.

**4. A hanging backend was bounded at two minutes.** A refused connection
returns at once and is apologised for; a backend that accepts and then says
nothing left the session in Thinking until the HTTP client gave up. Someone
waiting for a spoken answer has no scrollbar and nothing to read — they have
silence, and silence is indistinguishable from not being heard. Bounded at 45 s
and tested against a real socket that accepts and never replies, because httpx
enforces timeouts in the transport and a mock would have proved the timeout
worked by never testing it.

**5. `Executing` was unreachable.** It had been in the state machine from the
start, but from inside the voice engine a turn that launches a program and a
turn that merely answers are the same thing: a wait. The tool runner knows the
difference and now reports it.

**6. Two utterances on one loudspeaker ended the process.** Not an exception — a
segmentation fault. PortAudio was left holding a stream nobody had a reference
to any more, because the second `play()` overwrote the first's; nothing caught
it and nothing logged it, the process simply stopped mid-run. The live
acceptance reached it on its second scenario. Three things were wrong at three
levels: `Loudspeaker` promised "one at a time" and did nothing to ensure it, the
session started a new reply without cancelling the one still playing, and **mute
stopped listening while leaving the voice running** — which is not muting, it is
being talked at by something that has stopped listening.

**7. Enrollment could not record at all.** The root project depended on
`atlas-voice[vad,wake,stt,tts]` and the `audio` extra was simply missing — the
one package that talks to the microphone was the one package absent. Every test
passed throughout, because every test either fakes the device or skips. Only a
person pressing the button could find it.

**8. The level gate sat below the room's noise floor.** `MINIMUM_RMS` was 0.01
by guesswork; this machine's microphone reads 0.0105 with nobody speaking, so a
recording of an empty room would have been accepted as a phrase and averaged
into the profile. Now 0.025 rms with a 0.10 peak floor, measured rather than
chosen.

**9. Piper is not deterministic.** Four consecutive calls with the same text,
voice and speaker produced clips of 27 121, 28 421, 26 564 and 27 121 samples,
no two identical — VITS samples its own phoneme durations and nothing seeds
that. The wake word fired on one rendition and not the next; Whisper read one as
English and another as Russian. Both looked like bugs in the code under test and
neither was. Fixtures are now cached to disk, which is what makes a green run
mean anything.

---

## 9. Live Gemini acceptance — not measured, and why

The M3 acceptance suite could not be re-run to completion.

The Gemini free tier allows **20 generate requests per day, per model** — the
429 names the quota `GenerateRequestsPerDayPerProjectPerModel-FreeTier`
explicitly. There are 38 scenarios and the pipeline ones spend two model calls
each. The suite does not fit, and no amount of pacing changes that.

What was done about it: a quota refusal is now reported as a **skip**, not a
failure, and the rest of the run is skipped with it. A suite that goes red for
billing reasons teaches people to stop reading it, and "not measured" is the
honest word for a question nobody was allowed to ask. A 38-skip run is the
current state.

`-m core` runs the reduced set that fits inside a day: 14 scenarios, of which 12
ask the model directly for one request each and 2 drive the whole pipeline for
two or three. Around 17 of the 20, which fits — with little enough margin that
adding a scenario to the core set means checking this arithmetic again.

The cost of that choice, stated plainly: a provider that is genuinely down also
now reads as "not measured". A green run here is evidence only when the skip
count is zero.

**No model default was changed.** `gemini-flash-latest` answers correctly when
the allowance has not been spent. This is also the case the alias rule was
written for: the alias kept existing while becoming unusable, and the
architecture degraded correctly rather than pretending.

---

## 10. Test coverage

| Suite | Result |
|---|---|
| `packages/atlas-voice` | 238 passing |
| `packages/atlas-agent-windows` | 189 passing |
| `packages/atlas-backend` | 270 passing |
| `packages/atlas-shared` | 211 passing |
| `e2e/test_voice_e2e.py` | 13 passing, stable across repeated runs |
| `e2e/test_live_runner.py` | 10 passing |
| `e2e/test_gemini_live.py` | skipped when the daily allowance is spent |
| ruff / format / mypy | clean, 96 source files |

Areas that had no coverage at all when M4 began and now do:

- **`capture.py`** — the microphone path, where a mistake produces silence
  rather than an error and every downstream test passes regardless, because
  each supplies its own audio.
- **`playback.py`** — including the guarantee that prevented the segfault above.
- **`build_runtime`** — five models wired together, and the whole of what the
  launcher does before the microphone opens. A mistyped model path would
  previously have been found by whoever double-clicked it.
- **Speaker verification end to end**, through the real models rather than a
  stand-in embedder.
- **The acceptance runner's own bookkeeping**, which decides whether spoken
  results survive between sittings.

## 11. What M4 does not have

Stated so it is not mistaken for an oversight:

- **The owner's own spoken acceptance.** Seven ordinary commands, listed in
  §12. Every component behind them is covered by automated tests and the whole
  path has been driven end to end with real Gemini; what is outstanding is the
  final confirmation from the person who will use it, which is a different
  thing from an untested component and is tracked separately.
- **A real impostor measurement.** No recordings of other people exist. The
  four refusals in §5 used synthetic voices.
- **Acoustic echo cancellation.** Barge-in works because the microphone hears
  the room; on loudspeakers at volume the assistant can hear itself. Untested
  with the speakers loud.
- **Replay resistance.** A recording of the owner played back will pass
  verification. This was expected and is why verification is not authentication.
- **A measured wake-word figure for a real human voice.** The 592 clips are
  synthesised and recorded fixtures; the owner's own false-reject rate in daily
  use is unknown until it is used daily.
- **Recognition accuracy for a real Russian speaker.** §6 measures a
  synthesiser, and says so. The Russian figure is a lower bound and nothing
  should be tuned against it.
- **Personality Engine.** Roadmap only, deliberately after M4's critical path,
  and it will never touch Policy Engine, permissions, risk level or SAFE MODE.

---

## 12. Checkpoint

**Built, measured and covered by automated tests.** Everything in §1 to §10.
The wake word against 592 clips, latency and cost on this machine, recognition
under noise, speaker verification through the real models, the crash the live
run found and the eight other defects before it. The gate is clean: ruff,
format, mypy and every suite.

**Outstanding, and deliberately not blocking.** The owner's own spoken run. It
is the final user acceptance rather than a gap in coverage, so development
continues; the two are tracked separately and a synthetic result never counts
towards a spoken one — they are written to different files for that reason.

Done one command at a time, whenever there is half a minute:

```
live-e2e.bat --remaining
```

| Scenario | What to say | State |
|---|---|---|
| `english_open_notepad` | Jarvis, open Notepad | awaiting |
| `russian_open_notepad` | Jarvis, открой блокнот | awaiting |
| `russian_memory` | Jarvis, покажи использование памяти | awaiting |
| `quiet` | the same command, quietly | awaiting |
| `distant` | the same command, from where you sit | awaiting |
| `continuous` | a follow-up without saying Jarvis | awaiting |
| `barge_in` | interrupt the reply mid-sentence | awaiting |

Results accumulate in
[measurements/live-voice.json](measurements/live-voice.json), each carrying the
date it was established. A scenario already passed is not asked for again.

**Not started, and gated behind this.** M5. Tracker integration is next in the
roadmap and the tracker already exists — see `docs/TRACKER-CHANGE-PLAN.md` for
the analysis that has to come before any code is written.
