"""The sherpa-onnx keyword spotter, against real synthesised speech.

The positive cases are the point, as they were for the VAD. A keyword spotter
that never fires passes every "must stay silent" test perfectly, and this engine
is *unusually* quiet — it rejects every near-miss that defeated the trained
openWakeWord classifier. Testing only rejection would therefore look excellent
and prove nothing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from atlas_voice.engines.sherpa_kws import KeywordModel, SherpaKeywordSpotter
from atlas_voice.providers import VoiceEngineError, WakeWordProvider
from voicefixtures import MODELS, PIPER_MULTI, _synthesise, requires_piper

GIGASPEECH = KeywordModel(
    directory=MODELS / "kws" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
    encoder="encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    decoder="decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    joiner="joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    tokenisation="bpe",
)
ZH_EN = KeywordModel(
    directory=MODELS / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    encoder="encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    decoder="decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    joiner="joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    tokenisation="phone",
)

requires_kws = pytest.mark.skipif(
    not (GIGASPEECH.directory.is_dir() and ZH_EN.directory.is_dir()),
    reason="run scripts/fetch_voice_models.ps1 to download the keyword-spotting models",
)

pytestmark = [requires_kws, requires_piper]


def spotter(model: KeywordModel = GIGASPEECH, **kwargs: object) -> SherpaKeywordSpotter:
    options: dict[str, object] = {"phrases": ("HEY JARVIS", "JARVIS")}
    return SherpaKeywordSpotter(model, **(options | kwargs))  # type: ignore[arg-type]


def say(text: str, *, speaker: int = 11) -> np.ndarray:
    """The phrase with a second of silence either side, as a microphone gives it."""
    quiet = np.zeros(SAMPLE_RATE, dtype=np.float32)
    return np.concatenate([quiet, _synthesise(PIPER_MULTI, text, speaker_id=speaker), quiet])


def run(detector: SherpaKeywordSpotter, audio: np.ndarray) -> object | None:
    """Stream in pipeline frames, then flush, exactly as the session will."""
    found = None
    for offset in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        found = found or detector.push(audio[offset : offset + FRAME_SAMPLES])
    return found or detector.flush()


#: Enough different voices that the figure below means something. Recall is a
#: rate, not a per-speaker guarantee: asserting that a *particular* synthetic
#: speaker always fires turns a statistical property into a brittle boolean, and
#: two of them genuinely do not fire at default prosody.
SPEAKERS = [11, 42, 120, 200, 333, 470, 555, 640, 700, 777, 830, 900]


class TestItHearsTheWakePhrase:
    def test_hey_jarvis_recall_across_twelve_voices(self) -> None:
        detector = spotter()
        heard = sum(run(detector, say("Hey Jarvis", speaker=s)) is not None for s in SPEAKERS)

        # Measured at 11–12 of 12. The bar is set below the observation, not at
        # it: this is a rate, and pinning a test to the best run it has ever had
        # produces a suite that fails on a good day with a different seed.
        assert heard >= 10, f"only {heard}/{len(SPEAKERS)} voices were heard"

    def test_the_bare_word_is_heard_too_though_less_reliably(self) -> None:
        detector = spotter()
        heard = sum(run(detector, say("Jarvis", speaker=s)) is not None for s in SPEAKERS)

        # Consistently a little worse than the two-word form: less phonetic
        # material, less evidence. Recorded rather than hidden.
        assert heard >= 8, f"only {heard}/{len(SPEAKERS)} voices were heard"

    def test_the_detection_is_timed_from_the_stream(self) -> None:
        detection = run(spotter(), say("Hey Jarvis"))

        assert detection is not None
        # A second of silence precedes the phrase, so nothing can fire before it.
        assert 1.0 < detection.at < 4.0  # type: ignore[attr-defined]
        assert detection.label == "jarvis"  # type: ignore[attr-defined]

    def test_the_phoneme_model_hears_it_as_well(self) -> None:
        assert run(spotter(ZH_EN), say("Hey Jarvis")) is not None


class TestItIgnoresEverythingElse:
    @pytest.mark.parametrize(
        "phrase",
        [
            "Travis",
            "Jargon",
            "Service",
            "Harvey asked about the harvest",
            "Starve us of detail and we guess",
            "Java is running on the server",
            "The service is available again",
        ],
    )
    def test_near_misses_stay_silent(self, phrase: str) -> None:
        """Every one of these fired at 0.97 or above on the trained classifier."""
        assert run(spotter(), say(phrase)) is None

    def test_silence_stays_silent(self) -> None:
        assert run(spotter(), np.zeros(SAMPLE_RATE * 3, dtype=np.float32)) is None

    def test_noise_stays_silent(self) -> None:
        rng = np.random.default_rng(20260815)
        noise = (rng.standard_normal(SAMPLE_RATE * 3) * 0.05).astype(np.float32)

        assert run(spotter(), noise) is None


class TestStreaming:
    def test_one_utterance_fires_once(self) -> None:
        detector = spotter()
        audio = say("Hey Jarvis")

        detections = [
            found
            for offset in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
            if (found := detector.push(audio[offset : offset + FRAME_SAMPLES])) is not None
        ]

        assert len(detections) == 1

    def test_odd_chunk_sizes_are_handled(self) -> None:
        detector = spotter()
        audio = say("Hey Jarvis")
        found = None
        for offset in range(0, len(audio), 333):
            found = found or detector.push(audio[offset : offset + 333])

        assert (found or detector.flush()) is not None

    def test_flush_recovers_a_phrase_at_the_very_end(self) -> None:
        """Without the final flush the last word of a clip is still mid-decode.

        In a file-driven test that looks identical to a detector that cannot
        hear, which is exactly the sort of mistake this suite exists to catch.
        """
        detector = spotter()
        audio = np.concatenate(
            [np.zeros(SAMPLE_RATE, dtype=np.float32), _synthesise(PIPER_MULTI, "Hey Jarvis")]
        )

        streamed = None
        for offset in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            streamed = streamed or detector.push(audio[offset : offset + FRAME_SAMPLES])

        assert streamed is not None or detector.flush() is not None


class TestContract:
    def test_it_satisfies_the_provider_protocol(self) -> None:
        assert isinstance(spotter(), WakeWordProvider)

    def test_a_missing_model_file_says_how_to_get_it(self) -> None:
        broken = KeywordModel(
            directory=Path("nowhere"),
            encoder="e.onnx",
            decoder="d.onnx",
            joiner="j.onnx",
        )
        with pytest.raises(VoiceEngineError, match="fetch_voice_models"):
            SherpaKeywordSpotter(broken, phrases=("JARVIS",))

    def test_a_word_outside_the_phoneme_lexicon_is_refused(self) -> None:
        """Guessing a pronunciation would produce a wake word that never fires
        and no error to explain why."""
        with pytest.raises(VoiceEngineError, match="CMU phonemes"):
            SherpaKeywordSpotter(ZH_EN, phrases=("ДЖАРВИС",))

    def test_hand_written_phonemes_are_accepted(self) -> None:
        detector = SherpaKeywordSpotter(ZH_EN, keyword_tokens=("JH AA1 R V AH0 S",))

        assert run(detector, say("Jarvis")) is not None

    def test_no_phrases_at_all_is_refused(self) -> None:
        with pytest.raises(VoiceEngineError, match="no keyword phrases"):
            SherpaKeywordSpotter(GIGASPEECH, phrases=())
