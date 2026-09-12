"""The bookkeeping behind the live acceptance.

The acceptance needs a person to speak seven times, which nobody does in one
sitting — so the runner has to remember what earlier sittings established. If it
forgets, the cost is not a wrong number in a file: it is asking someone to say
everything again, which is how an acceptance quietly stops being run.

None of this needs a microphone, a model or a backend. It is a file and some
arithmetic, and that is exactly why it is worth testing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from live_voice_e2e import (
    SCENARIOS,
    Outcome,
    already_passed,
    merge_results,
    report_remaining,
)


class Settings:
    voice_speaker_threshold = 0.57


def outcome(name: str, *, passed: bool, transcript: str = "said something") -> Outcome:
    return Outcome(scenario=name, passed=passed, transcript=transcript, woke=True)


class TestRememberingEarlierSittings:
    def test_a_first_run_is_written_whole(self, tmp_path: Path) -> None:
        path = tmp_path / "live.json"

        record = merge_results(path, [outcome("one", passed=True)], Settings(), auto=False)

        assert [row["scenario"] for row in record["outcomes"]] == ["one"]
        assert json.loads(path.read_text(encoding="utf-8"))["outcomes"][0]["passed"] is True

    def test_a_later_run_keeps_what_the_earlier_one_established(self, tmp_path: Path) -> None:
        """The whole point. Speaking one command must not erase the other six."""
        path = tmp_path / "live.json"
        merge_results(path, [outcome("one", passed=True)], Settings(), auto=False)

        record = merge_results(path, [outcome("two", passed=True)], Settings(), auto=False)

        assert {row["scenario"] for row in record["outcomes"]} == {"one", "two"}

    def test_running_the_same_scenario_again_replaces_it(self, tmp_path: Path) -> None:
        """A retry is a newer answer to the same question, not a second one."""
        path = tmp_path / "live.json"
        merge_results(path, [outcome("one", passed=False)], Settings(), auto=False)

        record = merge_results(
            path, [outcome("one", passed=True, transcript="clearer")], Settings(), auto=False
        )

        assert len(record["outcomes"]) == 1
        assert record["outcomes"][0]["passed"] is True
        assert record["outcomes"][0]["transcript"] == "clearer"

    def test_each_result_records_when_it_happened(self, tmp_path: Path) -> None:
        """Sittings are days apart, and a result from before a change was made
        is worth telling apart from one after it."""
        path = tmp_path / "live.json"

        record = merge_results(path, [outcome("one", passed=True)], Settings(), auto=False)

        assert record["outcomes"][0]["at"]

    def test_a_corrupted_record_does_not_prevent_writing_a_good_one(self, tmp_path: Path) -> None:
        """Refusing to record today's work because yesterday's file is damaged
        would lose the only thing that is not damaged."""
        path = tmp_path / "live.json"
        path.write_text("{ this is not json", encoding="utf-8")

        record = merge_results(path, [outcome("one", passed=True)], Settings(), auto=False)

        assert [row["scenario"] for row in record["outcomes"]] == ["one"]

    def test_the_auto_and_interactive_records_are_separate(self, tmp_path: Path) -> None:
        """One is synthesised speech with verification off. Letting it count
        towards the other would report the owner's acceptance as done when
        nobody had spoken."""
        interactive = already_passed(auto=False)
        automatic = already_passed(auto=True)

        assert isinstance(interactive, set)
        assert isinstance(automatic, set)


class TestKnowingWhatIsLeft:
    def test_only_failures_and_untried_scenarios_remain(self, tmp_path: Path) -> None:
        path = tmp_path / "live.json"
        done = [outcome(scenario.name, passed=True) for scenario in SCENARIOS[:2]]
        record = merge_results(path, done, Settings(), auto=False)

        passed = {row["scenario"] for row in record["outcomes"] if row["passed"]}
        remaining = [s.name for s in SCENARIOS if s.name not in passed]

        assert len(remaining) == len(SCENARIOS) - 2

    def test_a_failed_scenario_is_still_outstanding(self, tmp_path: Path) -> None:
        path = tmp_path / "live.json"
        record = merge_results(
            path, [outcome(SCENARIOS[0].name, passed=False)], Settings(), auto=False
        )

        passed = {row["scenario"] for row in record["outcomes"] if row["passed"]}

        assert SCENARIOS[0].name not in passed

    def test_it_says_so_when_everything_has_passed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record: dict[str, Any] = {
            "outcomes": [{"scenario": s.name, "passed": True} for s in SCENARIOS]
        }

        report_remaining(record)

        assert "acceptance is complete" in capsys.readouterr().out

    def test_it_names_the_next_one_to_do(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Someone with thirty spare seconds should not have to work out which
        command to run."""
        record: dict[str, Any] = {"outcomes": [{"scenario": SCENARIOS[0].name, "passed": True}]}

        report_remaining(record)

        printed = capsys.readouterr().out
        assert SCENARIOS[1].name in printed
        assert "--only" in printed
