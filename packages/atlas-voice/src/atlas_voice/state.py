"""What the voice engine is doing, and who is allowed to change it.

The five states are the ones you asked to see:

    Listening → Thinking → Executing → Speaking → Listening
                                    ↘ Muted ↙

They exist for two different reasons and it is worth keeping them apart. One is
honesty: a microphone that is open should say so, and a person should never have
to guess whether the machine is listening. The other is mechanical — barge-in,
the idle timeout and the "model unreachable" path all key off the current state,
so it has to be a real object rather than a label on a tray icon.

**Mute is not a state like the others.** It is agent-owned and outranks
everything, exactly as SAFE MODE does in M2: anything may *enter* it, only the
person at the keyboard may leave it. A backend that could unmute a microphone
would be a defect no matter how the request was authenticated, so the transition
simply does not exist for a remote caller — this is enforced by construction
below, not by a policy check somewhere else that might be forgotten.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Actor", "IllegalTransitionError", "VoiceState", "VoiceStateMachine"]


class VoiceState(StrEnum):
    #: Microphone open, waiting for the wake word or the next turn.
    LISTENING = "listening"
    #: The command has been sent; waiting on the model.
    THINKING = "thinking"
    #: A tool is running on this machine.
    EXECUTING = "executing"
    #: Speaking a reply. The microphone stays open, for barge-in.
    SPEAKING = "speaking"
    #: The microphone is released. Nothing is heard.
    MUTED = "muted"
    #: Nothing is running: before start, and after shutdown.
    OFF = "off"


class Actor(StrEnum):
    """Who is asking for the change.

    The distinction is load-bearing. ``LOCAL`` is the person at this machine —
    the tray, the hotkey, the enrollment flow. ``ENGINE`` is the voice pipeline
    itself moving through a turn. ``REMOTE`` is anything that arrived over the
    network, and it may not touch the microphone.
    """

    LOCAL = "local"
    ENGINE = "engine"
    REMOTE = "remote"


class IllegalTransitionError(RuntimeError):
    """A transition that the state machine refuses to make."""


#: Legal moves for the engine driving an ordinary turn.
_ENGINE_TRANSITIONS: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.OFF: frozenset({VoiceState.LISTENING}),
    VoiceState.LISTENING: frozenset({VoiceState.THINKING, VoiceState.SPEAKING, VoiceState.OFF}),
    # Thinking may go straight to Speaking (a plain answer), to Executing (a
    # tool ran), or back to Listening (the turn failed and was announced).
    VoiceState.THINKING: frozenset(
        {VoiceState.EXECUTING, VoiceState.SPEAKING, VoiceState.LISTENING, VoiceState.OFF}
    ),
    VoiceState.EXECUTING: frozenset(
        {VoiceState.SPEAKING, VoiceState.THINKING, VoiceState.LISTENING, VoiceState.OFF}
    ),
    # Barge-in is Speaking → Listening, and it must always be available.
    VoiceState.SPEAKING: frozenset({VoiceState.LISTENING, VoiceState.THINKING, VoiceState.OFF}),
    # The engine cannot leave Muted. Only a local actor can.
    VoiceState.MUTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Transition:
    previous: VoiceState
    current: VoiceState
    actor: Actor


class VoiceStateMachine:
    """The current state, and the only legal ways to change it.

    Observers are notified on every accepted change; the tray icon and the
    on-screen indicator are observers, which is why they cannot drift out of
    sync with reality — there is no second copy of the state to update.
    """

    def __init__(self, *, on_change: Callable[[Transition], None] | None = None) -> None:
        self._state = VoiceState.OFF
        self._muted_from: VoiceState | None = None
        self._observers: list[Callable[[Transition], None]] = []
        if on_change is not None:
            self._observers.append(on_change)

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def is_muted(self) -> bool:
        return self._state is VoiceState.MUTED

    @property
    def microphone_is_open(self) -> bool:
        """Whether audio is being captured right now.

        Speaking counts: the microphone stays open so you can interrupt.
        """
        return self._state in (
            VoiceState.LISTENING,
            VoiceState.THINKING,
            VoiceState.EXECUTING,
            VoiceState.SPEAKING,
        )

    def observe(self, observer: Callable[[Transition], None]) -> None:
        self._observers.append(observer)

    # ---------------------------------------------------------------- changes

    def to(self, target: VoiceState, *, actor: Actor = Actor.ENGINE) -> None:
        """Move to ``target``, or raise :class:`IllegalTransitionError`.

        Muting and unmuting have their own methods; routing them through here
        would make it possible to reach ``MUTED`` without going past the
        ownership check.
        """
        if target is VoiceState.MUTED:
            raise IllegalTransitionError("use mute(); muting is not an ordinary transition")
        if self._state is VoiceState.MUTED:
            raise IllegalTransitionError(
                f"{actor} cannot move from muted to {target}; unmute is local-only"
            )
        if target not in _ENGINE_TRANSITIONS[self._state]:
            raise IllegalTransitionError(f"{self._state} → {target} is not a legal move")
        self._apply(target, actor)

    def mute(self, *, actor: Actor = Actor.LOCAL) -> None:
        """Release the microphone.

        Any actor may mute, including a remote one. Silencing a microphone is
        never the dangerous direction, and a remote kill path is worth having.
        """
        if self._state is VoiceState.MUTED:
            return
        self._muted_from = self._state
        self._apply(VoiceState.MUTED, actor)

    def unmute(self, *, actor: Actor = Actor.LOCAL) -> None:
        """Re-open the microphone. **Local actors only.**

        This is the whole point of the class. A backend, a signed command, or a
        model that has been talked into it cannot open your microphone; the
        capability does not exist on this side of the wire.
        """
        if actor is not Actor.LOCAL:
            raise IllegalTransitionError(f"{actor} cannot unmute; only the local user can")
        if self._state is not VoiceState.MUTED:
            return
        resumed = (
            self._muted_from
            if self._muted_from in (VoiceState.LISTENING, VoiceState.OFF)
            else VoiceState.LISTENING
        )
        self._muted_from = None
        self._apply(resumed or VoiceState.LISTENING, actor)

    def _apply(self, target: VoiceState, actor: Actor) -> None:
        transition = Transition(previous=self._state, current=target, actor=actor)
        self._state = target
        for observer in self._observers:
            observer(transition)
