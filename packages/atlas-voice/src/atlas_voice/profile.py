"""The owner's voice profile: a few hundred numbers, kept under lock.

What is stored is an *embedding* — a fixed-length vector describing the timbre
of a voice. It is not audio and cannot be played back. The recordings that
produced it are deleted once the profile exists, which is the default and has to
be turned off deliberately.

**Protection is injected, not assumed.** On Windows the agent supplies DPAPI,
scoped to the user account, exactly as it already protects the device key. This
package has no Windows dependency and no opinion about the mechanism; it refuses
to write an unprotected profile unless a caller explicitly asks for it, which
only tests do.

Nothing here reaches the network. The profile is never sent to the backend,
never sent to Gemini, and never leaves the machine — the same rule as the
enrollment audio.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

__all__ = ["Protector", "VoiceProfile", "VoiceProfileStore", "plaintext_protector"]

#: ``(protect, unprotect)``. The agent passes DPAPI; tests pass identity.
Protector = tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]


def plaintext_protector() -> Protector:
    """No protection at all. Only for tests, and named so nobody mistakes it."""
    return (lambda data: data, lambda data: data)


@dataclass(frozen=True)
class VoiceProfile:
    """The enrolled voice, and enough about it to judge whether it is any good."""

    embedding: np.ndarray
    #: How many phrases went into it.
    phrases: int
    #: How closely the individual phrase embeddings agreed with each other.
    #: A low value means one of the takes was not the same voice, or the room
    #: changed halfway through — the profile is then worth redoing.
    cohesion: float
    created_at: str
    model: str
    #: Kept so a future change of embedding model can refuse a stale profile
    #: rather than silently comparing vectors that mean different things.
    dimensions: int
    #: Which ways of speaking went into it — normal, quiet, at a distance.
    #: Recorded because cohesion alone cannot tell a good profile from a narrow
    #: one, and the difference is what decides whether the owner is recognised
    #: on a bad morning.
    covers: tuple[str, ...] = ()

    @property
    def quality(self) -> str:
        """How much this profile can be relied on.

        Cohesion is agreement between the takes, and on its own it rewards the
        wrong thing: twelve recordings made in one sitting, one distance, one
        volume agree beautifully and still describe a single way of speaking.
        The first real profile scored 0.84 here and then failed to recognise its
        owner speaking quietly — 0.54 against a threshold of 0.55. Nothing in
        the number gave any warning.

        So coverage is weighed too. A profile that heard only one manner cannot
        be strong however tightly it agrees with itself, and a profile that
        deliberately spans several is judged against a lower bar, because the
        variation it was asked for is the reason its cohesion is lower.
        """
        varied = len(self.covers) >= 2
        if varied:
            if self.cohesion >= 0.70:
                return "strong"
            return "usable" if self.cohesion >= 0.55 else "weak"
        # Narrow: capped, because agreement here only measures how similar the
        # sitting was to itself.
        return "usable" if self.cohesion >= 0.65 else "weak"


class VoiceProfileStore:
    """Reads and writes one profile, encrypted at rest."""

    def __init__(self, path: Path, *, protector: Protector) -> None:
        self._path = Path(path)
        self._protect, self._unprotect = protector

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def save(self, profile: VoiceProfile) -> None:
        payload = json.dumps(
            {
                "embedding": [float(value) for value in profile.embedding],
                "phrases": profile.phrases,
                "cohesion": round(profile.cohesion, 4),
                "created_at": profile.created_at,
                "model": profile.model,
                "dimensions": profile.dimensions,
                "covers": list(profile.covers),
            }
        ).encode("utf-8")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place: a half-written profile
        # that still parses would be a voice nobody can match and no error to
        # explain it.
        temporary = self._path.with_suffix(self._path.suffix + ".partial")
        temporary.write_bytes(self._protect(payload))
        temporary.replace(self._path)

    def load(self) -> VoiceProfile | None:
        if not self._path.is_file():
            return None
        raw = json.loads(self._unprotect(self._path.read_bytes()).decode("utf-8"))
        return VoiceProfile(
            embedding=np.array(raw["embedding"], dtype=np.float32),
            phrases=int(raw["phrases"]),
            cohesion=float(raw["cohesion"]),
            created_at=str(raw["created_at"]),
            model=str(raw["model"]),
            dimensions=int(raw["dimensions"]),
            # Absent from profiles written before coverage was recorded. They
            # were all single-manner, so an empty tuple is the truth about them.
            covers=tuple(str(item) for item in raw.get("covers", ())),
        )

    def delete(self) -> bool:
        """Remove the profile. Returns whether there was one."""
        if not self._path.is_file():
            return False
        self._path.unlink()
        return True


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
