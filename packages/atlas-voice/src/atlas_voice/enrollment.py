"""Registering the owner's voice: the flow, without the user interface.

The person reads a handful of short phrases; this collects them, judges each
take, and builds one averaged embedding. Everything about *how* it is presented
— a window, a tray wizard, a phone screen — lives elsewhere and drives this.

Four rules it enforces, each because the alternative fails quietly.

**Bad takes are rejected while the person is still there.** A clipped or
whispered recording still produces a perfectly confident embedding, and averaging
it in poisons the profile. The failure would surface weeks later as an assistant
that stopped recognising its owner. Each take is checked and, if it is no good,
asked for again with the reason.

**Takes that disagree with each other are caught.** If one phrase was recorded
with the microphone somewhere else, or someone else spoke, the embedding for
that take sits away from the rest. Cohesion is measured and reported, and a weak
profile says so rather than pretending.

**The recordings are deleted by default.** Once the profile exists the audio has
served its purpose. Keeping it is possible and deliberate, never accidental.

**Nothing leaves the machine.** Not the audio, not the embedding, not a summary
of either. This module has no network access of any kind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE, write_wav
from atlas_voice.profile import VoiceProfile, VoiceProfileStore, now
from atlas_voice.providers import VoiceEngineError

__all__ = ["PHRASES", "EnrollmentSession", "TakeVerdict"]

#: What the person is asked to read. Mixed languages on purpose: the profile
#: should hold up whichever they use, and Russian and English shape a voice
#: differently enough to matter. Short, ordinary, and nothing worth overhearing.
PHRASES: dict[Language, tuple[str, ...]] = {
    Language.EN: (
        "Good morning, Jarvis.",
        "Open the second document and check the totals.",
        "The meeting has been moved to Thursday afternoon.",
        "Remind me to call back before six.",
        "Show me how much disk space is left.",
        "That is all for now, thank you.",
    ),
    Language.RU: (
        "Доброе утро, Джарвис.",
        "Открой второй документ и проверь итоги.",
        "Совещание перенесли на четверг.",
        "Напомни перезвонить до шести.",
        "Покажи, сколько осталось места на диске.",
        "На сегодня всё, спасибо.",
    ),
}

#: Enough speech for a stable centroid without making the person read an essay.
#: Twelve short phrases come to roughly forty seconds.
DEFAULT_PHRASE_COUNT = 12
#: A take shorter than this is a false start rather than a phrase.
MINIMUM_TAKE_SECONDS = 1.0
#: Peaks at full scale mean the waveform has been flattened, and the embedding
#: then partly describes the clipping.
CLIPPING_LIMIT = 0.999
#: Below this the microphone was too far away or the person too quiet.
MINIMUM_RMS = 0.01
#: A take whose embedding sits this far from the others was not the same voice,
#: the same room, or the same microphone position.
OUTLIER_SIMILARITY = 0.45


@dataclass(frozen=True)
class TakeVerdict:
    """Whether one recording may go into the profile, and why not."""

    accepted: bool
    reason: str = ""
    seconds: float = 0.0
    peak: float = 0.0
    rms: float = 0.0

    @property
    def advice(self) -> str:
        """What to tell the person, in words they can act on."""
        return {
            "too_short": "That was cut off — please read the whole phrase.",
            "too_quiet": "A little louder, or slightly closer to the microphone.",
            "clipped": "That was too loud — please move back a little.",
            "": "",
        }.get(self.reason, self.reason)


@dataclass
class EnrollmentSession:
    """Collects takes, judges them, and produces a profile at the end."""

    embed: object  # SherpaSpeaker; typed loosely to avoid importing the engine
    store: VoiceProfileStore
    languages: Sequence[Language] = (Language.EN, Language.RU)
    phrase_count: int = DEFAULT_PHRASE_COUNT
    #: Off by default. The recordings exist to make the profile and nothing
    #: else; keeping them has to be a decision.
    keep_recordings: bool = False
    recordings_dir: Path | None = None

    _embeddings: list[np.ndarray] = field(default_factory=list, init=False)
    _accepted: list[str] = field(default_factory=list, init=False)
    _saved: list[Path] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.phrase_count < 4:
            raise ValueError("four phrases is the fewest that gives a usable centroid")

    # ------------------------------------------------------------- prompting

    def script(self) -> tuple[tuple[Language, str], ...]:
        """The phrases to read, alternating between the languages."""
        pools = {language: list(PHRASES[language]) for language in self.languages}
        script: list[tuple[Language, str]] = []
        index = 0
        while len(script) < self.phrase_count:
            language = list(self.languages)[index % len(self.languages)]
            pool = pools[language]
            script.append((language, pool[(index // len(self.languages)) % len(pool)]))
            index += 1
        return tuple(script)

    @property
    def collected(self) -> int:
        return len(self._embeddings)

    @property
    def remaining(self) -> int:
        return max(0, self.phrase_count - self.collected)

    # --------------------------------------------------------------- takes

    def judge(self, samples: np.ndarray) -> TakeVerdict:
        """Is this recording good enough to use? Checked before embedding."""
        seconds = len(samples) / SAMPLE_RATE
        peak = float(np.abs(samples).max(initial=0.0))
        rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0

        if seconds < MINIMUM_TAKE_SECONDS:
            return TakeVerdict(False, "too_short", seconds, peak, rms)
        if np.count_nonzero(np.abs(samples) >= CLIPPING_LIMIT) >= 3:
            return TakeVerdict(False, "clipped", seconds, peak, rms)
        if rms < MINIMUM_RMS:
            return TakeVerdict(False, "too_quiet", seconds, peak, rms)
        return TakeVerdict(True, "", seconds, peak, rms)

    def add(self, samples: np.ndarray, *, phrase: str = "") -> TakeVerdict:
        """Judge a take and, if it passes, keep its embedding."""
        verdict = self.judge(samples)
        if not verdict.accepted:
            return verdict

        try:
            vector = self.embed.embed(samples)  # type: ignore[attr-defined]
        except VoiceEngineError as error:
            return TakeVerdict(False, str(error), verdict.seconds, verdict.peak, verdict.rms)

        self._embeddings.append(vector)
        self._accepted.append(phrase)
        if self.keep_recordings and self.recordings_dir is not None:
            self.recordings_dir.mkdir(parents=True, exist_ok=True)
            path = self.recordings_dir / f"take_{len(self._embeddings):02d}.wav"
            write_wav(path, samples)
            self._saved.append(path)
        return verdict

    # -------------------------------------------------------------- finish

    def outliers(self) -> list[int]:
        """Which takes disagree with the rest. Indices into the accepted list."""
        if len(self._embeddings) < 3:
            return []
        stack = np.stack(self._embeddings)
        centroid = stack.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        return [
            index
            for index, vector in enumerate(stack)
            if float(np.dot(centroid, vector)) < OUTLIER_SIMILARITY
        ]

    def finish(self, *, drop_outliers: bool = True) -> VoiceProfile:
        """Average the takes into a profile, save it, and clean up."""
        if len(self._embeddings) < 4:
            raise VoiceEngineError(
                f"only {len(self._embeddings)} usable takes; at least four are needed"
            )

        keep = list(range(len(self._embeddings)))
        if drop_outliers:
            discard = set(self.outliers())
            # Never discard so much that the profile is built from a handful.
            if len(keep) - len(discard) >= 4:
                keep = [index for index in keep if index not in discard]

        stack = np.stack([self._embeddings[index] for index in keep])
        centroid = stack.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        cohesion = float(np.mean([np.dot(centroid, vector) for vector in stack]))

        profile = VoiceProfile(
            embedding=centroid.astype(np.float32),
            phrases=len(keep),
            cohesion=cohesion,
            created_at=now(),
            model=str(getattr(self.embed, "model", "unknown")),
            dimensions=int(centroid.shape[0]),
        )
        self.store.save(profile)
        self._discard_recordings()
        return profile

    def test(self, samples: np.ndarray) -> float:
        """Score a fresh utterance against the profile just made.

        Enrollment that does not end with a check is enrollment nobody trusts:
        the number here is what tells the person it worked.
        """
        profile = self.store.load()
        if profile is None:
            raise VoiceEngineError("there is no profile to test against")
        return float(np.dot(profile.embedding, self.embed.embed(samples)))  # type: ignore[attr-defined]

    def _discard_recordings(self) -> None:
        if self.keep_recordings:
            return
        for path in self._saved:
            path.unlink(missing_ok=True)
        self._saved.clear()

    def abandon(self) -> None:
        """Give up part-way through, leaving nothing behind."""
        self._embeddings.clear()
        self._accepted.clear()
        for path in self._saved:
            path.unlink(missing_ok=True)
        self._saved.clear()
