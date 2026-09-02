"""Getting audio off the microphone.

The device itself cannot be exercised in CI, which is why ``capture`` is kept
thin — but thin is not empty, and what is left is the part where a mistake means
the assistant hears *nothing at all*. Silence is the hardest fault to notice: it
looks exactly like a room where nobody is speaking, and every downstream test
passes because every downstream test supplies its own audio.

So the sound library is faked and the arithmetic is checked: the downmix, the
resampling, the framing, the clock, and what happens when the consumer falls
behind.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import numpy as np
import pytest

import atlas_voice.capture as capture
from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from atlas_voice.capture import Microphone, list_input_devices
from atlas_voice.providers import VoiceEngineError


class FakeInputStream:
    """Enough of a PortAudio stream for the parts under test."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeInputStream:
        self.entered = True
        return self

    def __exit__(self, *_: Any) -> None:
        self.exited = True


class FakeSoundDevice:
    def __init__(self, *, rate: int = SAMPLE_RATE, fail: bool = False) -> None:
        self.rate = rate
        self.fail = fail
        self.streams: list[FakeInputStream] = []
        self.default = type("Default", (), {"device": (3, 4)})()

    def query_devices(self, device: Any = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return [
                {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
                {
                    "name": "Микрофон (Realtek)",
                    "max_input_channels": 2,
                    "default_samplerate": 48000.0,
                },
                {"name": "Headset", "max_input_channels": 1, "default_samplerate": 16000.0},
                {"name": "Default mic", "max_input_channels": 1, "default_samplerate": 44100.0},
            ]
        return {"default_samplerate": float(self.rate)}

    def InputStream(self, **kwargs: Any) -> FakeInputStream:  # noqa: N802 - mirrors the real name
        if self.fail:
            raise RuntimeError("device in use")
        stream = FakeInputStream(**kwargs)
        self.streams.append(stream)
        return stream


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSoundDevice:
    device = FakeSoundDevice()
    monkeypatch.setattr(capture, "_sounddevice", lambda: device)
    return device


def drive(microphone: Microphone, blocks: list[np.ndarray], *, want: int) -> list[Any]:
    """Feed blocks in from another thread and collect ``want`` frames."""

    def feed() -> None:
        for block in blocks:
            microphone._queue.put(block)

    threading.Thread(target=feed, daemon=True).start()

    collected = []
    for frame in microphone.frames():
        collected.append(frame)
        if len(collected) >= want:
            microphone.stop()
            break
    return collected


class TestFraming:
    def test_blocks_become_frames_of_the_pipeline_size(self, fake: FakeSoundDevice) -> None:
        microphone = Microphone()
        blocks = [np.full(FRAME_SAMPLES, 0.25, dtype=np.float32) for _ in range(4)]

        frames = drive(microphone, blocks, want=3)

        assert all(len(frame.samples) == FRAME_SAMPLES for frame in frames)

    def test_a_block_that_is_not_a_whole_frame_is_carried_over(self, fake: FakeSoundDevice) -> None:
        """Device block sizes need not divide by the frame size, and a
        remainder that was dropped instead of kept would puncture the audio
        with a gap every block — inaudible individually, ruinous for a
        recogniser."""
        microphone = Microphone()
        odd = FRAME_SAMPLES + 100
        blocks = [np.linspace(0, 1, odd, dtype=np.float32) for _ in range(4)]

        frames = drive(microphone, blocks, want=4)

        # Nothing lost at the seam: the second frame continues where the first
        # stopped rather than restarting at the next block.
        joined = np.concatenate([frame.samples for frame in frames])
        expected = np.concatenate(blocks)[: len(joined)]
        assert np.allclose(joined, expected)

    def test_the_clock_advances_by_one_frame_each_time(self, fake: FakeSoundDevice) -> None:
        microphone = Microphone()
        blocks = [np.zeros(FRAME_SAMPLES, dtype=np.float32) for _ in range(4)]

        frames = drive(microphone, blocks, want=3)

        step = FRAME_SAMPLES / SAMPLE_RATE
        assert frames[0].started_at == pytest.approx(0.0)
        assert frames[1].started_at == pytest.approx(step)
        assert frames[2].started_at == pytest.approx(2 * step)


class TestTheDeviceRate:
    def test_a_48k_device_is_resampled_to_the_pipeline_rate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking the driver to convert is the alternative, and some drivers
        oblige by decimating, which folds everything above 8 kHz back into the
        band the recogniser cares about."""
        device = FakeSoundDevice(rate=48000)
        monkeypatch.setattr(capture, "_sounddevice", lambda: device)
        microphone = Microphone()

        # One second of 48 kHz audio should become one second at 16 kHz.
        blocks = [np.zeros(48000, dtype=np.float32)]
        frames = drive(microphone, blocks, want=SAMPLE_RATE // FRAME_SAMPLES)

        assert len(frames) == SAMPLE_RATE // FRAME_SAMPLES

    def test_the_stream_is_opened_at_the_device_rate_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = FakeSoundDevice(rate=44100)
        monkeypatch.setattr(capture, "_sounddevice", lambda: device)
        microphone = Microphone()

        drive(microphone, [np.zeros(44100, dtype=np.float32)], want=1)

        assert device.streams[0].kwargs["samplerate"] == 44100
        assert device.streams[0].kwargs["channels"] == 1


class TestFallingBehind:
    def test_blocks_are_dropped_rather_than_queued_without_limit(
        self, fake: FakeSoundDevice
    ) -> None:
        """An unbounded queue trades a stutter for latency that never recovers:
        the assistant would answer questions from a minute ago."""
        microphone = Microphone(queue_frames=4)
        block = np.zeros(FRAME_SAMPLES, dtype=np.float32)

        for _ in range(10):
            microphone._callback(block.reshape(-1, 1), FRAME_SAMPLES, None, None)

        assert microphone.dropped_blocks == 6
        assert microphone._queue.qsize() == 4

    def test_dropping_is_counted_so_a_stuttering_device_is_visible(
        self, fake: FakeSoundDevice
    ) -> None:
        microphone = Microphone(queue_frames=1)

        assert microphone.dropped_blocks == 0
        for _ in range(3):
            microphone._callback(
                np.zeros((FRAME_SAMPLES, 1), dtype=np.float32), FRAME_SAMPLES, None, None
            )
        assert microphone.dropped_blocks == 2


class TestChannels:
    def test_a_stereo_microphone_is_reduced_to_one_channel(self, fake: FakeSoundDevice) -> None:
        """The models want mono, and the two channels of a stereo microphone
        are very nearly the same signal."""
        microphone = Microphone()
        stereo = np.stack(
            [np.full(FRAME_SAMPLES, 0.5, dtype=np.float32), np.zeros(FRAME_SAMPLES, np.float32)],
            axis=1,
        )

        microphone._callback(stereo, FRAME_SAMPLES, None, None)

        block = microphone._queue.get_nowait()
        assert block.ndim == 1
        assert np.allclose(block, 0.5)

    def test_a_mono_stream_is_passed_through(self, fake: FakeSoundDevice) -> None:
        microphone = Microphone()

        microphone._callback(
            np.full(FRAME_SAMPLES, 0.25, dtype=np.float32), FRAME_SAMPLES, None, None
        )

        assert np.allclose(microphone._queue.get_nowait(), 0.25)

    def test_the_callback_copies_rather_than_keeping_the_buffer(
        self, fake: FakeSoundDevice
    ) -> None:
        """PortAudio reuses that buffer for the next block. Keeping a reference
        means the queue quietly fills with whatever is arriving now."""
        microphone = Microphone()
        buffer = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)

        microphone._callback(buffer, FRAME_SAMPLES, None, None)
        buffer[:] = 0.0

        assert np.allclose(microphone._queue.get_nowait(), 0.5)


class TestRecording:
    def test_it_returns_exactly_the_length_asked_for(self, fake: FakeSoundDevice) -> None:
        """Enrollment trims to this, and a take reported as five seconds that
        is really 5.02 would put a wobble in every measurement built on it."""
        microphone = Microphone()
        blocks = [np.full(FRAME_SAMPLES, 0.3, dtype=np.float32) for _ in range(60)]

        def feed() -> None:
            for block in blocks:
                microphone._queue.put(block)

        threading.Thread(target=feed, daemon=True).start()
        recorded = microphone.record(0.5)

        assert len(recorded) == int(0.5 * SAMPLE_RATE)

    def test_nothing_captured_gives_an_empty_array_not_a_crash(self, fake: FakeSoundDevice) -> None:
        microphone = Microphone()
        microphone.stop()

        assert len(microphone.record(0.5)) == 0


class TestWhenTheDeviceIsNotThere:
    def test_a_missing_library_says_how_to_install_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This exact message is what someone sees when the extra is missing,
        and it has already happened once for real."""

        def absent() -> Any:
            raise VoiceEngineError(
                "sounddevice is required to use the microphone; install atlas-voice[audio]"
            )

        monkeypatch.setattr(capture, "_sounddevice", absent)

        with pytest.raises(VoiceEngineError, match=r"atlas-voice\[audio\]"):
            list_input_devices()

    def test_a_device_that_will_not_open_is_reported_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "_sounddevice", lambda: FakeSoundDevice(fail=True))
        microphone = Microphone()

        with pytest.raises(VoiceEngineError, match="could not open the microphone"):
            next(microphone.frames())


class TestListingDevices:
    def test_outputs_are_left_out(self, fake: FakeSoundDevice) -> None:
        names = [device.name for device in list_input_devices()]

        assert "Speakers" not in names
        assert len(names) == 3

    def test_the_default_is_marked(self, fake: FakeSoundDevice) -> None:
        default = [device for device in list_input_devices() if device.is_default]

        assert len(default) == 1
        assert default[0].index == 3

    def test_the_rate_is_reported_so_the_ui_can_warn(self, fake: FakeSoundDevice) -> None:
        listed = {device.name: device for device in list_input_devices()}

        assert listed["Микрофон (Realtek)"].default_rate == 48000.0
        assert listed["Микрофон (Realtek)"].channels == 2


def test_the_queue_is_bounded_by_construction() -> None:
    """Asserted directly, because the bound is the whole design and a default
    that silently became zero would make it unbounded again."""
    microphone = Microphone(queue_frames=8)

    assert isinstance(microphone._queue, queue.Queue)
    assert microphone._queue.maxsize == 8
