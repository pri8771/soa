"""AWS Secrets Manager adapter tests (SEC-005) against a faked client.

These verify the adapter's TRANSLATION layer — name/reference mapping,
error classification, rotation and recovery-window semantics — not AWS
itself; the live backend is exercised at deployment verification (REL),
like the S3 production configuration."""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from soa_config import (
    FileSecretStore,
    MemorySecretStore,
    SecretNotFoundError,
    SecretReference,
    SecretStoreError,
    SecretStoreUnavailableError,
)
from soa_storage.secrets_aws import AwsSecretsManagerStore, build_secret_store


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeSecretsManagerClient:
    """The subset of the secretsmanager client the adapter touches,
    with the manager's documented error codes."""

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def __aenter__(self) -> "FakeSecretsManagerClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def create_secret(self, *, Name: str, SecretString: str) -> dict[str, Any]:
        if Name in self._state:
            raise _client_error("ResourceExistsException", "CreateSecret")
        self._state[Name] = {"value": SecretString, "deletion_scheduled": False}
        return {"Name": Name}

    async def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        entry = self._state.get(SecretId)
        if entry is None:
            raise _client_error("ResourceNotFoundException", "GetSecretValue")
        if entry["deletion_scheduled"]:
            raise _client_error("InvalidRequestException", "GetSecretValue")
        return {"SecretString": entry["value"]}

    async def delete_secret(self, *, SecretId: str, RecoveryWindowInDays: int) -> dict[str, Any]:
        entry = self._state.get(SecretId)
        if entry is None:
            raise _client_error("ResourceNotFoundException", "DeleteSecret")
        assert RecoveryWindowInDays >= 7, "deletion must keep the recovery window"
        entry["deletion_scheduled"] = True
        return {"Name": SecretId}


class FakeSession:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.regions_seen: list[str] = []

    def create_client(self, service: str, *, region_name: str) -> FakeSecretsManagerClient:
        assert service == "secretsmanager"
        self.regions_seen.append(region_name)
        return FakeSecretsManagerClient(self._state)


class UnavailableSecretsManagerClient(FakeSecretsManagerClient):
    async def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        del SecretId
        raise _client_error("ServiceUnavailableException", "GetSecretValue")


class UnavailableSession(FakeSession):
    def create_client(self, service: str, *, region_name: str) -> FakeSecretsManagerClient:
        assert service == "secretsmanager"
        self.regions_seen.append(region_name)
        return UnavailableSecretsManagerClient(self._state)


@pytest.fixture
def state() -> dict[str, Any]:
    return {}


@pytest.fixture
def store(state: dict[str, Any]) -> AwsSecretsManagerStore:
    return AwsSecretsManagerStore(region="eu-central-1", session=FakeSession(state))


async def test_roundtrip_and_reference_shape(
    store: AwsSecretsManagerStore, state: dict[str, Any]
) -> None:
    reference = await store.put("orgs/1/integrations/2/creds/3", "whsec_aws_value")
    assert str(reference) == "secretref://aws-secrets-manager/orgs/1/integrations/2/creds/3"
    assert await store.resolve(reference) == "whsec_aws_value"
    assert state["orgs/1/integrations/2/creds/3"]["value"] == "whsec_aws_value"


async def test_existing_names_are_refused(store: AwsSecretsManagerStore) -> None:
    await store.put("orgs/1/creds/a", "one")
    with pytest.raises(SecretStoreError, match="new name"):
        await store.put("orgs/1/creds/a", "two")


async def test_revocation_schedules_deletion_and_stops_resolution(
    store: AwsSecretsManagerStore, state: dict[str, Any]
) -> None:
    reference = await store.put("orgs/1/creds/a", "value")
    await store.revoke(reference)
    # Deletion is SCHEDULED (recoverable by operators), yet the value no
    # longer resolves — the recovery window is not a live read path.
    assert state["orgs/1/creds/a"]["deletion_scheduled"] is True
    with pytest.raises(SecretNotFoundError):
        await store.resolve(reference)
    await store.revoke(reference)  # idempotent


async def test_missing_secrets_and_foreign_references(store: AwsSecretsManagerStore) -> None:
    with pytest.raises(SecretNotFoundError):
        await store.resolve(SecretReference.parse("secretref://aws-secrets-manager/nope"))
    with pytest.raises(SecretStoreError, match="refusing to resolve across providers"):
        await store.resolve(SecretReference.parse("secretref://memory/name"))


async def test_transient_aws_failure_is_classified_retryable() -> None:
    store = AwsSecretsManagerStore(
        region="eu-central-1",
        session=UnavailableSession({"orgs/1/creds/a": {"value": "v", "deletion_scheduled": False}}),
    )
    with pytest.raises(SecretStoreUnavailableError, match="temporarily unavailable"):
        await store.resolve(SecretReference.parse("secretref://aws-secrets-manager/orgs/1/creds/a"))


class TestBackendFactory:
    def test_builds_each_configured_backend(self, tmp_path: Any) -> None:
        assert isinstance(build_secret_store(backend="memory"), MemorySecretStore)
        assert isinstance(
            build_secret_store(backend="file", directory=str(tmp_path / "s")), FileSecretStore
        )
        aws = build_secret_store(backend="aws-secrets-manager", aws_region="eu-central-1")
        assert isinstance(aws, AwsSecretsManagerStore)

    def test_missing_parameters_and_unknown_backends_fail(self) -> None:
        with pytest.raises(ValueError, match="directory"):
            build_secret_store(backend="file")
        with pytest.raises(ValueError, match="region"):
            build_secret_store(backend="aws-secrets-manager")
        with pytest.raises(ValueError, match="unknown secrets backend"):
            build_secret_store(backend="vault")
