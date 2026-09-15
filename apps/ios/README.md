# JARVIS for iPhone

**This has never been compiled.** There is no Mac in this project, so every
line was written against the backend's real wire format and none of it has been
through a compiler. Treat the first build as part of acceptance, not as a step
known to work. Expect to fix small things; the shape should be right, because
the protocol it speaks is the one the tests on the backend side exercise.

## What it is for

The laptop is where JARVIS listens. The phone is for the three things a laptop
is bad at:

- **finding out what happened** — is the machine on, what did it do, how long
  was the day;
- **answering the one question it is holding** — the Policy Engine holds MEDIUM
  and HIGH actions for confirmation, and a phone in your pocket is a better
  place to say yes than a machine in another room;
- **typing** when speaking aloud would be rude.

It is deliberately not a second voice assistant. Continuous listening, wake
words and speaker verification stay on the machine that has the microphone and
the models.

## Four files

| File | What it holds |
|---|---|
| `Identity.swift` | The Ed25519 key, the Keychain, and the exact bytes the backend expects signed |
| `Backend.swift` | Five calls. Pairing, token, overview, ask, confirm |
| `JARVISApp.swift` | Paired or not paired — the app is which one it is in |
| `Views.swift` | Pair, home, ask |

No dependencies, no package manager, no build script. Drop them into a new
SwiftUI project.

## Building it

1. Xcode 15 or later, iOS 17 target (the app uses `NavigationStack`,
   `.refreshable` and `TextField(axis:)`).
2. **File → New → Project → iOS → App.** Name it `JARVIS`, interface SwiftUI,
   language Swift. Delete the `ContentView.swift` and `JARVISApp.swift` it
   generates.
3. Drag the four files from `apps/ios/JARVIS/` into the project.
4. Signing: your own Apple ID works for running on your own phone. A paid
   developer account is only needed for TestFlight and for push notifications.
5. If the backend is on plain HTTP — a local backend, during development — iOS
   blocks it. Either use the HTTPS address the VPS serves, or add an App
   Transport Security exception for that host, and remove it afterwards.

## Pairing it

The phone needs its own pairing code — the one the agent used is single-use and
already spent. On the machine running the backend:

```bash
curl -fsS -X POST https://your-domain/v1/pair/start \
  -H "X-Atlas-Bootstrap-Token: $ATLAS_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"ios","name":"iPhone"}'
```

Type the `code_display` into the app with the server address. The phone
generates its key, signs the code with it, and is enrolled. Nothing secret is
typed, and the code is single-use.

## What is deliberately absent

**Push notifications.** They need an Apple developer account, an APNs key and a
paid membership, and they need the backend to hold a device token — none of
which can be done from here. The proactive notifications currently reach the
Windows agent, which speaks them aloud. Wiring the same `server.notify` messages
to APNs is the next step and is independent of everything above.

**A websocket.** The phone polls on foreground and on pull-to-refresh. A socket
would cost battery for a screen that is looked at for ten seconds at a time; it
becomes worth it when push exists and the app needs to act on a notification.

**Remote screen and touchpad.** A separate piece of work, and not a small one.
It needs its own security review before a line of it is written: a phone that
can move the mouse on a machine is a different threat model from a phone that
can ask questions.

## What the owner has to do

1. A Mac with Xcode. Nothing here can substitute for it.
2. An Apple ID for signing. Free for running on your own device.
3. A reachable backend — ideally the VPS, so the phone works away from home.

Until then this is source code that has been reasoned about carefully and never
executed, and that distinction matters more than the line count.
