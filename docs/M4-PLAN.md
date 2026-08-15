# M4 — Voice Engine: technical plan

> Status: **stack chosen, implementation started**.
> Date: 2026-08-15. Builds on [M3-REPORT.md](M3-REPORT.md).

## Chosen stack

| | Decision |
|---|---|
| **TTS** | **Hybrid, local by default** — Kokoro-82M (`bm_*`) for English, Piper (`dmitri`) for Russian. Azure exists behind a flag that is **off** until you have heard both |
| **STT** | **faster-whisper large-v3**, int8_float16 on CUDA, behind `STTProvider` |
| **Wake word** | **openWakeWord to begin with**, false accepts measured on podcast audio and live speech; the Porcupine decision is deferred until those numbers exist |
| VAD | Silero v5 |
| Speaker verification | ECAPA-TDNN (SpeechBrain), ONNX, embedding under DPAPI |

```
«Atlas» → wake word → speaker verification → VAD → STT → backend → Gemini
        → Policy Engine → signed command → Windows Agent → action → TTS
```

## 0. Where the voice engine lives, and why it matters

**Everything that touches audio runs on the Windows Agent. Only text crosses the
network.**

This is not a performance choice. It follows from rules already agreed:

* the microphone kill switch must be local and must outrank the backend, exactly
  as SAFE MODE does — a remote party that can unmute a microphone is a bug, not
  a feature;
* raw recordings and the voice embedding must never reach Gemini;
* an assistant that needs a VPS round trip to notice you said "stop" is not
  interruptible.

So the backend contract does not change at all in M4. The agent produces text
and posts it to the same `POST /v1/assistant/message` that M3 built; the reply
comes back as text and is spoken locally. **The Policy Engine, the signing path
and the confirmation flow are untouched.** Voice is a new front door to the
existing house, not a new house.

One consequence worth stating plainly: **speaker verification is not
authentication** and M4 does not treat it as such. It gates *whose speech ATLAS
listens to*, nothing more. MEDIUM and HIGH still go through the Policy Engine
and the M2 confirmation path exactly as they do for typed input.

## 1. Target hardware (measured on this machine)

| | |
|---|---|
| CPU | AMD Ryzen 5 5600H, 6 cores / 12 threads |
| RAM | 15.4 GB |
| GPU | **NVIDIA RTX 3060 Laptop, 6 GB VRAM**, driver 580.97 |
| iGPU | AMD Radeon (ignored) |
| Audio in | Realtek onboard mic; Logitech G435 over Bluetooth |

The 3060 is the deciding fact: it is enough to run the whole local stack with
room to spare.

| Component | VRAM | Disk | Latency |
|---|---|---|---|
| Wake word (openWakeWord) | CPU | ~5 MB | ~5 ms/frame |
| VAD (Silero v5) | CPU | ~2 MB | ~1 ms/30 ms frame |
| STT (faster-whisper large-v3, int8_float16) | ~2.5–3 GB | ~1.6 GB | ~0.4–0.8 s for a 5 s utterance |
| Speaker verification (ECAPA-TDNN) | CPU or ~150 MB | ~80 MB | ~50 ms |
| TTS (Kokoro-82M or Piper) | CPU | 0.3 GB / 60 MB | faster than real time |
| **Total** | **~3 GB of 6** | **~2 GB** | |

**A Bluetooth caveat you should know before choosing.** The G435, like every
Bluetooth headset, switches to HFP when the microphone opens: output collapses
to narrowband mono and the British voice will sound like a phone call for as
long as ATLAS is listening. Since ATLAS listens continuously, that is *always*.
Two ways out — use the onboard mic for input and keep the headset on A2DP for
output, or accept HFP quality. I will make the input and output devices
separately configurable so either works.

## 2. Wake word

"Atlas" is two syllables and phonetically close to ordinary speech ("at last",
"Атлант", "атлас" as a common noun — a Russian speaker says it in normal
conversation). False accepts are the real risk, not misses.

| Option | Licence | Local | Notes |
|---|---|---|---|
| **openWakeWord** | Apache-2.0 | fully | Custom word trained from synthetic speech; no key, no vendor, no telemetry |
| Porcupine (Picovoice) | proprietary, free for personal use | inference local | Best-in-class false-accept rate; needs an AccessKey |
| Vosk keyword spotting | Apache-2.0 | fully | Weaker; effectively runs a whole recogniser |

**Recommendation: openWakeWord**, because it introduces no vendor and no key
into the one component that is always listening. If measured false accepts are
unacceptable after tuning, Porcupine is the fallback and the interface makes the
swap a one-file change.

**Two-stage gate, which matters more than the engine choice.** A wake-word hit
alone does not wake ATLAS. The same audio window is passed to speaker
verification, and only a hit *from your voice* produces "Yes, sir?". This kills
most false accepts (the television, a podcast, a colleague) at no extra latency,
because the embedding is computed on audio already buffered.

## 3. VAD

**Silero VAD v5** — MIT, ~2 MB ONNX, about a millisecond per 30 ms frame on CPU,
and far better in noise than WebRTC VAD. There is no serious competitor at this
size. No decision needed here.

