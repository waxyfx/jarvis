"""The whole path, on the real stack, end to end.

    voice → wake word → speaker check → Whisper → backend → Gemini
          → Policy Engine → agent → action → Piper

Nothing is stubbed. This uses ``build_runtime`` — the same function the launcher
calls — against a running backend, the real model and the real agent, so a pass
here means the thing works rather than that the test does.

Two modes, and the difference between them is exactly one thing: whose voice.

**``--auto``** feeds synthesised speech and turns speaker verification off,
because a synthesiser is not the owner and would be refused at the gate. Everything
below that gate is real, including Gemini and including the program that opens on
this machine. It runs unattended.

**Interactive** (the default) opens the microphone with verification on and asks
a person to speak each scenario in turn. It is the only way to test the things
that need a human: a quiet voice, a voice from across the room, interrupting a
reply mid-sentence. There is no substitute for this and the script does not
pretend to be one.

Model requests are the scarce resource. The free tier allows twenty a day per
model and each scenario spends one or two, so the scenario list is short on
purpose and the script says how many it expects to use before it starts.

    uv run python scripts/live_voice_e2e.py --auto
    uv run python scripts/live_voice_e2e.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "atlas-voice" / "tests"))

from atlas_agent.config import get_agent_settings  # noqa: E402
from atlas_agent.identity import IdentityStore  # noqa: E402
from atlas_agent.runner import ToolRunner  # noqa: E402
from atlas_agent.safety.mode import SafeModeController  # noqa: E402
from atlas_agent.safety.paths import PathGuard  # noqa: E402
from atlas_agent.transport import AgentTransport  # noqa: E402
from atlas_agent.voice_runtime import BackendVoice, VoiceModels, build_runtime  # noqa: E402
from atlas_agent.voice_ui import _dpapi_protector  # noqa: E402
from atlas_shared.tools.manifest import RiskContext  # noqa: E402
from atlas_voice.audio import SAMPLE_RATE, frames_from_array  # noqa: E402
from atlas_voice.profile import VoiceProfileStore  # noqa: E402
from atlas_voice.session import SessionConfig, SessionEvent  # noqa: E402
from atlas_voice.state import VoiceState  # noqa: E402
from voicefixtures import PIPER_MULTI, say  # noqa: E402

MODELS = VoiceModels(root=REPO / ".models")

#: Voices that are not the owner. Chosen because they wake the detector, so a
#: refusal is a decision about whose voice it is rather than a detector that
#: heard nothing.
STRANGER_VOICES = (200, 333, 640, 11)
RESULTS = REPO / "docs" / "measurements"


@dataclass
class Scenario:
    """One thing to say, and what should happen when it is said."""

    name: str
    #: Shown to the person in interactive mode.
    instruction: str
    #: What the transcript should contain, lower-cased. Any one of them.
    expect_words: tuple[str, ...] = ()
    #: A tool that should end up executed, if any.
    expect_tool: str | None = None
    #: Said without the wake word, inside a conversation already open.
    continues: bool = False
    #: Interrupt the reply partway through instead of letting it finish.
    barge_in: bool = False
    #: Synthesised for --auto. None means the scenario needs a person.
    synthetic: tuple[Path, str, int | None] | None = None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="english_open_notepad",
        instruction="Say:  Jarvis, open Notepad",
        expect_words=("notepad",),
        expect_tool="app.launch",
        synthetic=(PIPER_MULTI, "Jarvis, open Notepad.", 200),
    ),
    Scenario(
        name="russian_open_notepad",
        instruction="Скажите:  Jarvis, открой блокнот",
        expect_words=("notepad", "блокнот"),
        expect_tool="app.launch",
        # The English voice cannot say this and the Russian one never wakes the
        # detector, so there is no honest way to synthesise it.
        synthetic=None,
    ),
    Scenario(
        name="russian_memory",
        instruction="Скажите:  Jarvis, покажи использование памяти",
        expect_words=("памяти", "memory"),
        expect_tool="system.metrics",
        synthetic=None,
    ),
    Scenario(
        name="quiet",
        instruction="Скажите ту же команду про память — вполголоса",
        expect_words=("памяти", "memory"),
        expect_tool="system.metrics",
        synthetic=None,
    ),
    Scenario(
        name="distant",
        instruction="Скажите её ещё раз — с того места, где вы обычно сидите",
        expect_words=("памяти", "memory"),
        expect_tool="system.metrics",
        synthetic=None,
    ),
    Scenario(
        name="continuous",
        instruction="Не говоря Jarvis:  а сколько осталось места на диске",
        expect_words=("диск", "disk", "места"),
        continues=True,
        synthetic=(PIPER_MULTI, "And how much disk space is left?", 200),
    ),
    Scenario(
        name="barge_in",
        instruction="Скажите Jarvis и команду, затем перебейте ответ на полуслове",
        barge_in=True,
        synthetic=None,
    ),
)


@dataclass
class Outcome:
    scenario: str
    woke: bool = False
    rejected: bool = False
    transcript: str = ""
    reply: str = ""
    tools: list[str] = field(default_factory=list)
    spoke: int = 0
    seconds: float = 0.0
    passed: bool = False
    note: str = ""
    #: Every session event during the scenario. The difference between "it did
    #: not hear me" and "it heard me and then something else happened" is only
    #: visible here, and guessing at it wastes more time than recording it.
    trace: list[str] = field(default_factory=list)

    def row(self) -> str:
        mark = "pass" if self.passed else "FAIL"
        return (
            f"  {mark}  {self.scenario:22} {self.seconds:5.1f}s  "
            f"wake={'y' if self.woke else 'n'} "
            f"said={self.transcript[:40]!r}"
        )


class Watcher:
    """Collects session events and lets a scenario wait for one."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []
        self.since = 0

    def __call__(self, event: SessionEvent) -> None:
        self.events.append(event)

    def mark(self) -> None:
        self.since = len(self.events)

    def new(self) -> list[SessionEvent]:
        return self.events[self.since :]

    async def wait_for(
        self, kinds: tuple[str, ...], *, timeout: float, after: int | None = None
    ) -> tuple[SessionEvent, int] | None:
        """The next event of these kinds, and where it sits in the log.

        ``after`` matters more than it looks. The acknowledgement is spoken
        before the command is understood, so a plain search for "speaking"
        finds "Yes, sir?" and reports it as the answer — which is how the first
        run of this script recorded a scenario as having replied while its tool
        list was still empty, because the turn had not finished.
        """
        start = self.since if after is None else after + 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index in range(start, len(self.events)):
                if self.events[index].kind in kinds:
                    return self.events[index], index
            await asyncio.sleep(0.05)
        return None


