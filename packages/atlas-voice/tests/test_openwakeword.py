"""The wake-word runtime, validated against a model known to work.

There is no "Atlas" model yet — it has to be trained. That is exactly why these
tests use the published ``hey_jarvis`` classifier: a runtime is either correct
or it is not, independently of which word it looks for, and proving it correct
first means a later training run can be judged on its own merits instead of
being blamed for a feature-pipeline bug.

The scoring arithmetic has no error path. Get the mel scaling wrong and the
model returns 0.061 for the phrase and 0.057 for unrelated speech: ordered
correctly, useless in practice, and silent about it.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas_voice.engines.openwakeword import OpenWakeWord
from atlas_voice.providers import VoiceEngineError
from voicefixtures import MODELS, requires_piper, speech

OWW = MODELS / "oww"
MEL = OWW / "melspectrogram.onnx"
EMBED = OWW / "embedding_model.onnx"
JARVIS = OWW / "hey_jarvis_v0.1.onnx"

requires_oww = pytest.mark.skipif(
    not (MEL.is_file() and EMBED.is_file() and JARVIS.is_file()),
    reason="run scripts/fetch_voice_models.ps1 to download the openWakeWord models",
)

pytestmark = [requires_oww, requires_piper]


def spotter(**kwargs: object) -> OpenWakeWord:
    options: dict[str, object] = {
        "melspectrogram_path": MEL,
        "embedding_path": EMBED,
        "label": "hey_jarvis",
    }
    return OpenWakeWord(JARVIS, **(options | kwargs))  # type: ignore[arg-type]


def padded(name: str) -> np.ndarray:
    """Speech with a second of silence either side, as a microphone delivers it."""
    quiet = np.zeros(16_000, dtype=np.float32)
    return np.concatenate([quiet, speech(name), quiet])


@pytest.fixture(scope="module")
def jarvis_audio() -> np.ndarray:
    from voicefixtures import PIPER_EN, _synthesise

    quiet = np.zeros(16_000, dtype=np.float32)
    return np.concatenate([quiet, _synthesise(PIPER_EN, "Hey Jarvis."), quiet])


class TestItDetectsItsWord:
    def test_the_wake_phrase_fires(self, jarvis_audio: np.ndarray) -> None:
        assert spotter().push(jarvis_audio) is not None

    def test_it_fires_on_the_first_confident_window_not_the_best_one(
        self, jarvis_audio: np.ndarray
    ) -> None:
        """Latency is the point: "Yes, sir?" should not wait for the peak.

        The first window over the threshold scores about 0.80 and the run peaks
        near 0.998 a few windows later. Firing at the peak would cost roughly
        300 ms for no gain in certainty.
        """
        detection = spotter().push(jarvis_audio)
        peak = max(spotter().scores_for(jarvis_audio))

        assert detection is not None
        assert detection.score > 0.5
        assert detection.label == "hey_jarvis"
        assert peak > 0.9
        assert detection.score < peak

    def test_the_detection_is_timed_from_the_stream(self, jarvis_audio: np.ndarray) -> None:
        detection = spotter().push(jarvis_audio)

        assert detection is not None
        # Speech starts a second in; the word cannot be detected before it.
        assert 0.9 < detection.at < 3.5


class TestItIgnoresEverythingElse:
    @pytest.mark.parametrize(
        "phrase", ["en_command", "ru_command", "en_near_miss", "ru_near_miss", "en_wake"]
    )
    def test_other_speech_does_not_fire(self, phrase: str) -> None:
        assert spotter().push(padded(phrase)) is None

    def test_silence_does_not_fire(self) -> None:
        assert spotter().push(np.zeros(16_000 * 3, dtype=np.float32)) is None

    def test_noise_does_not_fire(self) -> None:
        rng = np.random.default_rng(20260815)
        noise = (rng.standard_normal(16_000 * 3) * 0.05).astype(np.float32)

        assert spotter().push(noise) is None


class TestStreaming:
    def test_streaming_matches_a_single_call(self, jarvis_audio: np.ndarray) -> None:
        """The frames a chunk contributes depend on what came before it.

        Fed in isolation a chunk yields five mel frames where continuous audio
        owes eight, so a detector built the obvious way drifts away from the
        behaviour it was tuned against.
        """
        whole = spotter()
        whole.push(jarvis_audio)

        piecemeal = spotter()
        fired = None
        for offset in range(0, len(jarvis_audio), 512):
            detection = piecemeal.push(jarvis_audio[offset : offset + 512])
            fired = fired or detection

        assert fired is not None
        assert piecemeal.last_score == pytest.approx(whole.last_score, abs=1e-4)

    def test_odd_chunk_sizes_are_handled(self, jarvis_audio: np.ndarray) -> None:
        """Audio arrives in whatever size the device hands over."""
        detector = spotter()
        fired = None
        for offset in range(0, len(jarvis_audio), 333):
            fired = fired or detector.push(jarvis_audio[offset : offset + 333])

        assert fired is not None

    def test_one_word_fires_once(self, jarvis_audio: np.ndarray) -> None:
        """Overlapping windows mean a spoken word produces a run of high scores.

        Without the refractory period the session opens, closes and reopens
        underneath the speaker.
        """
        detector = spotter()
        detections = [
            detection
            for offset in range(0, len(jarvis_audio), 512)
            if (detection := detector.push(jarvis_audio[offset : offset + 512])) is not None
        ]

        assert len(detections) == 1

    def test_two_separated_words_fire_twice(self, jarvis_audio: np.ndarray) -> None:
        gap = np.zeros(16_000 * 2, dtype=np.float32)
        twice = np.concatenate([jarvis_audio, gap, jarvis_audio])

        detector = spotter()
        detections = [
            detection
            for offset in range(0, len(twice), 512)
            if (detection := detector.push(twice[offset : offset + 512])) is not None
        ]

        assert len(detections) == 2


class TestContract:
    def test_a_missing_model_says_how_to_get_it(self) -> None:
        with pytest.raises(VoiceEngineError, match="fetch_voice_models"):
            OpenWakeWord("nope.onnx", melspectrogram_path=MEL, embedding_path=EMBED)

    def test_an_impossible_threshold_is_refused(self) -> None:
        for bad in (0.0, 1.0, -1.0):
            with pytest.raises(ValueError):
                spotter(threshold=bad)

    def test_a_high_threshold_suppresses_the_detection(self, jarvis_audio: np.ndarray) -> None:
        assert spotter(threshold=0.999999).push(jarvis_audio) is None

    def test_reset_clears_buffered_audio(self, jarvis_audio: np.ndarray) -> None:
        detector = spotter()
        detector.push(jarvis_audio[: len(jarvis_audio) // 2])
        detector.reset()

        assert detector.push(jarvis_audio[len(jarvis_audio) // 2 :]) is None

    def test_scores_for_reports_every_window(self, jarvis_audio: np.ndarray) -> None:
        """Used for measuring false accepts, so it must not hide any window."""
        scores = np.array(spotter().scores_for(jarvis_audio))

        assert len(scores) > 10
        assert scores.max() > 0.9
        # The classifier window is about 1.3 s wide, so many windows overlap a
        # 1 s phrase — a low *fraction* would be the wrong thing to demand.
        # What must hold is that the recording ends quiet: once the phrase has
        # passed, the score returns to the floor rather than staying latched.
        assert scores[-5:].max() < 0.01
