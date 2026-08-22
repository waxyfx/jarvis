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

**The owner is asked to vary how they speak.** Not for realism's sake: a
profile built from twelve takes at one distance and one volume describes one way
of speaking, and the owner has several. The first real profile made here scored
cohesion 0.84 — nominally excellent — and then scored 0.54 against its own owner
speaking quietly, under a threshold of 0.55. It would have ignored him. So the
script asks for a few takes quietly and a few from across the room, and the
profile records which manners it heard.

**Nothing leaves the machine.** Not the audio, not the embedding, not a summary
of either. This module has no network access of any kind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE, write_wav
from atlas_voice.profile import VoiceProfile, VoiceProfileStore, now
from atlas_voice.providers import VoiceEngineError

__all__ = ["PHRASES", "EnrollmentSession", "Manner", "Prompt", "TakeVerdict"]

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
#:
#: Measured rather than guessed, and the first guess was wrong: this machine's
#: onboard microphone reads rms 0.0105 with nobody speaking, so the original
#: 0.01 would have accepted a recording of an empty room as a phrase. Speech at
#: a normal distance sits an order of magnitude above this.
MINIMUM_RMS = 0.025
#: A second floor, because room noise is diffuse while speech has peaks. The
#: same silent room peaks at 0.053; a spoken phrase reaches several times that.
MINIMUM_PEAK = 0.10
#: A take whose embedding sits this far from the others was not the same voice,
#: the same room, or the same microphone position.
#:
#: Kept deliberately loose now that the script asks for quiet and distant takes:
#: those *should* sit further from the centroid, and dropping them would undo
#: the coverage they were recorded for. It still catches a different person.
OUTLIER_SIMILARITY = 0.45


class Manner(StrEnum):
    """How a phrase is to be spoken.

    Each one is a region of the owner's voice that verification will meet in
    daily use. A profile that never heard the quiet one will not recognise it.
    """

    NORMAL = "normal"
    QUIET = "quiet"
    DISTANT = "distant"

    @property
    def hint(self) -> str:
        """Shown to the person, in words they can act on."""
        return {
            Manner.NORMAL: "",
            Manner.QUIET: "Say this one quietly — as if someone nearby were asleep.",
            Manner.DISTANT: "Say this one from where you normally sit, a metre or two back.",
        }[self]


@dataclass(frozen=True)
class Prompt:
    """One line to read, and how to read it."""

    language: Language
    text: str
    manner: Manner = Manner.NORMAL

    @property
    def hint(self) -> str:
        return self.manner.hint


def _manner_plan(count: int) -> list[Manner]:
    """Which take is spoken which way.

    Roughly a sixth quiet and a sixth distant, never the first — the first take
    is where people find their footing — and spread out rather than bunched at
    the end, so a session abandoned halfway still has some coverage.
    """
    plan = [Manner.NORMAL] * count
    varied = max(1, count // 6)
    special = [Manner.QUIET] * varied + [Manner.DISTANT] * varied
    if len(special) >= count:
        special = special[: max(0, count - 2)]
    if not special:
        return plan

    # Evenly spaced over everything after the first take.
    span = count - 1
    for position, manner in enumerate(special):
        index = 1 + round(span * (position + 1) / (len(special) + 1))
        while index < count and plan[index] is not Manner.NORMAL:
            index += 1
        if index < count:
            plan[index] = manner
    return plan


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
    _manners: list[Manner] = field(default_factory=list, init=False)
    _saved: list[Path] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.phrase_count < 4:
            raise ValueError("four phrases is the fewest that gives a usable centroid")

    # ------------------------------------------------------------- prompting

    def script(self) -> tuple[Prompt, ...]:
        """The phrases to read, alternating languages, with how to say each."""
        pools = {language: list(PHRASES[language]) for language in self.languages}
        manners = _manner_plan(self.phrase_count)
        script: list[Prompt] = []
        index = 0
        while len(script) < self.phrase_count:
            language = list(self.languages)[index % len(self.languages)]
            pool = pools[language]
            script.append(
                Prompt(
                    language=language,
                    text=pool[(index // len(self.languages)) % len(pool)],
                    manner=manners[len(script)],
                )
            )
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
        if rms < MINIMUM_RMS or peak < MINIMUM_PEAK:
            return TakeVerdict(False, "too_quiet", seconds, peak, rms)
        return TakeVerdict(True, "", seconds, peak, rms)

    def add(
        self, samples: np.ndarray, *, phrase: str = "", manner: Manner = Manner.NORMAL
    ) -> TakeVerdict:
        """Judge a take and, if it passes, keep its embedding.

        The level gate is the same whichever manner was asked for. A quiet take
        that falls under it is not quiet, it is unusable, and the floor was
        measured against this machine's actual noise rather than chosen.
        """
        verdict = self.judge(samples)
        if not verdict.accepted:
            return verdict

        try:
            vector = self.embed.embed(samples)  # type: ignore[attr-defined]
        except VoiceEngineError as error:
            return TakeVerdict(False, str(error), verdict.seconds, verdict.peak, verdict.rms)

        self._embeddings.append(vector)
        self._accepted.append(phrase)
        self._manners.append(manner)
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
            covers=self._coverage(keep),
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

    def _coverage(self, keep: Sequence[int]) -> tuple[str, ...]:
        """Which ways of speaking actually made it into the profile.

        Taken from the takes that survived rather than from the script, because
        a manner whose takes were all rejected was never heard.
        """
        seen = {self._manners[index] for index in keep if index < len(self._manners)}
        return tuple(sorted(str(manner) for manner in seen))

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
        self._manners.clear()
        for path in self._saved:
            path.unlink(missing_ok=True)
        self._saved.clear()
