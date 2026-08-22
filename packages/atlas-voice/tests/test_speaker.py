"""Speaker verification against the real embedding model.

Synthetic voices, so these are not the final word — voices from one synthesiser
share a great deal, and the honest calibration waits for recordings of the
actual owner. What can be established here is that the model separates speakers
at all, that a profile survives storage, and that a mismatched profile is
refused rather than silently compared.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from atlas_voice.engines.speaker import SherpaSpeaker
from atlas_voice.enrollment import EnrollmentSession
from atlas_voice.profile import VoiceProfile, VoiceProfileStore, now, plaintext_protector
from atlas_voice.providers import SpeakerProvider, VoiceEngineError
from voicefixtures import MODELS, PIPER_MULTI, _synthesise, requires_piper

MODEL = MODELS / "speaker" / "eres2net_base_sv.onnx"

requires_speaker = pytest.mark.skipif(
    not MODEL.is_file(),
    reason="run scripts/fetch_voice_models.ps1 to download the speaker embedding model",
)

pytestmark = [requires_speaker, requires_piper]

#: Long enough to characterise a voice, and nothing worth overhearing.
LINES = (
    "Good morning, Jarvis, it is a quiet day today",
    "Open the second document and check the totals please",
    "The meeting has been moved to Thursday afternoon",
    "Remind me to call back before six o'clock",
    "Show me how much disk space is left on this machine",
)


def say(text: str, speaker: int) -> np.ndarray:
    return _synthesise(PIPER_MULTI, text, speaker_id=speaker)


def speaker_engine(tmp_path: Path, **kwargs: object) -> SherpaSpeaker:
    store = VoiceProfileStore(tmp_path / "voice.bin", protector=plaintext_protector())
    return SherpaSpeaker(MODEL, store=store, **kwargs)  # type: ignore[arg-type]


def enrol(engine: SherpaSpeaker, speaker: int, *, count: int = 5) -> VoiceProfile:
    session = EnrollmentSession(embed=engine, store=engine._store, phrase_count=count)  # type: ignore[arg-type]
    for line in LINES[:count]:
        session.add(say(line, speaker))
    profile = session.finish()
    engine.forget()
    return profile


class TestItSeparatesVoices:
    def test_the_owner_is_recognised_on_an_unseen_phrase(self, tmp_path: Path) -> None:
        engine = speaker_engine(tmp_path)
        enrol(engine, 11)

        result = engine.verify(say("Jarvis, close Notepad and open Chrome", 11))

        assert result.accepted, f"owner scored {result.score:.3f}"

    @pytest.mark.parametrize("stranger", [200, 640, 42])
    def test_other_voices_score_lower_than_the_owner(self, tmp_path: Path, stranger: int) -> None:
        """A rate, not a guarantee: synthetic voices overlap, and the threshold
        is calibrated against real recordings, not these."""
        engine = speaker_engine(tmp_path)
        enrol(engine, 11)
        line = "Jarvis, close Notepad and open Chrome"

        owner = engine.verify(say(line, 11)).score
        other = engine.verify(say(line, stranger)).score

        assert other < owner, f"stranger {stranger} scored {other:.3f} vs owner {owner:.3f}"

    def test_the_score_and_threshold_are_both_reported(self, tmp_path: Path) -> None:
        engine = speaker_engine(tmp_path)
        enrol(engine, 11)

        result = engine.verify(say(LINES[0], 11))

        assert 0.0 <= result.score <= 1.0
        assert result.threshold > 0
        assert result.margin == pytest.approx(result.score - result.threshold)


class TestContract:
    def test_it_satisfies_the_provider_protocol(self, tmp_path: Path) -> None:
        assert isinstance(speaker_engine(tmp_path), SpeakerProvider)

    def test_verifying_without_a_profile_refuses(self, tmp_path: Path) -> None:
        """Silently accepting everyone would be the worst possible default."""
        engine = speaker_engine(tmp_path)

        with pytest.raises(VoiceEngineError, match="no voice profile"):
            engine.verify(say(LINES[0], 11))

    def test_too_little_audio_is_refused(self, tmp_path: Path) -> None:
        engine = speaker_engine(tmp_path)

        with pytest.raises(VoiceEngineError, match="at least"):
            engine.embed(np.zeros(1000, dtype=np.float32))

    def test_a_profile_from_another_model_is_refused(self, tmp_path: Path) -> None:
        """The vectors would still compare and the number would mean nothing."""
        engine = speaker_engine(tmp_path)
        engine._store.save(  # type: ignore[union-attr]
            VoiceProfile(
                embedding=np.ones(64, dtype=np.float32) / 8,
                phrases=5,
                cohesion=0.9,
                created_at=now(),
                model="something-else",
                dimensions=64,
            )
        )
        engine.forget()

        with pytest.raises(VoiceEngineError, match="re-enrollment"):
            engine.verify(say(LINES[0], 11))

    def test_a_missing_model_says_how_to_get_it(self, tmp_path: Path) -> None:
        with pytest.raises(VoiceEngineError, match="fetch_voice_models"):
            SherpaSpeaker(tmp_path / "absent.onnx")

    def test_an_impossible_threshold_is_refused(self, tmp_path: Path) -> None:
        for bad in (0.0, 1.0, -0.5):
            with pytest.raises(ValueError):
                speaker_engine(tmp_path, threshold=bad)

    def test_embeddings_are_unit_length(self, tmp_path: Path) -> None:
        vector = speaker_engine(tmp_path).embed(say(LINES[0], 11))

        assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-5)


class TestReplayIsNotDefendedAgainst:
    def test_the_same_audio_scores_as_the_owner(self, tmp_path: Path) -> None:
        """Recorded and replayed audio passes, and that is expected.

        The model compares timbre; it cannot tell a throat from a loudspeaker.
        Recorded here so the limitation is a measured fact in the suite rather
        than a caveat in a document nobody rereads — and so that anyone tempted
        to treat verification as authentication meets this test first.
        """
        engine = speaker_engine(tmp_path)
        enrol(engine, 11)
        recording = say("Jarvis, close Notepad and open Chrome", 11)

        assert engine.verify(recording).accepted
        # A "replay" is byte-identical audio played again: nothing distinguishes
        # it, because nothing here is looking.
        assert engine.verify(recording.copy()).accepted
