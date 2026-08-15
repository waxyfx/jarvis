"""The state machine, and the one invariant that matters most in it.

Mute is agent-owned. Anything may silence the microphone; only the person at
this machine may open it again. These tests are the enforcement — the property
is easy to state, easy to agree with, and easy to lose in a refactor.
"""

from __future__ import annotations

import pytest

from atlas_voice.state import (
    Actor,
    IllegalTransitionError,
    Transition,
    VoiceState,
    VoiceStateMachine,
)


def started() -> VoiceStateMachine:
    machine = VoiceStateMachine()
    machine.to(VoiceState.LISTENING)
    return machine


class TestOrdinaryTurn:
    def test_a_turn_runs_listening_thinking_executing_speaking(self) -> None:
        machine = started()

        machine.to(VoiceState.THINKING)
        machine.to(VoiceState.EXECUTING)
        machine.to(VoiceState.SPEAKING)
        machine.to(VoiceState.LISTENING)

        assert machine.state is VoiceState.LISTENING

    def test_a_plain_answer_skips_executing(self) -> None:
        machine = started()

        machine.to(VoiceState.THINKING)
        machine.to(VoiceState.SPEAKING)

        assert machine.state is VoiceState.SPEAKING

    def test_a_failed_turn_returns_to_listening(self) -> None:
        """No model, no network: the engine must not be left in Thinking."""
        machine = started()
        machine.to(VoiceState.THINKING)

        machine.to(VoiceState.LISTENING)

        assert machine.state is VoiceState.LISTENING

    def test_barge_in_is_always_available_while_speaking(self) -> None:
        machine = started()
        machine.to(VoiceState.THINKING)
        machine.to(VoiceState.SPEAKING)

        machine.to(VoiceState.LISTENING)

        assert machine.state is VoiceState.LISTENING

    def test_nonsense_transitions_are_refused(self) -> None:
        machine = started()

        with pytest.raises(IllegalTransitionError):
            machine.to(VoiceState.EXECUTING)

    def test_the_microphone_is_open_throughout_a_turn(self) -> None:
        """Including while speaking — that is what makes interruption possible."""
        machine = started()
        for state in (VoiceState.THINKING, VoiceState.EXECUTING, VoiceState.SPEAKING):
            machine.to(state)
            assert machine.microphone_is_open


class TestMuteIsLocallyOwned:
    def test_a_remote_actor_cannot_unmute(self) -> None:
        machine = started()
        machine.mute()

        with pytest.raises(IllegalTransitionError, match="only the local user"):
            machine.unmute(actor=Actor.REMOTE)

        assert machine.is_muted

    def test_the_engine_itself_cannot_unmute(self) -> None:
        """Not even ATLAS's own pipeline. There is no automatic path back."""
        machine = started()
        machine.mute()

        with pytest.raises(IllegalTransitionError):
            machine.unmute(actor=Actor.ENGINE)

        assert machine.is_muted

    def test_the_local_user_can_unmute(self) -> None:
        machine = started()
        machine.mute()

        machine.unmute(actor=Actor.LOCAL)

        assert not machine.is_muted
        assert machine.state is VoiceState.LISTENING

    def test_a_remote_actor_may_mute(self) -> None:
        """Silencing is never the dangerous direction."""
        machine = started()

        machine.mute(actor=Actor.REMOTE)

        assert machine.is_muted

    def test_muted_blocks_every_ordinary_transition(self) -> None:
        machine = started()
        machine.mute()

        for target in (VoiceState.LISTENING, VoiceState.THINKING, VoiceState.SPEAKING):
            with pytest.raises(IllegalTransitionError):
                machine.to(target, actor=Actor.REMOTE)

    def test_muting_cannot_be_reached_through_the_ordinary_path(self) -> None:
        """Otherwise a caller could arrive at Muted without the ownership check."""
        machine = started()

        with pytest.raises(IllegalTransitionError, match="use mute"):
            machine.to(VoiceState.MUTED)

    def test_the_microphone_is_not_open_while_muted(self) -> None:
        machine = started()
        machine.mute()

        assert not machine.microphone_is_open

    def test_muting_twice_is_harmless(self) -> None:
        machine = started()
        machine.mute()
        machine.mute()

        assert machine.is_muted

    def test_unmuting_when_not_muted_is_harmless(self) -> None:
        machine = started()
        machine.unmute()

        assert machine.state is VoiceState.LISTENING

    def test_muting_mid_turn_returns_to_listening_not_mid_turn(self) -> None:
        """Resuming into Executing would claim a tool is running when none is."""
        machine = started()
        machine.to(VoiceState.THINKING)
        machine.to(VoiceState.EXECUTING)

        machine.mute()
        machine.unmute()

        assert machine.state is VoiceState.LISTENING


class TestObservers:
    def test_every_change_is_reported(self) -> None:
        seen: list[Transition] = []
        machine = VoiceStateMachine(on_change=seen.append)

        machine.to(VoiceState.LISTENING)
        machine.to(VoiceState.THINKING)

        assert [t.current for t in seen] == [VoiceState.LISTENING, VoiceState.THINKING]
        assert seen[1].previous is VoiceState.LISTENING

    def test_the_actor_is_reported_so_the_trail_can_say_who_muted(self) -> None:
        seen: list[Transition] = []
        machine = started()
        machine.observe(seen.append)

        machine.mute(actor=Actor.REMOTE)

        assert seen[-1].actor is Actor.REMOTE
        assert seen[-1].current is VoiceState.MUTED

    def test_a_refused_transition_notifies_nobody(self) -> None:
        seen: list[Transition] = []
        machine = started()
        machine.observe(seen.append)

        with pytest.raises(IllegalTransitionError):
            machine.to(VoiceState.EXECUTING)

        assert seen == []
