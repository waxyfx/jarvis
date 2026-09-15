# Where every component stands

Milestone numbers stopped being useful once several directions started moving at
once. This is the replacement: one row per component, kept current.

**"Tested" means what was actually checked**, not what exists. A component with
a hundred unit tests and no run against the real thing says so.

Last updated: 2026-09-15.

---

## 1. Windows JARVIS — the voice and the machine

| Component | Status | Tested | Blockers |
|---|---|---|---|
| Wake word (sherpa-onnx) | done | 592 clips; 3.08 false wakes/hour, 0.233 s median | — |
| VAD, segmenter | done | synthesised speech, unit | — |
| Speaker verification | done | owner 0.74/0.67/0.65/0.66, strangers ≤0.497, threshold 0.57 | — |
| STT (faster-whisper large-v3) | done | WER 0.00 down to 5 dB SNR | — |
| TTS (Piper, RU/EN) | done | unit + live | — |
| Continuous conversation, barge-in | done | e2e with synthesised speech | owner's own voice `USER_ACCEPTANCE_PENDING` |
| Programs, processes, system metrics | done | unit, e2e, live Gemini | — |
| Policy Engine, SAFE MODE, signatures | done | unit, e2e, adversarial matrix | — |
| Gemini orchestration | done | live, `-m core` fits the free tier | 20 requests/day free tier |
| Voice acceptance, 12 scenarios | **pending** | 0 of 12 spoken | needs the owner's voice |

## 2. Sunny / Life OS

| Component | Status | Tested | Blockers |
|---|---|---|---|
| Tracker provider, 10 voice actions | done | unit, live against the real Sunny | — |
| Voice-sized digests | done | measured 9.9 s → 8.8 s | — |
| `REQUIRE_AUTH`, machine token | done | four checks, live | — |
| Deletion by voice | **deliberately absent** | — | not wanted |

## 3. Proactive JARVIS

| Component | Status | Tested | Blockers |
|---|---|---|---|
| Rules: reminder, briefing, summary, wellness | done | 26 unit | — |
| Prayer reminder | done | 20 unit | needs coordinates |
| Scheduler loop | done | 13 integration | — |
| `server.notify`, signed | done | 4 e2e, incl. forged frame → SAFE MODE | — |
| Delivery: speak + tray | done | 13 unit, e2e with speakers stood in | never heard aloud by the owner |
| Runs 24/7 | **no** | — | backend is on the laptop — see §6 |

## 4. Daily reports

| Component | Status | Tested | Blockers |
|---|---|---|---|
| Report content | done | 20 unit | — |
| Filing into Sunny as a note | done | one real note filed | — |
| Written when the laptop is off | **no** | — | see §6 |

## 5. Web, activity, prayer

| Component | Status | Tested | Blockers |
|---|---|---|---|
| `web.search` (DuckDuckGo Lite) | done | 10 unit, live, live Gemini chooses it | single provider |
| `web.read` + SSRF refusals | done | 23 unit, 18 gate | — |
| `activity.today` | done | 16 + 7, live through the stack | — |
| Prayer computation | done | 51 unit, from first principles | coordinates `USER_ACTION_REQUIRED` |
| Prayer timetable (supplied) | validated | 24 unit | nothing reads a file yet |
| Personality | done | 95 + 8, live | — |

## 6. Always-on backend

| Component | Status | Tested | Blockers |
|---|---|---|---|
| Local launcher | done | cold start to connected, ~10 s | — |
| Dockerfile, compose, Caddy | written | **never built** | no Docker here |
| One-command VPS setup | in progress | — | — |
| Moving the existing data and identity | in progress | — | — |
| The VPS itself | — | — | `USER_ACTION_REQUIRED`: a host |

## 7. iPhone and remote control

| Component | Status | Tested | Blockers |
|---|---|---|---|
| SwiftUI app | not started | — | needs a Mac to build; source can be written |
| Push notifications | not started | — | Apple developer account |
| Remote screen, touchpad | not started | — | after the app |

## 8. Not started, not blocked

Memory and personalisation · Kazakh · web protection · presence and posture.

---

## What needs the owner, and nothing else

1. **Prayer coordinates.** Two lines in `.env`. Not guessed: a timezone locates
   a city to within a few hundred kilometres, which is twenty minutes of Maghrib.
2. **A VPS**, if the proactive half should work with the laptop shut.
3. **Twelve spoken scenarios**, for the voice acceptance.
4. **A Mac and an Apple account**, for anything on the phone.

Nothing else here is waiting on them.