async def start_agent(
    settings: Any, identity: Any
) -> tuple[Any, asyncio.Event, asyncio.Task[None]]:
    """A real connected agent, so tools actually run on this machine."""
    controller = SafeModeController(settings.mode_state_path)
    runner = ToolRunner(
        safe_mode=controller,
        path_guard=PathGuard(settings.allowed_file_roots),
        risk_context=RiskContext(
            allowed_roots=settings.allowed_file_roots,
            executable_roots=settings.allowed_executable_roots,
        ),
    )
    transport = AgentTransport(settings, identity, runner=runner, safe_mode=controller)
    stop = asyncio.Event()
    task = asyncio.create_task(transport.run(stop=stop))
    await asyncio.wait_for(transport.connected.wait(), timeout=30)
    return transport, stop, task


async def run_scenario(
    scenario: Scenario,
    *,
    runtime: Any,
    watcher: Watcher,
    voice: BackendVoice,
    auto: bool,
) -> Outcome:
    outcome = Outcome(scenario=scenario.name)
    watcher.mark()
    started = time.monotonic()

    if auto:
        if scenario.synthetic is None:
            outcome.note = "needs a person"
            return outcome
        model, text, speaker = scenario.synthetic
        clip = say(model, text, speaker_id=speaker)
        audio = np.concatenate(
            [
                np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32),
                clip,
                np.zeros(int(1.4 * SAMPLE_RATE), dtype=np.float32),
            ]
        )
        for frame in frames_from_array(audio):
            await runtime.session.push(frame)
    else:
        print(f"\n  {scenario.instruction}")
        print("  (listening — speak now)")

    found = await watcher.wait_for(("heard", "rejected", "empty"), timeout=60 if auto else 45)
    if found is None:
        outcome.note = "nothing was heard"
        outcome.seconds = time.monotonic() - started
        outcome.trace = [f"{e.at:.2f} {e.kind}" for e in watcher.new()]
        return outcome
    heard, heard_at = found
    if heard.kind == "rejected":
        outcome.rejected = True
        outcome.note = heard.detail
        outcome.seconds = time.monotonic() - started
        outcome.trace = [f"{e.at:.2f} {e.kind}" for e in watcher.new()]
        return outcome

    outcome.woke = any(event.kind == "wake" for event in watcher.new())
    outcome.transcript = heard.detail

    # After the transcript, not merely after the scenario began: everything
    # before this point is the acknowledgement.
    spoken = await watcher.wait_for(("speaking", "turn_failed"), timeout=120, after=heard_at)
    outcome.seconds = time.monotonic() - started
    if spoken is None or spoken[0].kind == "turn_failed":
        outcome.note = "the turn failed" if spoken else "no reply was spoken"
        outcome.trace = [f"{e.at:.2f} {e.kind}" for e in watcher.new()]
        return outcome

    outcome.reply = spoken[0].detail
    outcome.spoke = sum(1 for event in watcher.new() if event.kind == "speaking")
    answer = voice.last or {}

    # A spent allowance is not a voice failure and must not be reported as one.
    # The free tier gives twenty model requests a day; the assistant answers
    # "the model is unavailable" aloud, which through this harness would
    # otherwise read as the wrong words coming back.
    if answer.get("stopped_because") == "provider_unavailable":
        outcome.note = (
            "the model's daily allowance is spent — everything up to the model "
            "worked, and this scenario was not measured"
        )
        outcome.trace = [f"{e.at:.2f} {e.kind}" for e in watcher.new()]
        return outcome
    outcome.tools = [
        str(call.get("tool")) for call in answer.get("executed", []) if isinstance(call, dict)
    ]

    outcome.trace = [
        f"{event.at:.2f} {event.kind}" + (f" {event.detail[:30]}" if event.detail else "")
        for event in watcher.new()
    ]

    said = outcome.transcript.lower()
    words_ok = not scenario.expect_words or any(word in said for word in scenario.expect_words)
    tool_ok = scenario.expect_tool is None or scenario.expect_tool in outcome.tools
    # A continuation that woke the detector proves nothing about continuation.
    continuation_ok = not scenario.continues or not outcome.woke
    woke_ok = scenario.continues or outcome.woke
    # And an interruption that never interrupted proves nothing either.
    interrupted_ok = not scenario.barge_in or any(
        event.kind == "barge_in" for event in watcher.new()
    )

    outcome.passed = words_ok and tool_ok and continuation_ok and woke_ok and interrupted_ok
    if not interrupted_ok:
        outcome.note = "the reply was never interrupted"
    elif not woke_ok:
        outcome.note = "the wake word never fired"
    elif not continuation_ok:
        outcome.note = "it needed the wake word again, so the conversation had closed"
    elif not words_ok:
        outcome.note = f"expected one of {scenario.expect_words}"
    elif not tool_ok:
        outcome.note = f"expected {scenario.expect_tool}, ran {outcome.tools}"
    return outcome


