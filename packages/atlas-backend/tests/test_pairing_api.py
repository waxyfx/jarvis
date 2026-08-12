"""Enrolment: the only path by which a device gains any access at all."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from atlas_shared.auth import pairing_signing_input
from atlas_shared.crypto import b64u_encode, generate_keypair, sign
from atlas_shared.enums import DeviceKind
from tests.conftest import (
    TEST_BOOTSTRAP_TOKEN,
    authenticate,
    pair_device,
    requires_db,
    run_sql,
    start_pairing,
)

pytestmark = [requires_db, pytest.mark.integration]


def complete_pairing(client: TestClient, code: str, private_key: bytes, public_key: bytes):  # type: ignore[no-untyped-def]
    proof = sign(private_key, pairing_signing_input(code, public_key))
    return client.post(
        "/v1/pair/complete",
        json={
            "code": code,
            "public_key": b64u_encode(public_key),
            "signature": b64u_encode(proof),
        },
    )


class TestBootstrap:
    def test_status_reports_bootstrap_open_initially(self, client: TestClient) -> None:
        response = client.get("/v1/pair/status")
        assert response.json() == {"devices": 0, "bootstrap_open": True}

    def test_first_device_can_pair_with_the_bootstrap_token(self, client: TestClient) -> None:
        device = pair_device(client)
        assert device.device_id

        status = client.get("/v1/pair/status").json()
        assert status == {"devices": 1, "bootstrap_open": False}

    def test_bootstrap_closes_once_a_device_exists(self, client: TestClient) -> None:
        pair_device(client)

        response = client.post(
            "/v1/pair/start",
            json={"kind": DeviceKind.IOS.value, "name": "iphone"},
            headers={"X-Atlas-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN},
        )
        assert response.status_code == 403
        assert "bootstrap pairing is closed" in response.json()["message"]

    def test_wrong_bootstrap_token_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/v1/pair/start",
            json={"kind": DeviceKind.WINDOWS_AGENT.value, "name": "x"},
            headers={"X-Atlas-Bootstrap-Token": "wrong"},
        )
        assert response.status_code == 401

    def test_no_credentials_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/v1/pair/start", json={"kind": DeviceKind.WINDOWS_AGENT.value, "name": "x"}
        )
        assert response.status_code == 401


class TestTrustedDeviceInitiation:
    def test_paired_device_can_enrol_the_next_one(self, client: TestClient) -> None:
        first = pair_device(client)
        token = authenticate(client, first)

        second = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)
        assert second.device_id != first.device_id
        assert client.get("/v1/pair/status").json()["devices"] == 2

    def test_revoked_device_cannot_enrol(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        client.post(
            f"/v1/devices/{device.device_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )

        response = client.post(
            "/v1/pair/start",
            json={"kind": DeviceKind.IOS.value, "name": "iphone"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


class TestCodeHandling:
    def test_code_is_single_use(self, client: TestClient) -> None:
        started = start_pairing(client)
        private_a, public_a = generate_keypair()
        assert complete_pairing(client, started["code"], private_a, public_a).status_code == 201

        private_b, public_b = generate_keypair()
        second = complete_pairing(client, started["code"], private_b, public_b)
        assert second.status_code == 401

    def test_expired_code_is_rejected(self, client: TestClient) -> None:
        started = start_pairing(client)
        run_sql("UPDATE pairing_codes SET expires_at = now() - interval '1 hour'")

        private_key, public_key = generate_keypair()
        assert complete_pairing(client, started["code"], private_key, public_key).status_code == 401

    def test_unknown_code_is_rejected(self, client: TestClient) -> None:
        private_key, public_key = generate_keypair()
        assert complete_pairing(client, "ZZZZZZZZ", private_key, public_key).status_code == 401

    @pytest.mark.parametrize("separator", ["-", " ", ""])
    def test_human_formatting_is_accepted(self, client: TestClient, separator: str) -> None:
        started = start_pairing(client)
        code = started["code"]
        typed = f"{code[:4]}{separator}{code[4:]}".lower()

        private_key, public_key = generate_keypair()
        assert complete_pairing(client, typed, private_key, public_key).status_code == 201

    @pytest.mark.parametrize("bad_code", ["SHORT", "TOOLONGCODE12", "IIIIOOOO"])
    def test_malformed_code_is_a_client_error(self, client: TestClient, bad_code: str) -> None:
        start_pairing(client)
        private_key, public_key = generate_keypair()
        # A signature cannot be formed over a code that fails normalisation, so
        # send a syntactically valid but irrelevant one: the server must reject
        # on the code itself, before it ever looks at the proof.
        response = client.post(
            "/v1/pair/complete",
            json={
                "code": bad_code,
                "public_key": b64u_encode(public_key),
                "signature": b64u_encode(sign(private_key, b"irrelevant")),
            },
        )
        assert response.status_code == 400


class TestProofOfPossession:
    def test_signature_from_a_different_key_is_rejected(self, client: TestClient) -> None:
        started = start_pairing(client)
        attacker_private, _ = generate_keypair()
        _, victim_public = generate_keypair()

        # Signing the right code with the wrong key must not enrol the key that
        # was presented — this is what stops a code interceptor.
        response = complete_pairing(client, started["code"], attacker_private, victim_public)
        assert response.status_code == 401

    def test_signature_over_a_different_code_is_rejected(self, client: TestClient) -> None:
        started = start_pairing(client)
        private_key, public_key = generate_keypair()
        wrong = sign(private_key, pairing_signing_input("ABCDEFGH", public_key))

        response = client.post(
            "/v1/pair/complete",
            json={
                "code": started["code"],
                "public_key": b64u_encode(public_key),
                "signature": b64u_encode(wrong),
            },
        )
        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("field", "value"),
        [("public_key", "AAAA"), ("signature", "AAAA"), ("public_key", "!!!not base64")],
    )
    def test_malformed_binary_fields_are_rejected(
        self, client: TestClient, field: str, value: str
    ) -> None:
        started = start_pairing(client)
        private_key, public_key = generate_keypair()
        proof = sign(private_key, pairing_signing_input(started["code"], public_key))
        body = {
            "code": started["code"],
            "public_key": b64u_encode(public_key),
            "signature": b64u_encode(proof),
        }
        body[field] = value
        assert client.post("/v1/pair/complete", json=body).status_code == 422

    def test_a_key_cannot_be_enrolled_twice(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        started = start_pairing(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        response = complete_pairing(client, started["code"], device.private_key, device.public_key)
        assert response.status_code == 403
        assert "already registered" in response.json()["message"]


class TestIntendedKind:
    def test_device_is_enrolled_as_the_kind_the_initiator_chose(self, client: TestClient) -> None:
        # The enrolling device does not get to pick its own kind, so a leaked
        # code cannot be redeemed as a more privileged device type.
        started = start_pairing(client, kind=DeviceKind.IOS, name="iphone")
        private_key, public_key = generate_keypair()
        response = complete_pairing(client, started["code"], private_key, public_key)

        assert response.status_code == 201
        assert response.json()["kind"] == DeviceKind.IOS.value
        assert response.json()["name"] == "iphone"
