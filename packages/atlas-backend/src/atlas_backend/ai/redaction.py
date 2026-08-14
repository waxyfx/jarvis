"""Redacting tool arguments before they enter the audit trail.

The audit log is readable from the phone and is meant to be read. Tool arguments
are usually innocuous — an application name, a search term — but the model fills
them from whatever the user said, and a user may paste a token into a chat by
accident. One careless message should not put a live credential into a permanent,
append-only record.

Deliberately conservative in one direction only: redacting a harmless value costs
a little context in the trail, while keeping a secret costs the secret.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["MAX_VALUE_LENGTH", "redact_arguments", "redact_text"]

MAX_VALUE_LENGTH = 200
_REDACTED = "[redacted]"

#: Argument names that should never appear in the trail whatever they contain.
_SENSITIVE_NAMES = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|auth|cookie|session)",
    re.IGNORECASE,
)

#: Values that look like credentials regardless of what they are called.
_SENSITIVE_VALUES = (
    # Long unbroken base64/base64url runs.
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),
    # Hex blobs: keys, hashes, tokens.
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # Anything that announces itself.
    re.compile(r"\b(?:bearer|basic)\s+\S+", re.IGNORECASE),
    # Common vendor key prefixes.
    re.compile(r"\b(?:sk|pk|ghp|gho|AIza|AQ\.)[A-Za-z0-9_\-.]{10,}"),
    # Card-like digit runs.
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)


def redact_text(value: str) -> str:
    """Mask credential-shaped substrings and cap the length."""
    redacted = value
    for pattern in _SENSITIVE_VALUES:
        redacted = pattern.sub(_REDACTED, redacted)

    if len(redacted) > MAX_VALUE_LENGTH:
        redacted = f"{redacted[:MAX_VALUE_LENGTH]}… ({len(value)} chars)"
    return redacted


def redact_arguments(args: Any) -> Any:
    """Recursively redact a tool-argument structure for the audit trail.

    Structure is preserved so the trail still shows *what shape* of call was
    made; only values are touched.
    """
    if isinstance(args, dict):
        return {
            key: _REDACTED if _SENSITIVE_NAMES.search(str(key)) else redact_arguments(value)
            for key, value in args.items()
        }
    if isinstance(args, list | tuple):
        # Long collections are summarised: a thousand paths in the trail help
        # nobody, and the count is the interesting part.
        items = list(args)
        if len(items) > 20:
            return [redact_arguments(item) for item in items[:20]] + [
                f"… and {len(items) - 20} more"
            ]
        return [redact_arguments(item) for item in items]
    if isinstance(args, str):
        return redact_text(args)
    return args
