"""Device identity storage.

The agent's Ed25519 private key is the credential that lets this machine be
commanded. It is generated locally, never transmitted, and stored encrypted with
**DPAPI** under the current Windows user account — so a copy of the file is
useless on another machine or under another user.

Off Windows there is no DPAPI. Rather than silently writing the key in the
clear, the store refuses unless the operator explicitly opts in; a development
convenience should not quietly become a production weakness.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from atlas_shared.crypto import KEY_SIZE, b64u_decode, b64u_encode, generate_keypair, public_key_for

__all__ = ["DeviceIdentity", "IdentityStore", "IdentityStoreError"]

_FORMAT_VERSION = 1
_DPAPI_DESCRIPTION = "ATLAS agent device key"

#: Held as a plain bool so that both branches stay reachable to a type checker
#: running on either platform — this code has to be correct on both.
_IS_WINDOWS: bool = sys.platform == "win32"


class IdentityStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    private_key: bytes
    public_key: bytes
    #: Assigned by the backend at enrolment; absent until then.
    device_id: str | None = None

    def with_device_id(self, device_id: str) -> DeviceIdentity:
        return DeviceIdentity(
            private_key=self.private_key, public_key=self.public_key, device_id=device_id
        )

    @property
    def is_enrolled(self) -> bool:
        return self.device_id is not None


class IdentityStore:
    def __init__(self, path: Path, *, allow_plaintext: bool = False) -> None:
        self._path = path
        self._allow_plaintext = allow_plaintext

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def create(self) -> DeviceIdentity:
        """Generate a fresh keypair. Does not write anything."""
        private_key, public_key = generate_keypair()
        return DeviceIdentity(private_key=private_key, public_key=public_key)

    def load(self) -> DeviceIdentity | None:
        if not self._path.exists():
            return None

        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityStoreError(f"identity file is unreadable: {exc}") from exc

        version = document.get("version")
        if version != _FORMAT_VERSION:
            raise IdentityStoreError(f"unsupported identity file version: {version!r}")

        protection = document.get("protection")
        blob = b64u_decode(document["private_key"])

        if protection == "dpapi":
            private_key = _dpapi_unprotect(blob)
        elif protection == "plaintext":
            if not self._allow_plaintext:
                raise IdentityStoreError(
                    "identity file holds an unprotected key but plaintext keys are "
                    "not allowed; re-pair on a machine with DPAPI"
                )
            private_key = blob
        else:
            raise IdentityStoreError(f"unknown key protection: {protection!r}")

        if len(private_key) != KEY_SIZE:
            raise IdentityStoreError("stored private key has the wrong length")

        public_key = public_key_for(private_key)
        stored_public = b64u_decode(document["public_key"])
        if public_key != stored_public:
            # Either the file was edited or the wrong key decrypted; refusing is
            # the only safe reading.
            raise IdentityStoreError("stored public key does not match the private key")

        return DeviceIdentity(
            private_key=private_key,
            public_key=public_key,
            device_id=document.get("device_id"),
        )

    def save(self, identity: DeviceIdentity) -> None:
        protection, blob = self._protect(identity.private_key)
        document = {
            "version": _FORMAT_VERSION,
            "protection": protection,
            "device_id": identity.device_id,
            "public_key": b64u_encode(identity.public_key),
            "private_key": b64u_encode(blob),
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        _restrict_permissions(temporary)
        # Atomic replace: a crash mid-write can never leave a half-written key.
        temporary.replace(self._path)
        _restrict_permissions(self._path)

    def _protect(self, private_key: bytes) -> tuple[str, bytes]:
        if _IS_WINDOWS:
            return "dpapi", _dpapi_protect(private_key)
        if not self._allow_plaintext:
            raise IdentityStoreError(
                "DPAPI is unavailable on this platform and plaintext key storage is "
                "disabled; set allow_plaintext_key only for local development"
            )
        return "plaintext", private_key


def _dpapi_protect(data: bytes) -> bytes:
    try:
        import win32crypt
    except ImportError as exc:  # pragma: no cover - Windows-only path
        raise IdentityStoreError("pywin32 is required to protect the device key") from exc

    # User-scoped: only this Windows account, on this machine, can decrypt.
    return bytes(win32crypt.CryptProtectData(data, _DPAPI_DESCRIPTION, None, None, None, 0))


def _dpapi_unprotect(blob: bytes) -> bytes:
    try:
        import win32crypt
    except ImportError as exc:  # pragma: no cover - Windows-only path
        raise IdentityStoreError("pywin32 is required to read the device key") from exc

    try:
        _, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    except Exception as exc:
        raise IdentityStoreError(
            "the device key could not be decrypted; it belongs to another Windows user or machine"
        ) from exc
    return bytes(data)


def _restrict_permissions(path: Path) -> None:
    """Best-effort narrowing of file permissions.

    On Windows the file inherits the user profile's ACL and DPAPI is the real
    protection; on POSIX this is what keeps a development key private.
    """
    if not _IS_WINDOWS:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
