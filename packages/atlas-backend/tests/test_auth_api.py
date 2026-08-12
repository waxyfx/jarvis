"""Challenge/response authentication and token handling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from starlette.testclient import TestClient

from atlas_shared.crypto import b64u_encode, generate_keypair, sign
from atlas_shared.enums import DeviceKind
from atlas_shared.protocol.errors import AtlasProtocolError
from tests.conftest import (
    TEST_JWT_SECRET,
    authenticate,
    pair_device,
    requires_db,
    run_sql,
)

pytestmark = [requires_db, pytest.mark.integration]


def get_challenge(client: TestClient, device_id: str) -> str:
    response = client.post("/v1/auth/challenge", json={"device_id": device_id})
    assert response.status_code == 200, response.text
    return str(response.json()["nonce"])


class TestHappyPath:
    def test_signed_challenge_yields_a_token(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        assert token

    def test_token_authorises_a_protected_endpoint(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)

        response = client.get("/v1/devices", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()[0]["id"] == device.device_id

    def test_tokens_can_be_reissued(self, client: TestClient) -> None:
        device = pair_device(client)
        assert authenticate(client, device) != ""
        assert authenticate(client, device) != ""


class TestChallengeIntegrity:
    def test_wrong_signature_is_rejected(self, client: TestClient) -> None:
        device = pair_device(client)
        nonce = get_challenge(client, device.device_id)
        other_private, _ = generate_keypair()

        response = client.post(
            "/v1/auth/token",
            json={
                "device_id": device.device_id,
                "nonce": nonce,
                "signature": b64u_encode(sign(other_private, b"anything")),
            },
        )
        assert response.status_code == 401

    def test_challenge_is_single_use(self, client: TestClient) -> None:
        device = pair_device(client)
        nonce = get_challenge(client, device.device_id)
        body = {
            "device_id": device.device_id,
            "nonce": nonce,
            "signature": device.sign_challenge(nonce),
        }
        assert client.post("/v1/auth/token", json=body).status_code == 200

        replayed = client.post("/v1/auth/token", json=body)
        assert replayed.status_code == 409

    def test_expired_challenge_is_rejected(self, client: TestClient) -> None:
        device = pair_device(client)
        nonce = get_challenge(client, device.device_id)
        run_sql("UPDATE auth_challenges SET expires_at = now() - interval '1 hour'")

        response = client.post(
            "/v1/auth/token",
            json={
                "device_id": device.device_id,
                "nonce": nonce,
                "signature": device.sign_challenge(nonce),
            },
        )
        assert response.status_code == 401

    def test_challenge_belonging_to_another_device_is_rejected(self, client: TestClient) -> None:
        first = pair_device(client)
        token = authenticate(client, first)
        second = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=token)

        nonce = get_challenge(client, first.device_id)
        response = client.post(
            "/v1/auth/token",
            json={
                "device_id": second.device_id,
                "nonce": nonce,
                "signature": second.sign_challenge(nonce),
            },
        )
        assert response.status_code == 401

    def test_unknown_device_is_rejected(self, client: TestClient) -> None:
        response = client.post("/v1/auth/challenge", json={"device_id": str(uuid.uuid4())})
        assert response.status_code == 401


class TestRevocation:
    def test_revoked_device_cannot_obtain_a_challenge(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        client.post(
            f"/v1/devices/{device.device_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )

        response = client.post("/v1/auth/challenge", json={"device_id": device.device_id})
        assert response.status_code == 401

    def test_existing_token_stops_working_immediately(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/devices", headers=headers).status_code == 200

        client.post(f"/v1/devices/{device.device_id}/revoke", headers=headers)

        # The token is still cryptographically valid; revocation must bite anyway.
        assert client.get("/v1/devices", headers=headers).status_code == 401


class TestBearerHandling:
    @pytest.mark.parametrize(
        "header",
        ["", "Bearer", "Bearer ", "Basic abc", "abc", "bearer"],
    )
    def test_malformed_authorization_header(self, client: TestClient, header: str) -> None:
        pair_device(client)
        response = client.get("/v1/devices", headers={"Authorization": header})
        assert response.status_code == 401

    def test_missing_header(self, client: TestClient) -> None:
        pair_device(client)
        assert client.get("/v1/devices").status_code == 401

    def test_token_signed_with_another_secret_is_rejected(self, client: TestClient) -> None:
        device = pair_device(client)
        forged = jwt.encode(
            {
                "iss": "atlas-backend",
                "aud": "atlas-device",
                "sub": device.device_id,
                "uid": str(uuid.uuid4()),
                "knd": "windows_agent",
                "trt": "trusted",
                "jti": "01J000000000000000000000AA",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "a-different-secret-of-adequate-length-for-hs256",
            algorithm="HS256",
        )
        response = client.get("/v1/devices", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401


class TestTokenServiceUnit:
    """Cases that are awkward to reach through HTTP but must still hold."""

    def test_expired_token_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_backend.auth.tokens import TokenService

        service = TokenService(settings)
        expired = jwt.encode(
            {
                "iss": "atlas-backend",
                "aud": "atlas-device",
                "sub": str(uuid.uuid4()),
                "uid": str(uuid.uuid4()),
                "knd": "ios",
                "trt": "trusted",
                "jti": "01J000000000000000000000AA",
                "iat": int((datetime.now(UTC) - timedelta(hours=3)).timestamp()),
                "exp": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(AtlasProtocolError, match="expired"):
            service.verify(expired)

    def test_algorithm_confusion_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_backend.auth.tokens import TokenService

        # An unsigned token must never be accepted, whatever it claims.
        unsigned = jwt.encode({"sub": str(uuid.uuid4())}, key="", algorithm="none")
        with pytest.raises(AtlasProtocolError):
            TokenService(settings).verify(unsigned)

    @pytest.mark.parametrize("garbage", ["", "not.a.token", "a.b.c", "..", "x" * 200])
    def test_garbage_tokens_are_rejected(  # type: ignore[no-untyped-def]
        self, settings, garbage: str
    ) -> None:
        from atlas_backend.auth.tokens import TokenService

        with pytest.raises(AtlasProtocolError):
            TokenService(settings).verify(garbage)
