"""GCP Secret Manager adapter tests (SEC-005).

A fake async Secret Manager client (storing versions in memory and
raising the real google-api-core exceptions) exercises the adapter's
mapping onto Secret Manager: hashed secret ids, create-then-add-version
puts, immutable references, not-found resolution, and idempotent revoke —
plus the cross-provider guard and the backend factory.
"""

import pytest
from google.api_core import exceptions as gcp_exceptions

from soa_config import MemorySecretStore, SecretNotFoundError, SecretReference, SecretStoreError
from soa_storage.secrets_gcp import GcpSecretManagerStore, _secret_id, build_secret_store

PROJECT = "soa-pilot"


class FakeSecretManagerClient:
    """Minimal in-memory stand-in for SecretManagerServiceAsyncClient."""

    def __init__(self) -> None:
        # secret_path -> latest payload bytes (None = no live version)
        self.secrets: dict[str, bytes | None] = {}

    async def create_secret(self, request: dict) -> object:
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.secrets:
            raise gcp_exceptions.AlreadyExists(f"{name} exists")
        self.secrets[name] = None
        return type("Secret", (), {"name": name})()

    async def add_secret_version(self, request: dict) -> object:
        self.secrets[request["parent"]] = request["payload"]["data"]
        return type("Version", (), {"name": f"{request['parent']}/versions/1"})()

    async def access_secret_version(self, request: dict) -> object:
        secret_path = request["name"].rsplit("/versions/", 1)[0]
        if secret_path not in self.secrets:
            raise gcp_exceptions.NotFound(f"{secret_path} missing")
        data = self.secrets[secret_path]
        if data is None:
            raise gcp_exceptions.FailedPrecondition("no live version")
        payload = type("Payload", (), {"data": data})()
        return type("Response", (), {"payload": payload})()

    async def delete_secret(self, request: dict) -> None:
        if request["name"] not in self.secrets:
            raise gcp_exceptions.NotFound(f"{request['name']} missing")
        del self.secrets[request["name"]]


def store() -> tuple[GcpSecretManagerStore, FakeSecretManagerClient]:
    client = FakeSecretManagerClient()
    return GcpSecretManagerStore(project=PROJECT, client=client), client


class TestSecretIdMapping:
    def test_ids_are_valid_and_deterministic(self) -> None:
        # A path-like name with '/' and '.' maps to a Secret-Manager-valid id.
        name = "orgs/abc.def/integrations/quickbooks"
        first = _secret_id(name)
        assert first == _secret_id(name)  # deterministic
        assert first.startswith("soa-")
        assert all(c.isalnum() or c in "-_" for c in first)

    def test_distinct_names_map_to_distinct_ids(self) -> None:
        assert _secret_id("a/b") != _secret_id("a/c")


class TestRoundTrip:
    async def test_put_then_resolve_returns_the_value(self) -> None:
        gcp, _ = store()
        reference = await gcp.put("orgs/1/keys/anthropic", "sk-ant-123")
        assert str(reference) == "secretref://gcp-secret-manager/orgs/1/keys/anthropic"
        assert await gcp.resolve(reference) == "sk-ant-123"

    async def test_put_is_immutable_per_name(self) -> None:
        gcp, _ = store()
        await gcp.put("orgs/1/keys/a", "v1")
        with pytest.raises(SecretStoreError, match="already exists"):
            await gcp.put("orgs/1/keys/a", "v2")

    async def test_resolve_missing_is_not_found(self) -> None:
        gcp, _ = store()
        with pytest.raises(SecretNotFoundError):
            await gcp.resolve(SecretReference(provider="gcp-secret-manager", name="orgs/1/keys/x"))

    async def test_revoke_then_resolve_is_not_found(self) -> None:
        gcp, _ = store()
        reference = await gcp.put("orgs/1/keys/a", "v1")
        await gcp.revoke(reference)
        with pytest.raises(SecretNotFoundError):
            await gcp.resolve(reference)

    async def test_revoke_is_idempotent(self) -> None:
        gcp, _ = store()
        reference = await gcp.put("orgs/1/keys/a", "v1")
        await gcp.revoke(reference)
        await gcp.revoke(reference)  # no error the second time


class TestGuards:
    async def test_cross_provider_resolve_is_refused(self) -> None:
        gcp, _ = store()
        foreign = SecretReference(provider="aws-secrets-manager", name="orgs/1/keys/a")
        with pytest.raises(SecretStoreError, match="across providers"):
            await gcp.resolve(foreign)

    def test_empty_project_is_refused(self) -> None:
        with pytest.raises(ValueError, match="project id"):
            GcpSecretManagerStore(project="")


class TestFactory:
    def test_factory_builds_the_gcp_backend(self) -> None:
        built = build_secret_store(backend="gcp-secret-manager", gcp_project=PROJECT)
        assert isinstance(built, GcpSecretManagerStore)

    def test_gcp_backend_requires_a_project(self) -> None:
        with pytest.raises(ValueError, match="project id"):
            build_secret_store(backend="gcp-secret-manager")

    def test_factory_still_builds_memory(self) -> None:
        assert isinstance(build_secret_store(backend="memory"), MemorySecretStore)
