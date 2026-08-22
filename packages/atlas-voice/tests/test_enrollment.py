"""Voice enrollment: the flow, the rejections, and the storage.

The embedder is faked here on purpose. What these tests are about is whether a
bad take is caught while the person is still sitting there, whether disagreeing
takes are noticed, and whether the recordings actually go away — none of which
is a property of any particular model.

The model-backed half lives in test_speaker.py, and the final word on whether
enrollment works belongs to recordings of the actual owner.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.enrollment import EnrollmentSession, TakeVerdict
from atlas_voice.profile import VoiceProfileStore, plaintext_protector
from atlas_voice.providers import VoiceEngineError


class FakeEmbedder:
    """Returns a stable vector per 'voice', so agreement is controllable."""

    model = "fake-embedder"

    def __init__(self) -> None:
        self.voice = 0
        self.calls = 0

    def embed(self, samples: np.ndarray) -> np.ndarray:
        self.calls += 1
        rng = np.random.default_rng(self.voice)
        base = rng.standard_normal(64).astype(np.float32)
        # A little jitter, so takes agree closely without being identical.
        jitter = np.random.default_rng(self.calls).standard_normal(64).astype(np.float32) * 0.05
        vector = base + jitter
        return vector / np.linalg.norm(vector)


def speech(seconds: float = 2.0, *, level: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE), dtype=np.float32) / SAMPLE_RATE
    return (level * np.sin(2 * np.pi * 140 * t)).astype(np.float32)


def session(tmp_path: Path, **kwargs: object) -> EnrollmentSession:
    store = VoiceProfileStore(tmp_path / "voice.bin", protector=plaintext_protector())
    return EnrollmentSession(embed=FakeEmbedder(), store=store, **kwargs)  # type: ignore[arg-type]


class TestTheScript:
    def test_it_asks_for_both_languages(self, tmp_path: Path) -> None:
        script = session(tmp_path).script()

        languages = {language for language, _ in script}
        assert languages == {Language.EN, Language.RU}

    def test_it_asks_for_the_configured_number(self, tmp_path: Path) -> None:
        assert len(session(tmp_path, phrase_count=8).script()) == 8

    def test_too_few_phrases_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="four phrases"):
            session(tmp_path, phrase_count=2)


class TestJudgingTakes:
    def test_a_good_take_is_accepted(self, tmp_path: Path) -> None:
        assert session(tmp_path).judge(speech()).accepted

    def test_a_clipped_take_is_rejected_with_advice(self, tmp_path: Path) -> None:
        """A clipped waveform still produces a confident embedding, and
        averaging it in poisons the profile weeks before anyone notices."""
        loud = np.clip(speech(level=2.0), -1.0, 1.0)

        verdict = session(tmp_path).judge(loud)

        assert not verdict.accepted
        assert verdict.reason == "clipped"
        assert "move back" in verdict.advice

    def test_a_quiet_take_is_rejected(self, tmp_path: Path) -> None:
        verdict = session(tmp_path).judge(speech(level=0.001))

        assert not verdict.accepted
        assert verdict.reason == "too_quiet"
        assert verdict.advice

    def test_a_short_take_is_rejected(self, tmp_path: Path) -> None:
        verdict = session(tmp_path).judge(speech(seconds=0.4))

        assert not verdict.accepted
        assert verdict.reason == "too_short"

    def test_a_rejected_take_is_not_embedded(self, tmp_path: Path) -> None:
        enrol = session(tmp_path)

        enrol.add(speech(seconds=0.2))

        assert enrol.collected == 0
        assert enrol.embed.calls == 0  # type: ignore[attr-defined]


class TestBuildingTheProfile:
    def test_a_profile_is_saved_and_reports_its_quality(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=6)
        for _ in range(6):
            enrol.add(speech())

        profile = enrol.finish()

        assert profile.phrases == 6
        assert profile.quality in ("strong", "usable", "weak")
        assert enrol.store.exists()

    def test_too_few_usable_takes_refuses_rather_than_guessing(self, tmp_path: Path) -> None:
        enrol = session(tmp_path)
        for _ in range(3):
            enrol.add(speech())

        with pytest.raises(VoiceEngineError, match="at least four"):
            enrol.finish()

    def test_a_take_from_another_voice_is_spotted(self, tmp_path: Path) -> None:
        """One phrase recorded by someone else, or from across the room."""
        enrol = session(tmp_path, phrase_count=6)
        for _ in range(5):
            enrol.add(speech())
        enrol.embed.voice = 99  # type: ignore[attr-defined]
        enrol.add(speech())

        assert enrol.outliers() == [5]

    def test_outliers_are_dropped_from_the_profile(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=6)
        for _ in range(5):
            enrol.add(speech())
        enrol.embed.voice = 99  # type: ignore[attr-defined]
        enrol.add(speech())

        profile = enrol.finish()

        assert profile.phrases == 5
        assert profile.cohesion > 0.9

    def test_dropping_never_leaves_too_few(self, tmp_path: Path) -> None:
        """Discarding most of the takes would produce a profile from scraps."""
        enrol = session(tmp_path, phrase_count=5)
        enrol.add(speech())
        for index in range(4):
            enrol.embed.voice = 50 + index  # type: ignore[attr-defined]
            enrol.add(speech())

        profile = enrol.finish()

        assert profile.phrases == 5


class TestRecordings:
    def test_they_are_not_kept_by_default(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=4, recordings_dir=tmp_path / "takes")
        for _ in range(4):
            enrol.add(speech())

        enrol.finish()

        assert list((tmp_path / "takes").glob("*.wav")) == []

    def test_keeping_them_is_deliberate(self, tmp_path: Path) -> None:
        enrol = session(
            tmp_path, phrase_count=4, keep_recordings=True, recordings_dir=tmp_path / "takes"
        )
        for _ in range(4):
            enrol.add(speech())

        enrol.finish()

        assert len(list((tmp_path / "takes").glob("*.wav"))) == 4

    def test_abandoning_leaves_nothing_behind(self, tmp_path: Path) -> None:
        enrol = session(
            tmp_path, phrase_count=4, keep_recordings=True, recordings_dir=tmp_path / "takes"
        )
        for _ in range(3):
            enrol.add(speech())

        enrol.abandon()

        assert list((tmp_path / "takes").glob("*.wav")) == []
        assert enrol.collected == 0
        assert not enrol.store.exists()


class TestTheFinalCheck:
    def test_a_fresh_utterance_scores_against_the_profile(self, tmp_path: Path) -> None:
        """Enrollment that does not end with a check is enrollment nobody
        trusts."""
        enrol = session(tmp_path, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        enrol.finish()

        assert enrol.test(speech()) > 0.9

    def test_a_different_voice_scores_low(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        enrol.finish()
        enrol.embed.voice = 99  # type: ignore[attr-defined]

        assert enrol.test(speech()) < 0.5

    def test_testing_without_a_profile_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(VoiceEngineError, match="no profile"):
            session(tmp_path).test(speech())


class TestStorage:
    def test_a_profile_survives_a_round_trip(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        saved = enrol.finish()

        loaded = enrol.store.load()

        assert loaded is not None
        assert np.allclose(loaded.embedding, saved.embedding, atol=1e-6)
        assert loaded.dimensions == saved.dimensions

    def test_protection_is_actually_applied(self, tmp_path: Path) -> None:
        """The bytes on disk must not be the plain payload.

        Asserted against a protector that really transforms, because a store
        that quietly wrote plaintext would pass every round-trip test.
        """
        marker = b"\x01\x02"
        store = VoiceProfileStore(
            tmp_path / "voice.bin",
            protector=(lambda data: marker + data[::-1], lambda blob: blob[len(marker) :][::-1]),
        )
        enrol = EnrollmentSession(embed=FakeEmbedder(), store=store, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        enrol.finish()

        raw = (tmp_path / "voice.bin").read_bytes()
        assert raw.startswith(marker)
        assert b'"embedding"' not in raw
        assert store.load() is not None

    def test_deleting_removes_it(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        enrol.finish()

        assert enrol.store.delete() is True
        assert enrol.store.load() is None
        assert enrol.store.delete() is False

    def test_no_partial_file_is_left_behind(self, tmp_path: Path) -> None:
        enrol = session(tmp_path, phrase_count=4)
        for _ in range(4):
            enrol.add(speech())
        enrol.finish()

        assert list(tmp_path.glob("*.partial")) == []


def test_the_verdict_speaks_plainly() -> None:
    assert TakeVerdict(False, "too_quiet").advice
    assert TakeVerdict(True).advice == ""
