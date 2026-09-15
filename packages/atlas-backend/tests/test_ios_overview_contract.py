"""The keys the iPhone app decodes, against the keys this backend sends.

`apps/ios/JARVIS/Backend.swift` lists every field by hand in `CodingKeys`,
because Swift's decoder is strict: a name that does not match is a decoding
failure, and the app reports "JARVIS answered something this app could not
read" — which points at the backend when the fault is a rename nobody carried
across.

Nothing compiles the Swift here; there is no Mac. What this does is read the
`CodingKeys` back out of the source and compare them with the response models,
so a field renamed on this side fails a test that names the Swift file rather
than surfacing on a phone weeks later.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas_backend.api.assistant import ToolCallSummary
from atlas_backend.api.overview import MachineOut, OverviewOut, PendingOut

SWIFT = Path(__file__).resolve().parents[3] / "apps" / "ios" / "JARVIS" / "Backend.swift"

requires_the_app = pytest.mark.skipif(
    not SWIFT.is_file(), reason="the iOS sources are not in this checkout"
)


def swift_keys(struct: str) -> set[str]:
    """The wire names one Swift struct decodes.

    `case deviceID = "device_id"` gives the wire name; a bare `case name` means
    the property name is the wire name.
    """
    source = SWIFT.read_text(encoding="utf-8")
    block = re.search(rf"struct {struct}[^{{]*\{{(.*?)\n\}}", source, re.DOTALL)
    assert block, f"{struct} is not declared the way this test expects in {SWIFT.name}"

    keys = re.search(r"enum CodingKeys[^{]*\{(.*?)\}", block.group(1), re.DOTALL)
    assert keys, f"{struct} has no CodingKeys"

    found: set[str] = set()
    for line in keys.group(1).splitlines():
        line = line.strip().removeprefix("case ").rstrip(",")
        if not line:
            continue
        for part in line.split(","):
            part = part.strip()
            if "=" in part:
                found.add(part.split("=")[1].strip().strip('"'))
            elif part:
                found.add(part)
    return found


@requires_the_app
class TestThePhoneCanReadWhatIsSent:
    @pytest.mark.parametrize(
        ("struct", "model"),
        [("Machine", MachineOut), ("PendingAction", PendingOut), ("Overview", OverviewOut)],
    )
    def test_every_key_the_app_decodes_is_actually_sent(self, struct: str, model: type) -> None:
        """The failing direction. A key the app expects and the backend does not
        send is a decoding error on the phone, and the message blames the
        backend."""
        sent = set(model.model_fields)
        expected = swift_keys(struct)

        assert expected <= sent, (
            f"{struct} decodes keys this backend does not send: {expected - sent}"
        )

    @pytest.mark.parametrize(
        ("struct", "model"),
        [("Machine", MachineOut), ("PendingAction", PendingOut), ("Overview", OverviewOut)],
    )
    def test_nothing_sent_is_silently_ignored(self, struct: str, model: type) -> None:
        """The quieter direction. A field added here and not to the app is not
        an error — Swift ignores unknown keys — so it would be added, shipped,
        and never shown, with nothing anywhere to say why."""
        sent = set(model.model_fields)
        expected = swift_keys(struct)

        assert sent <= expected, f"{struct} ignores keys this backend sends: {sent - expected}"


@requires_the_app
class TestTheAssistantEndpointToo:
    """`/v1/assistant/message` predates the phone and has its own shape.

    Only the forward direction here: the app deliberately ignores the turn's
    token counts and iteration count, which it has no screen for. What it must
    not do is expect a field that is not sent — which is exactly what it did,
    decoding `call_id` from a payload that has called it `id` since M3.
    """

    def test_every_key_the_app_decodes_is_sent(self) -> None:
        sent = set(ToolCallSummary.model_fields)
        expected = swift_keys("ToolCallSummary")

        assert expected <= sent, f"the app decodes keys that are not sent: {expected - sent}"

    def test_the_identifier_is_named_the_same_on_both_endpoints(self) -> None:
        """Two endpoints naming the same thing differently is how a client ends
        up decoding one and failing on the other."""
        assert "id" in ToolCallSummary.model_fields
        assert "id" in PendingOut.model_fields
