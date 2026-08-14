"""Path guard: the last word on which files ATLAS may touch.

The server also screens paths, but it cannot see this machine's filesystem — it
does textual prefix matching on strings a model may have produced. Only the
agent can resolve what a path *actually* points at. So this runs last, on the
machine that owns the files, and its answer is final.

What it defends against, in the order the checks run:

* relative paths, which would depend on whatever the working directory happens
  to be;
* UNC and device paths (``\\\\server\\share``, ``\\\\?\\``, ``\\\\.\\``), which
  reach outside the local filesystem or bypass normalisation entirely;
* NTFS alternate data streams (``notes.txt:hidden``), which hide content behind
  a path that looks ordinary;
* reserved DOS device names (``CON``, ``NUL``, ``COM1`` …), which are not files;
* ``..`` traversal;
* **symlinks, junctions and other reparse points that point out of bounds** —
  the reason resolution happens before the boundary check, not after;
* a denylist that applies even inside the allowed roots, because "somewhere I
  said ATLAS may work" is not the same as "everything in there is fair game".
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePath

from atlas_shared.enums import RefusalReason

__all__ = ["PathGuard", "PathRefusedError", "ResolvedPath"]

#: Never accessible, wherever they live. These are not configurable: the point
#: of a floor is that it cannot be lowered from a config file.
_ALWAYS_DENIED: tuple[str, ...] = (
    # ATLAS's own credentials. The assistant must not be able to read the key
    # that authorises it.
    "*/agent_identity*.json",
    "*/atlas_device_key*",
    "*.env",
    "*.env.*",
    # SSH, GPG and cloud credentials.
    "*/.ssh/*",
    "*/.gnupg/*",
    "*/.aws/*",
    "*/.azure/*",
    "*/.config/gcloud/*",
    "*/id_rsa*",
    "*/id_ed25519*",
    # Password managers.
    "*.kdbx",
    "*.kdb",
    "*/1password*",
    "*/bitwarden*",
    # Browser profiles hold session cookies and saved passwords.
    "*/user data/*",
    "*/mozilla/firefox/profiles/*",
    # Private key material in any common encoding.
    "*.pem",
    "*.pfx",
    "*.p12",
    "*.jks",
)

#: Reserved DOS device names. Opening one is never a file operation.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)


class PathRefusedError(Exception):
    """A path was rejected. Carries the reason recorded in the audit trail."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path that passed every check, with what was learned on the way."""

    original: str
    resolved: Path
    root: Path
    #: True when a symlink, junction or other reparse point was followed. The
    #: path is still inside the roots — this is recorded so the audit trail
    #: shows that indirection was involved.
    followed_reparse_point: bool


class PathGuard:
    def __init__(
        self,
        allowed_roots: Iterable[str | os.PathLike[str]],
        *,
        extra_denied: Sequence[str] = (),
    ) -> None:
        resolved_roots: list[Path] = []
        for root in allowed_roots:
            candidate = Path(root).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"allowed root must be absolute: {root!r}")
            resolved_roots.append(_real(candidate))

        if not resolved_roots:
            raise ValueError("at least one allowed root is required")

        self._roots = tuple(resolved_roots)
        self._denied = (*_ALWAYS_DENIED, *(pattern.lower() for pattern in extra_denied))

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def check(self, raw_path: str) -> ResolvedPath:
        """Validate ``raw_path``, or raise :class:`PathRefusedError`."""
        if not raw_path or not raw_path.strip():
            raise PathRefusedError(RefusalReason.ARGS_INVALID, "empty path")

        text = raw_path.strip()
        self._reject_special_prefixes(text)

        candidate = Path(text)
        if not candidate.is_absolute():
            raise PathRefusedError(
                RefusalReason.PATH_OUTSIDE_ROOTS,
                "path must be absolute; a relative path depends on the working directory",
            )

        self._reject_alternate_data_stream(candidate)
        self._reject_reserved_names(candidate)

        # Resolution happens *before* the boundary check. Checking first and
        # resolving after is the classic mistake: a junction inside an allowed
        # root would pass the check and then land anywhere on the disk.
        resolved = _real(candidate)
        followed = resolved != _lexical(candidate)

        root = self._containing_root(resolved)
        if root is None:
            detail = (
                "resolves outside the allowed directories"
                if followed
                else "is outside the allowed directories"
            )
            raise PathRefusedError(RefusalReason.PATH_OUTSIDE_ROOTS, f"path {detail}")

        self._reject_denylisted(resolved)

        return ResolvedPath(
            original=raw_path,
            resolved=resolved,
            root=root,
            followed_reparse_point=followed,
        )

    def is_allowed(self, raw_path: str) -> bool:
        try:
            self.check(raw_path)
        except PathRefusedError:
            return False
        return True

    # ------------------------------------------------------------------ checks

    def _reject_special_prefixes(self, text: str) -> None:
        normalised = text.replace("/", "\\")
        if normalised.startswith("\\\\"):
            raise PathRefusedError(
                RefusalReason.PATH_OUTSIDE_ROOTS,
                "UNC and device paths are refused; they reach outside the local filesystem "
                "or skip normalisation",
            )

    def _reject_alternate_data_stream(self, candidate: PurePath) -> None:
        # A drive letter legitimately contains one colon, in the first component.
        for part in candidate.parts[1:]:
            if ":" in part:
                raise PathRefusedError(
                    RefusalReason.PATH_DENYLISTED,
                    "alternate data streams are refused",
                )

    def _reject_reserved_names(self, candidate: PurePath) -> None:
        for part in candidate.parts:
            stem = part.split(".")[0].strip().lower()
            if stem in _RESERVED_NAMES:
                raise PathRefusedError(
                    RefusalReason.PATH_DENYLISTED,
                    f"'{part}' is a reserved device name, not a file",
                )

    def _containing_root(self, resolved: Path) -> Path | None:
        for root in self._roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return root
        return None

    def _reject_denylisted(self, resolved: Path) -> None:
        # Compared with forward slashes so one pattern works regardless of how
        # the caller wrote the separators.
        haystack = resolved.as_posix().lower()
        for pattern in self._denied:
            if fnmatch(haystack, pattern) or fnmatch(haystack, f"{pattern}/*"):
                raise PathRefusedError(
                    RefusalReason.PATH_DENYLISTED,
                    "path matches a protected pattern and is never accessible",
                )


def _real(path: Path) -> Path:
    """Fully resolve, following reparse points, without requiring existence.

    ``strict=False`` matters: a file the agent is about to *create* does not
    exist yet, but its parent directory still has to be inside the roots.
    """
    return Path(os.path.normcase(os.path.realpath(path)))


def _lexical(path: Path) -> Path:
    """Normalise without touching the filesystem.

    Compared against the real path to detect that a reparse point was followed.
    """
    return Path(os.path.normcase(os.path.normpath(os.path.abspath(path))))


def contains_reparse_point(path: Path) -> bool:
    """Whether any existing component of ``path`` is a reparse point.

    Used for reporting, not for the decision: the boundary check already covers
    where a reparse point leads. Always ``False`` off Windows.
    """
    if sys.platform != "win32":
        return False

    current = path
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        try:
            # Windows-only attribute; getattr keeps this type-checkable on both
            # platforms without a per-platform ignore.
            attributes = getattr(os.lstat(current), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        if current.parent == current:
            return False
        current = current.parent
    return False
