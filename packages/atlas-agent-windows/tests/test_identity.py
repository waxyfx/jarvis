"""Identity storage: the agent's only long-lived credential."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from atlas_agent.identity import DeviceIdentity, IdentityStore, IdentityStoreError
from atlas_shared.crypto import b64u_encode, generate_keypair, public_key_for

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@pytest.fixture
def store(tmp_path: Path) -> IdentityStore:
    # Off Windows there is no DPAPI, so the development escape hatch is used to
    # keep these tests runnable; the protection itself is covered separately.
    return IdentityStore(tmp_path / "identity.json", allow_plaintext=sys.platform != "win32")


class TestRoundTrip:
    def test_missing_file_reads_as_none(self, store: IdentityStore) -> None:
        assert store.exists() is False
        assert store.load() is None

    def test_created_identity_is_a_valid_keypair(self, store: IdentityStore) -> None:
        identity = store.create()
        assert public_key_for(identity.private_key) == identity.public_key
        assert identity.is_enrolled is False

    def test_save_and_load(self, store: IdentityStore) -> None:
        identity = store.create().with_device_id("11111111-1111-1111-1111-111111111111")
        store.save(identity)

        loaded = store.load()
        assert loaded is not None
        assert loaded.private_key == identity.private_key
        assert loaded.public_key == identity.public_key
        assert loaded.device_id == identity.device_id
        assert loaded.is_enrolled is True

    def test_saving_twice_replaces_atomically(self, store: IdentityStore) -> None:
        store.save(store.create().with_device_id("a"))
        second = store.create().with_device_id("b")
        store.save(second)

        loaded = store.load()
        assert loaded is not None
        assert loaded.device_id == "b"
        assert loaded.private_key == second.private_key
        assert not store.path.with_suffix(store.path.suffix + ".tmp").exists()

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        nested = IdentityStore(
            tmp_path / "a" / "b" / "identity.json", allow_plaintext=sys.platform != "win32"
        )
        nested.save(nested.create())
        assert nested.exists()


class TestFileIntegrity:
    def test_unreadable_file_raises(self, store: IdentityStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("not json", encoding="utf-8")
        with pytest.raises(IdentityStoreError, match="unreadable"):
            store.load()

    def test_unknown_version_raises(self, store: IdentityStore) -> None:
        store.save(store.create())
        document = json.loads(store.path.read_text(encoding="utf-8"))
        document["version"] = 99
        store.path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(IdentityStoreError, match="unsupported identity file version"):
            store.load()

    def test_unknown_protection_raises(self, store: IdentityStore) -> None:
        store.save(store.create())
        document = json.loads(store.path.read_text(encoding="utf-8"))
        document["protection"] = "rot13"
        store.path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(IdentityStoreError, match="unknown key protection"):
            store.load()

    def test_mismatched_public_key_raises(self, store: IdentityStore) -> None:
        store.save(store.create())
        document = json.loads(store.path.read_text(encoding="utf-8"))
        _, other_public = generate_keypair()
        document["public_key"] = b64u_encode(other_public)
        store.path.write_text(json.dumps(document), encoding="utf-8")

        # An edited file must fail closed: we cannot tell tampering from
        # corruption, and both mean "do not use this key".
        with pytest.raises(IdentityStoreError, match="does not match"):
            store.load()


class TestPlaintextPolicy:
    def test_plaintext_storage_is_refused_by_default_off_windows(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("DPAPI is available, so plaintext is never reached")

        strict = IdentityStore(tmp_path / "identity.json", allow_plaintext=False)
        with pytest.raises(IdentityStoreError, match="plaintext key storage is disabled"):
            strict.save(strict.create())

    def test_plaintext_file_is_refused_when_not_allowed(self, tmp_path: Path) -> None:
        permissive = IdentityStore(tmp_path / "identity.json", allow_plaintext=True)
        identity = permissive.create()
        if sys.platform == "win32":
            # Force the plaintext form that a POSIX machine would have written.
            permissive.path.parent.mkdir(parents=True, exist_ok=True)
            permissive.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "protection": "plaintext",
                        "device_id": None,
                        "public_key": b64u_encode(identity.public_key),
                        "private_key": b64u_encode(identity.private_key),
                    }
                ),
                encoding="utf-8",
            )
        else:
            permissive.save(identity)

        strict = IdentityStore(permissive.path, allow_plaintext=False)
        with pytest.raises(IdentityStoreError, match="not allowed"):
            strict.load()


@on_windows
class TestWindowsProtection:
    def test_key_is_encrypted_on_disk(self, tmp_path: Path) -> None:
        store = IdentityStore(tmp_path / "identity.json", allow_plaintext=False)
        identity = store.create()
        store.save(identity)

        document = json.loads(store.path.read_text(encoding="utf-8"))
        assert document["protection"] == "dpapi"
        # The raw private key must not be recoverable from the file contents.
        assert b64u_encode(identity.private_key) not in store.path.read_text(encoding="utf-8")

    def test_dpapi_round_trip(self, tmp_path: Path) -> None:
        store = IdentityStore(tmp_path / "identity.json", allow_plaintext=False)
        identity = store.create().with_device_id("device-1")
        store.save(identity)

        loaded = store.load()
        assert loaded is not None
        assert loaded.private_key == identity.private_key


def test_with_device_id_does_not_mutate() -> None:
    identity = DeviceIdentity(private_key=b"\x01" * 32, public_key=b"\x02" * 32)
    updated = identity.with_device_id("abc")
    assert identity.device_id is None
    assert updated.device_id == "abc"
