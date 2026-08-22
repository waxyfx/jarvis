"""Transcript normalisation: the fix for code-switching Whisper cannot do.

No models, no audio. The table is deterministic, so the tests state exactly
what goes in and what must come out.
"""

from __future__ import annotations

import pytest

from atlas_voice.normalize import Normaliser, TranscriptRules


class TestCodeSwitching:
    @pytest.mark.parametrize(
        ("heard", "expected"),
        [
            ("открой вс код", "открой VS Code"),
            ("открой вэс код и покажи память", "открой VS Code и покажи память"),
            ("запусти ви эс код", "запусти VS Code"),
            ("открой хром", "открой Chrome"),
            ("закрой блокнот", "закрой Notepad"),
            ("открой телеграм", "открой Telegram"),
        ],
    )
    def test_transliterated_product_names_are_restored(self, heard: str, expected: str) -> None:
        """Whisper decodes English inside Russian through Russian phonotactics.

        The acoustics were fine; the spelling is what the tool layer cannot
        match.
        """
        assert Normaliser().apply(heard)[0] == expected

    def test_the_longest_rule_wins(self) -> None:
        """«ви эс код» must not be half-rewritten by a shorter overlapping rule."""
        assert Normaliser().apply("ви эс код")[0] == "VS Code"

    def test_it_reports_what_it_changed(self) -> None:
        _, applied = Normaliser().apply("открой вс код и хром")

        assert any("VS Code" in entry for entry in applied)
        assert any("Chrome" in entry for entry in applied)


class TestItLeavesEverythingElseAlone:
    @pytest.mark.parametrize(
        "text",
        [
            "я забыл шарф в париже",
            "дарвин был натуралистом",
            "в сервисе произошла ошибка",
            "мне нравится джазовый концерт",
            "open chrome and show memory usage",
        ],
    )
    def test_unrelated_speech_survives(self, text: str) -> None:
        assert Normaliser().apply(text)[0] == text

    def test_it_matches_whole_words_only(self) -> None:
        """«хромота» contains «хром» and is not a browser."""
        assert Normaliser().apply("у него хромота")[0] == "у него хромота"

    def test_english_already_spelled_correctly_is_untouched(self) -> None:
        assert Normaliser().apply("open VS Code")[0] == "open VS Code"


class TestFiller:
    def test_hesitation_is_dropped(self) -> None:
        assert Normaliser().apply("ээ открой хром")[0] == "открой Chrome"

    def test_punctuation_does_not_drift(self) -> None:
        assert Normaliser().apply("открой хром, пожалуйста")[0] == "открой Chrome, пожалуйста"


class TestItCannotInventTargets:
    def test_rules_pointing_outside_the_catalogue_are_dropped(self) -> None:
        """A table that can rewrite speech into a name nothing recognises is
        worse than no table: the failure changes from "did not understand" to
        "understood something that does not exist"."""
        rules = TranscriptRules(aliases={"хром": "Chrome", "фотошоп": "Photoshop"})
        normaliser = Normaliser(rules, known_names=frozenset({"Chrome", "Notepad"}))

        assert normaliser.apply("открой хром")[0] == "открой Chrome"
        assert normaliser.apply("открой фотошоп")[0] == "открой фотошоп"

    def test_without_a_catalogue_every_rule_applies(self) -> None:
        rules = TranscriptRules(aliases={"фотошоп": "Photoshop"})

        assert Normaliser(rules).apply("открой фотошоп")[0] == "открой Photoshop"


class TestEdges:
    def test_empty_text_is_fine(self) -> None:
        assert Normaliser().apply("")[0] == ""

    def test_whitespace_is_collapsed(self) -> None:
        assert Normaliser().apply("  открой   хром  ")[0] == "открой Chrome"

    def test_case_is_ignored_when_matching(self) -> None:
        assert Normaliser().apply("Открой ХРОМ")[0] == "Открой Chrome"
