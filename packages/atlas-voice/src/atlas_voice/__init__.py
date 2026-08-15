"""ATLAS Voice Engine.

Runs entirely on the Windows Agent. Audio never leaves this machine: the wake
word, the voice activity detector, the recogniser, the speaker embedding and the
synthesiser are all local, and only *text* is sent to the backend — the same
text a keyboard would have produced, through the same M3 endpoint, meeting the
same Policy Engine.

Importing this package pulls in no model, no GPU runtime and no sound library.
The engines live behind extras (``atlas-voice[stt]`` and friends) and are
imported when selected, so the contracts and the session logic stay testable on
a machine with no audio hardware.
"""

from __future__ import annotations

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
from atlas_voice.providers import (
    Detection,
    SpeakerProvider,
    SpeechChunk,
    STTProvider,
    Transcript,
    TTSProvider,
    Utterance,
    VADProvider,
    VerificationResult,
    Voice,
    VoiceEngineError,
    WakeWordProvider,
)
from atlas_voice.state import Actor, IllegalTransitionError, VoiceState, VoiceStateMachine

__all__ = [
    "FRAME_MS",
    "FRAME_SAMPLES",
    "SAMPLE_RATE",
    "Actor",
    "Detection",
    "Frame",
    "IllegalTransitionError",
    "RingBuffer",
    "STTProvider",
    "SpeakerProvider",
    "SpeechChunk",
    "TTSProvider",
    "Transcript",
    "Utterance",
    "VADProvider",
    "VerificationResult",
    "Voice",
    "VoiceEngineError",
    "VoiceState",
    "VoiceStateMachine",
    "WakeWordProvider",
    "frames_from_array",
    "read_wav",
    "write_wav",
]
