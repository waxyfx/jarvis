"""Turn-taking, tested exactly.

The segmenter takes booleans, so these tests state timings in frames and assert
them to the frame. "Did it wait long enough before deciding I had finished
speaking" is the kind of property that regresses silently and is then blamed on
the microphone.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas_voice.audio import FRAME_MS, FRAME_SAMPLES, Frame
from atlas_voice.providers import SpeechChunk
from atlas_voice.segmenter import SegmenterConfig, SpeechSegmenter


def frame(index: int, *, level: float = 0.4) -> Frame:
    return Frame(
        samples=np.full(FRAME_SAMPLES, level, dtype=np.float32),
        started_at=index * FRAME_MS / 1000,
    )


def feed(
    segmenter: SpeechSegmenter, pattern: str, *, start: int = 0
) -> tuple[list[SpeechChunk], int]:
    """Drive the segmenter from a string: ``#`` is speech, ``.`` is silence."""
    chunks: list[SpeechChunk] = []
    index = start
    for symbol in pattern:
        chunk = segmenter.push(frame(index), is_speech=symbol == "#")
        if chunk is not None:
            chunks.append(chunk)
        index += 1
    return chunks, index


#: Round numbers in frames: start after 3, end after 10, pre-roll 6, min 4.
TUNED = SegmenterConfig(
    start_after_ms=3 * FRAME_MS,
    end_after_ms=10 * FRAME_MS,
    preroll_ms=6 * FRAME_MS,
    min_utterance_ms=4 * FRAME_MS,
    max_utterance_ms=40 * FRAME_MS,
)


class TestOpeningAnUtterance:
    def test_a_single_noisy_frame_does_not_open_one(self) -> None:
        """A key press, a door. The pre-roll means waiting costs nothing."""
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "..#..........")

        assert chunks == []
        assert not segmenter.is_open

    def test_sustained_speech_opens_one(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        feed(segmenter, "...###")

        assert segmenter.is_open

    def test_the_preroll_keeps_audio_from_before_the_decision(self) -> None:
        """Without it the first consonant is lost: «закрой» → «акрой»."""
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "........" + "#" * 5 + "." * 12)

        assert len(chunks) == 1
        # Opened on frame 3 of speech, but audio reaches back a pre-roll before.
        assert chunks[0].started_at < 8 * FRAME_MS / 1000


class TestClosingAnUtterance:
    def test_a_short_pause_does_not_end_the_turn(self) -> None:
        """People pause to think. Ending here is the rudest possible bug."""
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#####" + "." * 6 + "#####")

        assert chunks == []
        assert segmenter.is_open

    def test_a_long_pause_ends_the_turn(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#####" + "." * 10)

        assert len(chunks) == 1
        assert not segmenter.is_open

    def test_the_trailing_silence_is_mostly_trimmed(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#" * 10 + "." * 10)
        chunk = chunks[0]

        # Some tail is kept deliberately, so the last consonant survives; the
        # whole 10 frames of silence must not be.
        assert chunk.duration_s < (10 + 10) * FRAME_MS / 1000
        assert chunk.duration_s > 10 * FRAME_MS / 1000

    def test_two_utterances_are_reported_separately(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#####" + "." * 10 + "#####" + "." * 10)

        assert len(chunks) == 2
        assert chunks[0].ended_at <= chunks[1].started_at

    def test_a_very_short_utterance_is_discarded(self) -> None:
        """A cough that lasted long enough to open, not long enough to mean anything."""
        tight = SegmenterConfig(
            start_after_ms=1 * FRAME_MS,
            end_after_ms=3 * FRAME_MS,
            preroll_ms=1 * FRAME_MS,
            min_utterance_ms=20 * FRAME_MS,
            max_utterance_ms=40 * FRAME_MS,
        )
        segmenter = SpeechSegmenter(tight)

        chunks, _ = feed(segmenter, "#" + "." * 5)

        assert chunks == []
        assert not segmenter.is_open


class TestTheCeiling:
    def test_endless_speech_is_cut_off(self) -> None:
        """A stuck VAD, a fan, a television left on. The turn still has to end."""
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#" * 100)

        assert len(chunks) >= 1
        assert segmenter.last_was_truncated

    def test_an_ordinary_ending_is_not_marked_truncated(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        feed(segmenter, "#####" + "." * 10)

        assert not segmenter.last_was_truncated

    def test_a_config_with_an_impossible_range_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SegmenterConfig(min_utterance_ms=1000, max_utterance_ms=500)


class TestFlush:
    def test_flushing_keeps_what_was_being_said(self) -> None:
        """Mute pressed mid-sentence: the finished words are still a command."""
        segmenter = SpeechSegmenter(TUNED)
        feed(segmenter, "#" * 10)

        chunk = segmenter.flush()

        assert chunk is not None
        assert chunk.duration_s > 0
        assert not segmenter.is_open

    def test_flushing_with_nothing_open_returns_nothing(self) -> None:
        assert SpeechSegmenter(TUNED).flush() is None

    def test_reset_discards_an_open_utterance(self) -> None:
        segmenter = SpeechSegmenter(TUNED)
        feed(segmenter, "#" * 10)

        segmenter.reset()

        assert not segmenter.is_open
        assert segmenter.flush() is None


class TestChunkContents:
    def test_the_samples_are_the_concatenated_audio(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "#" * 10 + "." * 10)
        chunk = chunks[0]

        assert chunk.samples.dtype == np.float32
        assert len(chunk.samples) == pytest.approx(chunk.duration_s * 16_000, rel=0.02)

    def test_timestamps_come_from_the_stream_not_the_clock(self) -> None:
        segmenter = SpeechSegmenter(TUNED)

        chunks, _ = feed(segmenter, "." * 20 + "#" * 10 + "." * 10)

        assert chunks[0].started_at > 0.1
