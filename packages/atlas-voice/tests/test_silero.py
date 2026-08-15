"""Silero VAD against real speech.

The positive cases are the point. This module exists in the shape it does
because the first implementation passed every negative test while being
completely blind: fed the wrong input shape, the model scored 0.003 on speech
and 0.004 on silence. Both numbers are "not speech", and a suite that only
checked silence would have shipped it.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE, frames_from_array
from atlas_voice.engines.silero import SileroVAD
from atlas_voice.providers import VoiceEngineError
from atlas_voice.segmenter import SpeechSegmenter
from voicefixtures import SILERO, requires_piper, requires_silero

pytestmark = requires_silero


def vad(**kwargs: object) -> SileroVAD:
    return SileroVAD(SILERO, **kwargs)  # type: ignore[arg-type]


def scores(detector: SileroVAD, audio: np.ndarray) -> np.ndarray:
    return np.array(
        [
            detector.probability(audio[offset : offset + FRAME_SAMPLES])
            for offset in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
        ]
    )


class TestItHearsSpeech:
    @requires_piper
    @pytest.mark.parametrize("phrase", ["en_command", "ru_command"])
    def test_most_frames_of_real_speech_are_detected(self, speech_fixture, phrase) -> None:  # type: ignore[no-untyped-def]
        detected = scores(vad(), speech_fixture(phrase))

        # Utterances contain pauses between words, so demanding every frame
        # would be wrong. Well over half is the honest bar, and the measured
        # figure is around 90%.
        assert (detected > 0.5).mean() > 0.6, f"only {(detected > 0.5).mean():.0%} of frames"

    @requires_piper
    def test_speech_scores_far_above_silence(self, speech_fixture, silence) -> None:  # type: ignore[no-untyped-def]
        """The gap is what makes a threshold meaningful.

        When the model was fed the wrong shape, both sat near 0.003 — ordered
        correctly, but with no gap to put a threshold in.
        """
        speech_mean = scores(vad(), speech_fixture("en_command")).mean()
        silence_mean = scores(vad(), silence).mean()

        assert speech_mean > 0.5
        assert silence_mean < 0.05
        assert speech_mean - silence_mean > 0.4


class TestItIgnoresEverythingElse:
    def test_silence_is_not_speech(self, silence: np.ndarray) -> None:
        assert (scores(vad(), silence) > 0.5).mean() == 0.0

    def test_room_noise_is_not_speech(self, room_noise: np.ndarray) -> None:
        """Otherwise a fan opens a turn every few seconds."""
        assert (scores(vad(), room_noise) > 0.5).mean() < 0.02

    def test_a_pure_tone_is_not_speech(self) -> None:
        t = np.arange(SAMPLE_RATE * 2, dtype=np.float32) / SAMPLE_RATE
        tone = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

        assert (scores(vad(), tone) > 0.5).mean() == 0.0

    def test_mains_hum_is_not_speech(self) -> None:
        t = np.arange(SAMPLE_RATE * 2, dtype=np.float32) / SAMPLE_RATE
        hum = (0.2 * np.sin(2 * np.pi * 50 * t)).astype(np.float32)

        assert (scores(vad(), hum) > 0.5).mean() == 0.0


class TestContract:
    def test_the_wrong_frame_size_is_refused(self) -> None:
        """Silero silently degrades on a short window; better to raise."""
        with pytest.raises(ValueError, match="512"):
            vad().probability(np.zeros(256, dtype=np.float32))

    def test_a_missing_model_says_how_to_get_it(self) -> None:
        with pytest.raises(VoiceEngineError, match="fetch_voice_models"):
            SileroVAD("does-not-exist.onnx")

    def test_an_impossible_threshold_is_refused(self) -> None:
        for bad in (0.0, 1.0, -0.5, 2.0):
            with pytest.raises(ValueError):
                vad(threshold=bad)

    @requires_piper
    def test_reset_returns_it_to_a_clean_state(self, speech_fixture) -> None:  # type: ignore[no-untyped-def]
        detector = vad()
        audio = speech_fixture("en_command")
        first = scores(detector, audio)

        detector.reset()
        second = scores(detector, audio)

        assert np.allclose(first, second, atol=1e-5)

    @requires_piper
    def test_the_threshold_moves_the_decision(self, speech_fixture) -> None:  # type: ignore[no-untyped-def]
        audio = speech_fixture("ru_command")
        strict = vad(threshold=0.95)
        lenient = vad(threshold=0.05)

        strict_hits = sum(
            strict.is_speech(audio[i : i + FRAME_SAMPLES])
            for i in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
        )
        lenient_hits = sum(
            lenient.is_speech(audio[i : i + FRAME_SAMPLES])
            for i in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
        )

        assert lenient_hits > strict_hits


class TestWithTheSegmenter:
    """The two together are what the session actually uses."""

    @requires_piper
    @pytest.mark.parametrize("phrase", ["en_wake_command", "ru_wake_command"])
    def test_a_spoken_phrase_becomes_exactly_one_utterance(self, speech_fixture, phrase) -> None:  # type: ignore[no-untyped-def]
        detector = vad()
        segmenter = SpeechSegmenter()
        # Silence on both sides, as a microphone would deliver it.
        pad = np.zeros(SAMPLE_RATE, dtype=np.float32)
        audio = np.concatenate([pad, speech_fixture(phrase), pad])

        chunks = [
            chunk
            for frame in frames_from_array(audio)
            if (chunk := segmenter.push(frame, is_speech=detector.is_speech(frame.samples)))
            is not None
        ]

        assert len(chunks) == 1, f"got {len(chunks)} utterances"
        assert 0.5 < chunks[0].duration_s < 5.0
        assert not segmenter.last_was_truncated

    @requires_piper
    def test_the_utterance_starts_before_the_first_detected_frame(self, speech_fixture) -> None:  # type: ignore[no-untyped-def]
        """The pre-roll: otherwise «закрой» arrives as «акрой»."""
        detector = vad()
        segmenter = SpeechSegmenter()
        pad = np.zeros(SAMPLE_RATE, dtype=np.float32)
        audio = np.concatenate([pad, speech_fixture("ru_wake_command"), pad])

        first_speech_at = None
        chunk = None
        for frame in frames_from_array(audio):
            speaking = detector.is_speech(frame.samples)
            if speaking and first_speech_at is None:
                first_speech_at = frame.started_at
            produced = segmenter.push(frame, is_speech=speaking)
            if produced is not None:
                chunk = produced

        assert chunk is not None and first_speech_at is not None
        assert chunk.started_at < first_speech_at

    def test_pure_silence_produces_no_utterance(self, silence: np.ndarray) -> None:
        detector = vad()
        segmenter = SpeechSegmenter()

        produced = [
            segmenter.push(frame, is_speech=detector.is_speech(frame.samples))
            for frame in frames_from_array(silence)
        ]

        assert all(chunk is None for chunk in produced)