def merge_results(
    path: Path, outcomes: list[Outcome], settings: Any, *, auto: bool
) -> dict[str, Any]:
    """Keep what earlier runs established instead of starting over.

    An acceptance that needs a person to speak seven times is not something
    anyone does in one sitting, and a report that only remembers the last run
    punishes doing it in pieces — which is the only way it actually gets done.
    A scenario's newest result replaces its previous one; everything else
    stands.
    """
    previous: dict[str, Any] = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            previous = {row["scenario"]: row for row in stored.get("outcomes", [])}
        except (json.JSONDecodeError, KeyError, TypeError):
            # A corrupted record is not a reason to refuse to write a good one.
            previous = {}

    for outcome in outcomes:
        previous[outcome.scenario] = vars(outcome) | {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    record = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "auto" if auto else "interactive",
        "threshold": settings.voice_speaker_threshold,
        "outcomes": [previous[name] for name in previous],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def already_passed(auto: bool) -> set[str]:
    """Scenario names recorded as having passed in an earlier sitting."""
    path = RESULTS / ("live-voice-auto.json" if auto else "live-voice.json")
    if not path.is_file():
        return set()
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return set()
    return {row["scenario"] for row in stored.get("outcomes", []) if row.get("passed")}


def report_remaining(record: dict[str, Any]) -> None:
    """What still has to be spoken, so the next sitting can be a short one."""
    passed = {row["scenario"] for row in record["outcomes"] if row.get("passed")}
    remaining = [s.name for s in SCENARIOS if s.name not in passed]
    if not remaining:
        print("\nEvery scenario has passed at some point. M4 acceptance is complete.")
        return
    print(f"\nStill to do ({len(remaining)}): {', '.join(remaining)}")
    print(f"  one at a time:  live-e2e.bat --only {remaining[0]}")


async def preflight(settings: Any) -> bool:
    """Everything that has to be true before a person is asked to speak.

    Checked together and reported together. The alternative is discovering the
    backend is down after the models have loaded and someone is already talking
    into a microphone, and then discovering the next thing after fixing that
    one.
    """
    problems: list[str] = []

    identity = IdentityStore(settings.identity_path).load()
    if identity is None or not identity.is_enrolled:
        problems.append("this machine is not paired — run: atlas-agent pair --code XXXX-XXXX")

    for item in MODELS.missing():
        problems.append(f"missing model: {item}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.backend_url}/v1/health")
        if response.status_code != 200:
            problems.append(f"the backend answered {response.status_code}")
    except Exception:
        problems.append(
            f"the backend is not answering at {settings.backend_url} — "
            "start it with: uv run atlas-backend  (and PostgreSQL before it)"
        )

    if problems:
        print("Not ready:")
        for problem in problems:
            print(f"  - {problem}")
        return False
    return True


async def check_stranger(settings: Any, identity: Any, store: VoiceProfileStore) -> int:
    """Someone who is not the owner, against the owner's real profile.

    The one part of the acceptance that can be done without the owner present,
    and it costs no model requests because nothing reaches the model — that is
    the whole point. Verification is on and the threshold is whatever is
    configured, so this exercises the deployed setting rather than a test one.

    A synthetic voice is a weak stand-in for a real impostor and this is not an
    impostor rate. What it establishes is that the gate is reached, that it
    closes, and that closing leaves a trace instead of silence.
    """
    if store.load() is None:
        print("No voice profile to test against. Run enroll-voice.bat first.")
        return 1

    watcher = Watcher()
    runtime = await build_runtime(
        settings=settings,
        identity=identity,
        store=store,
        models=MODELS,
        responder=_refuse_to_answer,
        config=SessionConfig(verify_speaker=True, idle_timeout_s=30.0),
        on_event=watcher,
    )
    runtime.session.start()

    turned_away = 0
    for speaker in STRANGER_VOICES:
        watcher.mark()
        clip = say(PIPER_MULTI, "Jarvis, open Notepad.", speaker_id=speaker)
        audio = np.concatenate(
            [
                np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32),
                clip,
                np.zeros(int(1.2 * SAMPLE_RATE), dtype=np.float32),
            ]
        )
        for frame in frames_from_array(audio):
            await runtime.session.push(frame)

        kinds = [event.kind for event in watcher.new()]
        rejected = "rejected" in kinds
        reached_model = "heard" in kinds
        turned_away += rejected and not reached_model
        mark = "turned away" if rejected and not reached_model else "LET THROUGH"
        print(f"  voice {speaker:4}: {mark}  ({', '.join(kinds) or 'nothing happened'})")
        runtime.session.mute()
        runtime.session.unmute()

    runtime.stop()
    print(f"\n{turned_away} of {len(STRANGER_VOICES)} strangers were turned away")
    print(f"threshold {settings.voice_speaker_threshold}")
    return 0 if turned_away == len(STRANGER_VOICES) else 1


