import math
from typing import Any

import pytest

from atlas_shared.canonical import canonical_json, canonical_sha256_hex


def test_key_order_does_not_affect_output() -> None:
    left = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    right = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_json(left) == canonical_json(right)


def test_output_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_non_ascii_is_emitted_literally() -> None:
    # Escaping to \uXXXX would still be valid JSON but would make the byte form
    # depend on the encoder's settings, which is exactly what we cannot have.
    assert canonical_json({"k": "привет"}) == '{"k":"привет"}'.encode()


def test_digest_matches_manual_hash() -> None:
    import hashlib

    payload = {"name": "chrome", "count": 3}
    expected = hashlib.sha256(canonical_json(payload)).hexdigest()
    assert canonical_sha256_hex(payload) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match=r"Out of range|not JSON compliant"):
        canonical_json({"x": value})


@pytest.mark.parametrize(
    "payload",
    [
        {1: "int key"},
        {"nested": {2.5: "float key"}},
        {"in_list": [{None: "none key"}]},
    ],
)
def test_non_string_keys_are_rejected(payload: dict[str, Any]) -> None:
    # json.dumps would silently coerce these to strings, letting two distinct
    # objects canonicalise identically — a signature-collision hazard.
    with pytest.raises(TypeError, match="non-string object key"):
        canonical_json(payload)


def test_empty_containers_round_trip() -> None:
    assert canonical_json({}) == b"{}"
    assert canonical_json({"a": [], "b": {}}) == b'{"a":[],"b":{}}'
