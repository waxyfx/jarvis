"""Deterministic JSON encoding used as input to signatures and hash chains.

Two processes that agree on a value must produce byte-identical output, or every
signature check fails. The rules:

* object keys sorted, no insignificant whitespace;
* UTF-8 output, non-ASCII characters emitted literally (not ``\\uXXXX``);
* NaN and Infinity rejected — they have no JSON representation;
* non-string object keys rejected, because JSON would silently coerce them and
  two different Python dicts could then canonicalise to the same bytes.

Floats round-trip through CPython's shortest-repr algorithm, which is stable
across platforms for IEEE-754 doubles. Even so, prefer integers or strings in
signed payloads: a value that survives a float is one fewer thing to reason
about when a signature fails in production.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "canonical_sha256_hex"]


def canonical_json(value: Any) -> bytes:
    """Serialise ``value`` to canonical UTF-8 JSON bytes."""
    _reject_non_string_keys(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256_hex(value: Any) -> str:
    """Lowercase hex SHA-256 of the canonical encoding of ``value``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _reject_non_string_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string object key at {path}: {key!r} ({type(key).__name__})")
            _reject_non_string_keys(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_non_string_keys(item, f"{path}[{index}]")
