"""REST calls the agent makes: enrolment and authentication."""

from __future__ import annotations

import httpx

from atlas_agent.config import AgentSettings
from atlas_agent.identity import DeviceIdentity
from atlas_shared.auth import challenge_signing_input, pairing_signing_input
from atlas_shared.crypto import b64u_decode, b64u_encode, sign

__all__ = ["BackendClient", "BackendError", "EnrolmentRefusedError"]


class BackendError(RuntimeError):
    """The backend could not be reached, or answered with an error."""


class EnrolmentRefusedError(BackendError):
    """The backend rejected the pairing attempt. Retrying will not help."""


class BackendClient:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.backend_url,
            timeout=self._settings.request_timeout_s,
            verify=self._settings.verify_tls,
        )

    async def pairing_status(self) -> dict[str, object]:
        async with self._client() as client:
            response = await _get(client, "/v1/pair/status")
        return dict(response.json())

    async def enrol(self, identity: DeviceIdentity, code: str) -> DeviceIdentity:
        """Redeem a pairing code, binding this key to a new device record.

        The proof signs the code together with our own public key, so a party
        who intercepts the code cannot enrol a key of their own in its place.
        """
        proof = sign(identity.private_key, pairing_signing_input(code, identity.public_key))

        async with self._client() as client:
            try:
                response = await client.post(
                    "/v1/pair/complete",
                    json={
                        "code": code,
                        "public_key": b64u_encode(identity.public_key),
                        "signature": b64u_encode(proof),
                    },
                )
            except httpx.HTTPError as exc:
                raise BackendError(f"could not reach the backend: {exc}") from exc

        if response.status_code != 201:
            raise EnrolmentRefusedError(_describe(response))

        body = response.json()
        pinned = body.get("server_public_key")
        if not pinned:
            raise EnrolmentRefusedError(
                "the backend did not return a server public key; it is running a "
                "version older than M2 and its commands could not be verified"
            )
        return identity.enrolled_as(str(body["device_id"]), b64u_decode(pinned))

    async def authenticate(self, identity: DeviceIdentity) -> str:
        """Complete challenge/response and return a bearer token."""
        if identity.device_id is None:
            raise BackendError("this agent is not paired yet")

        async with self._client() as client:
            try:
                challenge = await client.post(
                    "/v1/auth/challenge", json={"device_id": identity.device_id}
                )
                if challenge.status_code != 200:
                    raise BackendError(f"challenge refused: {_describe(challenge)}")

                nonce = str(challenge.json()["nonce"])
                signature = sign(
                    identity.private_key,
                    challenge_signing_input(identity.device_id, b64u_decode(nonce)),
                )

                token = await client.post(
                    "/v1/auth/token",
                    json={
                        "device_id": identity.device_id,
                        "nonce": nonce,
                        "signature": b64u_encode(signature),
                    },
                )
            except httpx.HTTPError as exc:
                raise BackendError(f"could not reach the backend: {exc}") from exc

        if token.status_code != 200:
            raise BackendError(f"token refused: {_describe(token)}")
        return str(token.json()["access_token"])


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    try:
        response = await client.get(path)
    except httpx.HTTPError as exc:
        raise BackendError(f"could not reach the backend: {exc}") from exc
    if response.status_code != 200:
        raise BackendError(_describe(response))
    return response


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("message") or body.get("detail") or response.text
    except ValueError:
        detail = response.text
    return f"HTTP {response.status_code}: {detail}"
