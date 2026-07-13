"""Secret-store interface tests (SEC-005): reference parsing is strict,
values round-trip through the local adapters, rotation semantics
(immutable references, fresh names), provider confinement, the env
store's read-only stance, and the file store's permissions and
containment."""

import os
from pathlib import Path

import pytest

from soa_config import (
    EnvSecretStore,
    FileSecretStore,
    MemorySecretStore,
    SecretNotFoundError,
    SecretReference,
    SecretStore,
    SecretStoreError,
    SecretStoreReadOnlyError,
)


class TestSecretReference:
    def test_roundtrip_and_shape(self) -> None:
        reference = SecretReference.parse("secretref://memory/orgs/1/creds/2")
        assert (reference.provider, reference.name) == ("memory", "orgs/1/creds/2")
        assert str(reference) == "secretref://memory/orgs/1/creds/2"

    @pytest.mark.parametrize(
        "raw",
        [
            "whsec_a_raw_secret_value",  # a value is never a reference
            "secretref://memory/",  # empty name
            "secretref:///name",  # empty provider
            "secretref://memory/../etc/passwd",  # traversal
            "secretref://memory//absolute",  # empty first segment
            "secretref://MEMORY/name",  # provider casing is strict
            "secretref://memory/" + "a" * 600,  # unbounded names
        ],
    )
    def test_malformed_references_are_refused(self, raw: str) -> None:
        with pytest.raises(ValueError):
            SecretReference.parse(raw)


class TestMemoryStore:
    async def test_roundtrip_rotation_and_revocation(self) -> None:
        store = MemorySecretStore()
        assert isinstance(store, SecretStore)
        reference = await store.put("orgs/1/creds/a", "value-one")
        assert await store.resolve(reference) == "value-one"
        # References are immutable: the same name cannot be re-written.
        with pytest.raises(SecretStoreError, match="new name"):
            await store.put("orgs/1/creds/a", "value-two")
        await store.revoke(reference)
        await store.revoke(reference)  # idempotent
        with pytest.raises(SecretNotFoundError):
            await store.resolve(reference)

    async def test_provider_confinement(self) -> None:
        store = MemorySecretStore()
        foreign = SecretReference(provider="aws-secrets-manager", name="x")
        with pytest.raises(SecretStoreError, match="refusing to resolve across providers"):
            await store.resolve(foreign)


class TestEnvStore:
    async def test_resolves_platform_variables_and_refuses_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = EnvSecretStore()
        monkeypatch.setenv("SOA_TEST_PLATFORM_SECRET", "from-the-environment")
        reference = SecretReference.parse("secretref://env/SOA_TEST_PLATFORM_SECRET")
        assert await store.resolve(reference) == "from-the-environment"
        monkeypatch.delenv("SOA_TEST_PLATFORM_SECRET")
        with pytest.raises(SecretNotFoundError):
            await store.resolve(reference)
        with pytest.raises(SecretStoreReadOnlyError):
            await store.put("ANYTHING", "value")
        with pytest.raises(SecretStoreReadOnlyError):
            await store.revoke(reference)


class TestFileStore:
    async def test_roundtrip_permissions_and_persistence(self, tmp_path: Path) -> None:
        directory = tmp_path / "secrets"
        store = FileSecretStore(directory)
        reference = await store.put("orgs/1/integrations/2/creds/3", "whsec_file_value")
        assert await store.resolve(reference) == "whsec_file_value"
        # Owner-only on the directory and every secret file.
        assert os.stat(directory).st_mode & 0o777 == 0o700
        secret_file = directory / "orgs/1/integrations/2/creds/3"
        assert os.stat(secret_file).st_mode & 0o777 == 0o600
        # A fresh store over the same directory still resolves — real
        # persistence, unlike the memory store.
        assert await FileSecretStore(directory).resolve(reference) == "whsec_file_value"
        with pytest.raises(SecretStoreError, match="new name"):
            await store.put("orgs/1/integrations/2/creds/3", "other")
        await store.revoke(reference)
        with pytest.raises(SecretNotFoundError):
            await store.resolve(reference)

    async def test_names_cannot_escape_the_directory(self, tmp_path: Path) -> None:
        store = FileSecretStore(tmp_path / "secrets")
        # The reference type already refuses traversal shapes outright.
        with pytest.raises(ValueError):
            await store.put("../outside", "value")