It does three jobs: deciding when your utterance ended (turn-taking), keeping
silence out of the STT input, and detecting that you started talking while ATLAS
is speaking (barge-in).

## 4. STT — Russian, English, and the honest truth about code-switching

| Option | Licence | ru+en | Notes |
|---|---|---|---|
| **faster-whisper large-v3** (CTranslate2) | MIT (model MIT) | strong both | int8_float16 fits comfortably; the default choice |
| faster-whisper medium | MIT | good | ~1.5 GB VRAM; fallback if the GPU is busy |
| NVIDIA Canary / Parakeet multilingual | CC-BY / NVIDIA OM | strong | Excellent quality, but NeMo is a heavy dependency |
| Vosk | Apache-2.0 | mediocre | Language fixed up front — wrong shape for this |
| Cloud (Gemini audio, Deepgram, Azure) | — | strong | **Sends your raw voice off the machine. Rejected as the default** |

**Recommendation: faster-whisper large-v3, int8_float16, CUDA**, behind an
`STTProvider` so any of the above can replace it.

**Where this will disappoint, stated up front.** Whisper assigns *one* language
per utterance. Intra-sentence code-switching — «Открой **VS Code** и покажи
**memory usage**» — is precisely its weak spot: it may transliterate the English
into Cyrillic («открой вс код»). Auto-detection between whole utterances works
well; auto-detection *within* one does not, in any local model I know of.

The mitigation is unglamorous and effective: feed Whisper an `initial_prompt`
containing the vocabulary that actually matters — application names, the tool
catalogue's nouns, "VS Code, Chrome, Notepad, Telegram, PowerShell" — which
biases decoding toward Latin spellings of those tokens inside Russian speech.
Then normalise the transcript against a small alias table before it reaches
Gemini (`вс код`, `вэс код`, `вс-код` → `VS Code`). That alias table is the same
kind of object M3 already uses for tool aliases, and it is testable offline
without a microphone.

I will measure this and report real numbers rather than claiming it works.

## 5. Speaker verification

| Option | Licence | Notes |
|---|---|---|
| **SpeechBrain ECAPA-TDNN** (`spkrec-ecapa-voxceleb`) | Apache-2.0 | 192-dim embedding, ~1% EER on VoxCeleb, ~50 ms on CPU, no gating |
| pyannote/embedding | MIT, **gated** | Requires a Hugging Face token and accepting terms |
| WeSpeaker / 3D-Speaker CAM++ | Apache-2.0 | Slightly better EER, less turnkey |

**Recommendation: ECAPA-TDNN**, exported to ONNX so the runtime stays light.

**Enrollment flow.** ATLAS asks you to read 8–12 short phrases, mixed Russian and
English, roughly 30–45 seconds of speech total — enough for a stable centroid.
It shows progress, rejects takes that are too quiet or clipped, and asks you to
repeat those. It computes one embedding per phrase, checks they agree with each
other (a bad take that slips through would poison the centroid), and stores the
mean.

**Storage.** The embedding — a few hundred floats, not audio — is encrypted with
DPAPI under your Windows account, the same mechanism M1 uses for the device key.
**Raw recordings are deleted immediately after enrollment by default**, with an
explicit opt-in flag to keep them for re-enrollment. `enroll`, `re-enroll` and
`delete` are local agent commands; the backend cannot invoke them.

**The replay attack, honestly.** ECAPA compares timbre. It does not know whether
the sound came from a throat or a speaker, so **a decent recording of your voice
played back will pass**. This is a known property of every speaker-verification
model without a dedicated anti-spoofing stage, and it is the main reason your
instruction to treat it as a convenience filter rather than authentication is
the correct one. I will test it and report the result rather than pretending
otherwise. If it matters later, an anti-spoofing model (AASIST-family) is a
separate M-level addition; what protects you today is that MEDIUM and HIGH still
require confirmation through the M2 path.

## 6. TTS — this is the decision I need from you

The requirement is an *original* calm British male AI voice, with no cloning of
any real actor. All options below are original voices; none clones anyone.

The complication is **Russian**. ATLAS answers in Russian too, and the best
British-male options do not speak it — so the choice is really about whether one
voice identity carries across both languages.

| Option | Cost | Privacy | ru + en | Quality |
|---|---|---|---|---|
| **A. Kokoro-82M + Piper** | free | fully local | `bm_george`/`bm_lewis` for English, Piper `dmitri`/`ruslan` for Russian — **two different timbres** | English very good; Russian good |
| **B. Piper only** | free | fully local | en_GB `alan` + ru `dmitri` — two timbres, one engine | Both clearly synthetic, calm and clear |
| **C. Azure Neural TTS, multilingual voice** | ~$16 per 1M chars → well under $1/month for personal use | **reply text leaves the machine** | **One voice identity across both languages** | Best of the three |
| **D. Hybrid** | free by default | local unless you flip a switch | A locally, C when enabled | — |

