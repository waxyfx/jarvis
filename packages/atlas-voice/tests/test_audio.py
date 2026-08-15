"""Audio primitives. No sound device, no models, no network."""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import numpy as np
import pytest

from atlas_voice.audio import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Frame,
    RingBuffer,
    frames_from_array,
    read_wav,
    write_wav,
)


def tone(seconds: float, freq: float = 440.0, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE), dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestFrame:
    def test_a_frame_knows_where_it_sits_in_the_stream(self) -> None:
        frame = Frame(samples=np.zeros(FRAME_SAMPLES, dtype=np.float32), started_at=1.5)

        assert frame.duration_s == pytest.approx(FRAME_MS / 1000)
        assert frame.ends_at == pytest.approx(1.5 + FRAME_MS / 1000)

    def test_the_wrong_dtype_is_refused_rather_than_coerced(self) -> None:
        # Silent coercion here would mean int16 audio reaching a model that
        # expects [-1, 1] and being 32768 times too loud.
        with pytest.raises(ValueError, match="float32"):
            Frame(samples=np.zeros(FRAME_SAMPLES, dtype=np.int16), started_at=0.0)

    def test_stereo_is_refused(self) -> None:
        with pytest.raises(ValueError, match="mono"):
            Frame(samples=np.zeros((FRAME_SAMPLES, 2), dtype=np.float32), started_at=0.0)

    def test_silence_and_speech_are_distinguishable_by_level(self) -> None:
        silence = Frame(samples=np.zeros(FRAME_SAMPLES, dtype=np.float32), started_at=0.0)
        loud = Frame(samples=tone(FRAME_MS / 1000), started_at=0.0)

        assert silence.rms == 0.0
        assert silence.peak == 0.0
        assert loud.rms > 0.3
        assert not silence.is_clipped

    def test_clipping_is_detected(self) -> None:
        samples = tone(FRAME_MS / 1000, amplitude=2.0).clip(-1.0, 1.0)
        assert Frame(samples=samples, started_at=0.0).is_clipped

    def test_a_loud_but_unclipped_frame_is_not_flagged(self) -> None:
        # 0.95 peak is a healthy recording level, not a rejected take.
        assert not Frame(samples=tone(FRAME_MS / 1000, amplitude=0.95), started_at=0.0).is_clipped


class TestFraming:
    def test_an_array_cuts_into_whole_frames(self) -> None:
        # 512 samples is 32 ms, so a second is 31.25 frames: 31 whole ones and a
        # padded remainder. Silero wants exactly 512-sample windows, which is
        # worth more than a frame count that divides a second evenly.
        frames = list(frames_from_array(tone(1.0)))

        assert len(frames) == math.ceil(SAMPLE_RATE / FRAME_SAMPLES)
        assert all(len(frame.samples) == FRAME_SAMPLES for frame in frames)

    def test_frame_timestamps_are_contiguous(self) -> None:
        frames = list(frames_from_array(tone(0.5), start_at=10.0))

        assert frames[0].started_at == pytest.approx(10.0)
        for earlier, later in itertools.pairwise(frames):
            assert later.started_at == pytest.approx(earlier.ends_at)

    def test_a_ragged_tail_is_padded_by_default(self) -> None:
        samples = tone(FRAME_MS / 1000 * 2.5)
        frames = list(frames_from_array(samples))

        assert len(frames) == 3
        assert len(frames[-1].samples) == FRAME_SAMPLES
        # The padding is silence, not a repeat of earlier audio.
        assert frames[-1].samples[-10:].tolist() == [0.0] * 10

    def test_a_ragged_tail_can_be_dropped_instead(self) -> None:
        samples = tone(FRAME_MS / 1000 * 2.5)
        assert len(list(frames_from_array(samples, pad_final=False))) == 2

    def test_int_input_is_converted_rather_than_rejected(self) -> None:
        # Unlike Frame itself: this is the documented conversion point.
        frames = list(frames_from_array(np.zeros(FRAME_SAMPLES, dtype=np.float64)))
        assert frames[0].samples.dtype == np.float32


class TestRingBuffer:
    def test_it_holds_the_most_recent_audio_and_drops_the_rest(self) -> None:
        buffer = RingBuffer(seconds=1.0)
        for frame in frames_from_array(tone(3.0)):
            buffer.push(frame)

        assert buffer.seconds_held == pytest.approx(1.0, abs=FRAME_MS / 1000)

    def test_the_tail_is_the_audio_just_before_now(self) -> None:
        """The reason the buffer exists: "Atlas, закрой Notepad" in one breath.

        The wake word is only recognised after it has been said, so the command
        that followed it is already in the past by the time anything reacts.
        """
        buffer = RingBuffer(seconds=2.0)
        marker = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)
        for frame in frames_from_array(np.zeros(SAMPLE_RATE, dtype=np.float32)):
            buffer.push(frame)
        buffer.push(Frame(samples=marker, started_at=1.0))

        tail = buffer.tail(seconds=FRAME_MS / 1000)
        assert tail.tolist() == marker.tolist()

    def test_asking_for_more_than_it_holds_returns_what_there_is(self) -> None:
        buffer = RingBuffer(seconds=5.0)
        for frame in frames_from_array(tone(0.2)):
            buffer.push(frame)

        tail = buffer.tail(seconds=5.0)
        assert 0 < len(tail) <= SAMPLE_RATE

    def test_an_empty_buffer_yields_empty_audio_not_a_crash(self) -> None:
        assert len(RingBuffer(seconds=1.0).tail(seconds=1.0)) == 0

    def test_clearing_forgets_everything(self) -> None:
        buffer = RingBuffer(seconds=1.0)
        for frame in frames_from_array(tone(1.0)):
            buffer.push(frame)
        buffer.clear()

        assert len(buffer) == 0

    def test_a_zero_length_buffer_is_refused(self) -> None:
        with pytest.raises(ValueError):
            RingBuffer(seconds=0)


class TestWavRoundTrip:
    def test_audio_survives_a_write_and_read(self, tmp_path: Path) -> None:
        original = tone(0.25)
        path = tmp_path / "sample.wav"
        write_wav(path, original)

        assert np.allclose(read_wav(path), original, atol=1e-4)

    def test_out_of_range_samples_are_clipped_not_wrapped(self, tmp_path: Path) -> None:
        # Wrapping turns a loud passage into white noise, which sounds like a
        # broken microphone and is hard to trace back to the writer.
        path = tmp_path / "loud.wav"
        write_wav(path, np.array([2.0, -2.0, 0.0], dtype=np.float32))

        assert read_wav(path).tolist() == pytest.approx([1.0, -1.0, 0.0], abs=1e-4)

    def test_a_fixture_at_the_wrong_rate_fails_loudly(self, tmp_path: Path) -> None:
        import wave

        path = tmp_path / "wrong.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44_100)
            handle.writeframes(b"\x00\x00" * 100)

        with pytest.raises(ValueError, match="16000"):
            read_wav(path)

    def test_a_stereo_fixture_fails_loudly(self, tmp_path: Path) -> None:
        import wave

        path = tmp_path / "stereo.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * 200)

        with pytest.raises(ValueError, match="mono"):
            read_wav(path)
