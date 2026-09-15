"""The iPhone app signs by hand. This is what stops it drifting.

`apps/ios/JARVIS/Identity.swift` builds the same two signing inputs this package
builds, in Swift, from constants copied by eye. Nothing compiles that file here
— there is no Mac in this project — so the risk is not that the Swift is wrong
today but that the Python changes tomorrow and nobody looks.

A changed domain string or separator produces a signature that verifies against
nothing, and the symptom is "the phone cannot pair", which points at the phone.
So the constants are read back out of the Swift source and compared. It is a
crude check and it is the one that would have caught the real mistake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas_shared.auth import challenge_signing_input, normalise_pairing_code, pairing_signing_input

SWIFT = Path(__file__).resolve().parents[3] / "apps" / "ios" / "JARVIS" / "Identity.swift"

requires_the_app = pytest.mark.skipif(
    not SWIFT.is_file(), reason="the iOS sources are not in this checkout"
)


def swift_constant(name: str) -> str:
    """The string literal assigned to ``name`` in the Swift file."""
    found = re.search(rf'{name}\s*=\s*Data\("([^"]+)"\.utf8\)', SWIFT.read_text(encoding="utf-8"))
    assert found, f"{name} is not declared the way this test expects in {SWIFT.name}"
    return found.group(1)


@requires_the_app
class TestTheDomainsMatch:
    def test_the_pairing_domain_is_the_same_string(self) -> None:
        """Bound into every enrolment signature. A mismatch means the backend
        rejects a signature the phone believes it computed correctly."""
        produced = pairing_signing_input("4F2K9X1M", b"\x00" * 32)

        assert produced.split(b"\x1f")[0].decode() == swift_constant("pairingDomain")

    def test_the_challenge_domain_is_the_same_string(self) -> None:
        produced = challenge_signing_input("a-device", b"\x01" * 32)

        assert produced.split(b"\x1f")[0].decode() == swift_constant("challengeDomain")

    def test_the_separator_is_the_same_byte(self) -> None:
        """0x1f, and the Swift writes it as a number rather than a name."""
        source = SWIFT.read_text(encoding="utf-8")

        assert "Data([0x1f])" in source
        assert b"\x1f" in pairing_signing_input("4F2K9X1M", b"\x00" * 32)

    def test_the_two_domains_are_still_different(self) -> None:
        """The whole point of having domains: a signature made for one purpose
        must never verify as a signature for the other."""
        assert swift_constant("pairingDomain") != swift_constant("challengeDomain")


@requires_the_app
class TestTheCodeIsNormalisedTheSameWay:
    @pytest.mark.parametrize(
        "typed", ["4F2K-9X1M", "4f2k 9x1m", "4F2K9X1M", " 4f2k—9x1m ".replace("—", "-")]
    )
    def test_python_accepts_what_a_person_types(self, typed: str) -> None:
        assert normalise_pairing_code(typed) == "4F2K9X1M"

    def test_the_swift_strips_the_same_characters(self) -> None:
        """Swift's `isLetter || isNumber` and Python's `str.isalnum` agree on
        everything a pairing code can contain — the alphabet is Crockford
        base32, which is ASCII. They diverge on exotic scripts, which cannot
        appear here."""
        source = SWIFT.read_text(encoding="utf-8")

        assert "uppercased()" in source
        assert "$0.isLetter || $0.isNumber" in source