**An argument for C that I want to make explicitly, because it cuts against the
project's instincts.** The text ATLAS speaks was *written by Gemini* — it already
came from the cloud. Sending that same sentence to Azure to be spoken exposes
nothing that has not already left the machine. What must stay local is **your
voice**: the recordings, the embedding, the STT. Those stay local under every
option here. So cloud TTS is a materially smaller concession than cloud STT
would be, and it is the only way to get one consistent voice identity across
Russian and English — which is exactly what a code-switching assistant needs.

**My recommendation: D, defaulting to A.** Ship fully local, make Azure a flag
you can turn on after hearing both. If you want the single-identity voice from
day one, choose C.

## 7. Conversation behaviour

* **Short acknowledgement.** "Yes, sir?" within ~200 ms of the wake word,
  played from a pre-rendered file rather than synthesised, so it is instant.
* **Continuous conversation.** After the first «Atlas», the session stays open;
  VAD handles turn-taking and no further wake word is needed.
* **Idle timeout.** The session closes after configurable silence (default 25 s),
  announced by a soft tone rather than words. Saying «спасибо» / "that's all"
  also closes it.
* **Barge-in.** The microphone stays open while ATLAS speaks. Speech from the
  verified speaker stops playback immediately, mid-word.
* **Mute.** Tray toggle and a global hotkey, agent-side, same precedence as the
  kill switch. The backend cannot unmute; when muted the audio device is
  released, not merely ignored, so the OS microphone indicator goes dark.
* **States.** `Listening / Thinking / Executing / Speaking / Muted`, shown in the
  tray icon and an optional small always-on-top pill.
* **No network, no Gemini.** Wake word, VAD, STT and TTS are all local, so ATLAS
  still hears you and still answers — it says it cannot reach the model and
  closes the turn. The M3 turn timeout already bounds this; the voice engine
  must never sit in a spinner. This is a first-class test, not an afterthought.

**Barge-in is the hard part.** With a headset, the headset's own echo
cancellation carries it. With speakers, ATLAS hears itself and interrupts
itself. That needs acoustic echo cancellation — WebRTC APM with the render
stream as reference. I am budgeting real work for this and will report what it
achieves on speakers rather than only demonstrating it on a headset.

## 8. Structure and dependency weight

```
packages/atlas-voice/          new: engine-agnostic, no Windows API
  providers/  wake.py  vad.py  stt.py  speaker.py  tts.py     ← protocols
  engines/    openwakeword.py  silero.py  faster_whisper.py  ecapa.py  kokoro.py  piper.py  azure.py
  session.py  the conversation state machine
  normalize.py  transcript → command text, alias table
packages/atlas-agent-windows/
  voice/      audio devices, mute, tray states, hotkey, DPAPI profile store
```

`torch` plus CUDA is roughly 2.5 GB. It goes in an **optional extra**
(`atlas-voice[cuda]`) so the backend and the VPS install stay lean — the VPS must
not grow a machine-learning stack because the desktop grew a microphone.

## 9. How it will be tested

Deterministic, offline, from fixture WAVs — no microphone needed in CI:

| Case | Method |
|---|---|
| **False wake words** | "at last", "Atlanta", «атлас» as a common noun, «Атлантика», plus 30 min of podcast audio. Measured as false accepts per hour, not pass/fail |
| **Someone else's voice** | Synthesised and recorded non-owner speech saying «Atlas, закрой Notepad» → must not pass verification |
| **Replay through a speaker** | Your enrollment audio played through the laptop speakers into the mic. **Expected to pass verification** — recorded as a measured limitation with the exact score |
| **Noise** | Fixtures mixed with café/keyboard/fan noise at several SNRs; wake-word and STT accuracy reported per SNR |
| **Russian / English / mixed** | The M3 command set, spoken. Asserts the *tool and arguments* after the full voice path, reusing the M3 assertions |
| **Barge-in** | Synthetic: TTS playing, speech injected, assert playback stopped within N ms |
| **Model unreachable** | Gemini stubbed to fail; assert ATLAS speaks the failure and returns to Listening within the timeout |
| **Mute** | Assert the device is released and that a backend message cannot re-open it |

## 10. Sub-milestones

| | Scope |
|---|---|
| M4.1 | Audio I/O, device selection, mute, tray states, `atlas-voice` skeleton and provider protocols |
| M4.2 | VAD + wake word, false-accept measurement |
| M4.3 | Speaker verification, enrollment / re-enrollment / delete, DPAPI storage |
| M4.4 | STT, language handling, transcript normalisation |
| M4.5 | TTS, "Yes, sir?", barge-in, echo cancellation |
| M4.6 | Session state machine: continuous conversation, idle timeout, offline behaviour |
| M4.7 | The full test matrix in §9, report, commit, tag `m4` |

## 11. Out of scope, per your instruction

iPhone, remote control and vision are **not** in M4. Nothing here anticipates
them beyond keeping the provider protocols transport-agnostic.

## 12. A note on model availability

M3 taught this the hard way: a model id that is still *listed* may no longer be
*served*. Every model named in this plan will be verified as actually loadable
at implementation time, and the plan updated if one has moved. Names and
licences here reflect the state I know; treat them as subject to that check.
