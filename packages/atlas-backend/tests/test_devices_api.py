"""Device inventory and the emergency-disconnect path."""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient

from atlas_shared.enums import DeviceKind, TrustLevel
from tests.conftest import authenticate, pair_device, paired_and_authenticated, requires_db

pytestmark = [requires_db, pytest.mark.integration]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestListing:
    def test_lists_the_callers_devices(self, client: TestClient) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        listed = client.get("/v1/devices", headers=bearer(token)).json()
        assert {device["name"] for device in listed} == {"workstation", "iphone"}
        assert {device["kind"] for device in listed} == {"windows_agent", "ios"}

    def test_public_keys_are_never_returned(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        listed = client.get("/v1/devices", headers=bearer(token)).json()
        assert "public_key" not in listed[0]

    def test_requires_authentication(self, client: TestClient) -> None:
        pair_device(client)
        assert client.get("/v1/devices").status_code == 401


class TestRevocation:
    def test_revoking_marks_the_device(self, client: TestClient) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        phone = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        response = client.post(f"/v1/devices/{phone.device_id}/revoke", headers=bearer(token))
        assert response.status_code == 200
        assert response.json()["trust_level"] == TrustLevel.REVOKED.value
        assert response.json()["revoked_at"] is not None

    def test_revoking_is_idempotent(self, client: TestClient) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        phone = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        first = client.post(f"/v1/devices/{phone.device_id}/revoke", headers=bearer(token))
        second = client.post(f"/v1/devices/{phone.device_id}/revoke", headers=bearer(token))
        assert first.status_code == second.status_code == 200
        assert first.json()["revoked_at"] == second.json()["revoked_at"]

    def test_unknown_device_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        response = client.post(f"/v1/devices/{uuid.uuid4()}/revoke", headers=bearer(token))
        assert response.status_code == 403

    def test_a_revoked_device_cannot_be_restored_by_re_pairing_the_same_key(
        self, client: TestClient
    ) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        phone = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)
        client.post(f"/v1/devices/{phone.device_id}/revoke", headers=bearer(token))

        # The key is still registered, so re-enrolling it is refused: recovery
        # means a new key, not resurrecting the old identity.
        from atlas_shared.auth import pairing_signing_input
        from atlas_shared.crypto import b64u_encode, sign
        from tests.conftest import start_pairing

        started = start_pairing(client, kind=DeviceKind.IOS, name="iphone", bearer=token)
        proof = sign(phone.private_key, pairing_signing_input(started["code"], phone.public_key))
        response = client.post(
            "/v1/pair/complete",
            json={
                "code": started["code"],
                "public_key": b64u_encode(phone.public_key),
                "signature": b64u_encode(proof),
            },
        )
        assert response.status_code == 403


class TestEmergencyDisconnect:
    def test_revoke_all_includes_the_caller(self, client: TestClient) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        response = client.post("/v1/devices/revoke-all", headers=bearer(token))
        assert response.status_code == 200
        assert len(response.json()) == 2
        assert all(device["trust_level"] == TrustLevel.REVOKED.value for device in response.json())

        # The caller has just cut its own access, which is the intent.
        assert client.get("/v1/devices", headers=bearer(token)).status_code == 401

    def test_bootstrap_remains_closed_after_revoke_all(self, client: TestClient) -> None:
        agent = pair_device(client)
        token = authenticate(client, agent)
        client.post("/v1/devices/revoke-all", headers=bearer(token))

        # Devices still exist (revoked, not deleted), so recovery needs the
        # bootstrap token *and* a deliberate operator action, not an automatic
        # re-open of enrolment.
        assert client.get("/v1/pair/status").json() == {"devices": 1, "bootstrap_open": False}
