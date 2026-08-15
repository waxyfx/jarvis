"""Pytest fixtures for the voice suite. The material lives in
:mod:`voicefixtures`; this file only exposes it to tests."""

from __future__ import annotations

import numpy as np
import pytest

from atlas_voice.audio import SAMPLE_RATE
from voicefixtures import speech


@pytest.fixture(scope="session")
def speech_fixture():  # type: ignore[no-untyped-def]
    """``speech_fixture("ru_wake_command")`` → float32 audio at 16 kHz."""
    return speech


@pytest.fixture(scope="session")
def silence() -> np.ndarray:
    return np.zeros(SAMPLE_RATE * 2, dtype=np.float32)


@pytest.fixture(scope="session")
def room_noise() -> np.ndarray:
    """Broadband noise at a level a microphone would actually pick up.

    Not speech, and the VAD must say so — otherwise every fan and air
    conditioner opens a turn.
    """
    rng = np.random.default_rng(20260815)
    return (rng.standard_normal(SAMPLE_RATE * 2) * 0.05).astype(np.float32)