async def _refuse_to_answer(transcript: Any) -> str:
    """Nothing should ever call this. If it does, the gate let someone past."""
    raise AssertionError(f"a stranger reached the model: {transcript.text!r}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true", help="synthetic speech, no person needed")
    parser.add_argument(
        "--stranger",
        action="store_true",
        help="check that a voice which is not the owner is turned away, and stop",
    )
    parser.add_argument("--only", nargs="*", help="run only these scenarios by name")
    parser.add_argument(
        "--remaining",
        action="store_true",
        help="run only what has not passed yet, so a session can be short",
    )
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    settings = get_agent_settings()
    if not await preflight(settings):
        return 1

    identity = IdentityStore(settings.identity_path).load()
    assert identity is not None

    store = VoiceProfileStore(
        Path(settings.identity_path).parent / "voice_profile.bin", protector=_dpapi_protector()
    )
    profile = store.load()
    if profile is None and not arguments.auto:
        print("No voice profile. Run enroll-voice.bat first.")
        return 1
    if profile is not None:
        print(
            f"profile: {profile.phrases} phrases, {profile.quality} "
            f"(cohesion {profile.cohesion:.2f}; heard {', '.join(profile.covers) or 'one manner'})"
        )
    print(f"threshold: {settings.voice_speaker_threshold}")

    if arguments.stranger:
        return await check_stranger(settings, identity, store)

    chosen = [s for s in SCENARIOS if not arguments.only or s.name in arguments.only]
    if arguments.remaining:
        chosen = [s for s in chosen if s.name not in already_passed(arguments.auto)]
        if not chosen:
            print("Nothing left: every scenario has passed.")
            return 0
    runnable = [s for s in chosen if not arguments.auto or s.synthetic is not None]
    print(
        f"scenarios: {len(runnable)} of {len(chosen)}; "
        f"expect up to {2 * len(runnable)} model requests"
    )

    print("\nconnecting the agent ...")
    _, stop, agent_task = await start_agent(settings, identity)

    print("loading the voice models ...")
    voice = BackendVoice(settings, identity)
    watcher = Watcher()
    runtime = await build_runtime(
        settings=settings,
        identity=identity,
        store=store,
        models=MODELS,
        responder=voice,
        # Verification off for --auto and only for --auto: a synthesiser is not
        # the owner, and leaving it on would test the gate rather than the path.
        config=SessionConfig(verify_speaker=not arguments.auto, idle_timeout_s=45.0),
        on_event=watcher,
    )
    runtime.session.start()

    pump: asyncio.Task[None] | None = None
    if not arguments.auto:
        pump = asyncio.create_task(runtime.run(stop=stop))
        await asyncio.sleep(1.0)

    outcomes: list[Outcome] = []
    try:
        for index, scenario in enumerate(runnable):
            outcome = await run_scenario(
                scenario, runtime=runtime, watcher=watcher, voice=voice, auto=arguments.auto
            )
            outcomes.append(outcome)
            print(outcome.row())
            if outcome.note:
                print(f"        {outcome.note}")

            # Close the conversation unless the next scenario is meant to
            # continue it. Without this the second scenario rides on the first
            # one's open session and records "no wake word needed" as a pass
            # when nothing was tested at all. Mute is the public way to end a
            # conversation from outside, and unmuting starts a fresh one.
            following = runnable[index + 1 :]
            if not (following and following[0].continues):
                runtime.session.mute()
                runtime.session.unmute()
            else:
                # A continuation must not start while the previous reply is
                # still coming out of the speakers: pushed frames would be
                # treated as an interruption of it rather than as a command.
                await watcher.wait_for(("conversation_closed",), timeout=0.1)
                while runtime.session.states.state is VoiceState.SPEAKING:
                    await asyncio.sleep(0.1)
            await asyncio.sleep(1.0)
    finally:
        stop.set()
        if pump is not None:
            pump.cancel()
        runtime.stop()
        await asyncio.wait_for(agent_task, timeout=20)

    passed = sum(1 for outcome in outcomes if outcome.passed)
    print(f"\n{passed} of {len(outcomes)} scenarios passed")

    path = RESULTS / ("live-voice-auto.json" if arguments.auto else "live-voice.json")
    record = merge_results(path, outcomes, settings, auto=arguments.auto)
    print(f"written to {path.relative_to(REPO)}")
    report_remaining(record)
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
