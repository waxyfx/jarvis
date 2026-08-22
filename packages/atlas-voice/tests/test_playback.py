"""Playback, and the one property that matters: it can be stopped.

The sound card is faked here. What is being tested is not whether audio reaches
the speakers — no test can know that — but whether cancelling the playback task
aborts the stream instead of draining it, because that difference is the whole
of barge-in.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import numpy as np
import pytest

import atlas_voice.playback as playback
from atlas_voice.playback import Loudspeaker, _Cursor, silence
from atlas_voice.providers import Utterance, Voice, VoiceEngineError


def utterance(seconds: float = 0.5, rate: int = 22050) -> Utterance:
    samples = np.sin(np.arange(int(seconds * rate), dtype=np.float32) * 0.05)
    return Utterance(samples=samples.astype(np.float32), sample_rate=rate, voice=Voice.EN)


class FakeStream:
    """Runs the callback on its own thread, like PortAudio does."""

    def __init__(self, callback: Any, finished_callback: Any, blocksize: int, **_: Any) -> None:
        self.callback = callback
        self.finished_callback = finished_callback
        self.blocksize = blocksize
        self.aborted = False
        self.stopped = False
        self.closed = False
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._halt.is_set():
            out = np.zeros((self.blocksize, 1), dtype=np.float32)
            try:
                self.callback(out, self.blocksize, None, None)
            except _CallbackStopError:
                break
            threading.Event().wait(0.001)
        self.finished_callback()

    def abort(self, ignore_errors: bool = True) -> None:
        self.aborted = True
        self._halt.set()

    def stop(self, ignore_errors: bool = True) -> None:
        self.stopped = True
        self._halt.set()

    def close(self, ignore_errors: bool = True) -> None:
        self.closed = True
        self._halt.set()


class _CallbackStopError(Exception):
    pass


class FakeSoundDevice:
    CallbackStop = _CallbackStopError

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.streams: list[FakeStream] = []

    def OutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - mirrors the real name
        if self.fail:
            raise RuntimeError("no such device")
        stream = FakeStream(**kwargs)
        self.streams.append(stream)
        return stream


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSoundDevice:
    device = FakeSoundDevice()
    monkeypatch.setattr(playback, "_sounddevice", lambda: device)
    return device


class TestTheCursor:
    def test_it_hands_out_whole_blocks(self) -> None:
        cursor = _Cursor(np.arange(1024, dtype=np.float32))

        chunk, last = cursor.take(512)

        assert len(chunk) == 512
        assert not last

    def test_the_last_block_is_padded_not_truncated(self) -> None:
        """A short block leaves the driver playing whatever was in the buffer,
        which is audible as a click at the end of every sentence."""
        cursor = _Cursor(np.ones(600, dtype=np.float32))
        cursor.take(512)

        chunk, last = cursor.take(512)

        assert len(chunk) == 512
        assert last
        assert chunk[88:].sum() == 0.0

    def test_an_exact_fit_still_reports_the_end(self) -> None:
        cursor = _Cursor(np.ones(512, dtype=np.float32))

        _, last = cursor.take(512)

        assert last
        assert cursor.finished


@pytest.mark.asyncio
class TestPlaying:
    async def test_it_plays_to_the_end(self, fake: FakeSoundDevice) -> None:
        speaker = Loudspeaker()

        await asyncio.wait_for(speaker.play(utterance(0.2)), timeout=5)

        assert fake.streams[0].closed

    async def test_nothing_to_play_opens_no_device(self, fake: FakeSoundDevice) -> None:
        empty = Utterance(samples=np.zeros(0, dtype=np.float32), sample_rate=22050, voice=Voice.EN)

        await Loudspeaker().play(empty)

        assert fake.streams == []

    async def test_cancelling_aborts_rather_than_draining(self, fake: FakeSoundDevice) -> None:
        """The whole point. ``stop()`` would finish the sentence first."""
        speaker = Loudspeaker()
        task = asyncio.create_task(speaker.play(utterance(30.0)))
        await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.streams[0].aborted
        assert not fake.streams[0].stopped

    async def test_cancelling_leaves_no_stream_open(self, fake: FakeSoundDevice) -> None:
        speaker = Loudspeaker()
        task = asyncio.create_task(speaker.play(utterance(30.0)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.streams[0].closed
        assert speaker._stream is None

    async def test_a_missing_device_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(playback, "_sounddevice", lambda: FakeSoundDevice(fail=True))

        with pytest.raises(VoiceEngineError, match="could not open the speakers"):
            await Loudspeaker().play(utterance())

    async def test_stopping_from_outside_works(self, fake: FakeSoundDevice) -> None:
        """The tray's mute does not own the playback task."""
        speaker = Loudspeaker()
        task = asyncio.create_task(speaker.play(utterance(30.0)))
        await asyncio.sleep(0.05)

        speaker.stop()
        await asyncio.wait_for(task, timeout=5)

        assert fake.streams[0].aborted


def test_silence_is_the_length_asked_for() -> None:
    assert len(silence(0.25, 16000)) == 4000
