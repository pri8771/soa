"""AWS Secrets Manager adapter and backend factory (SEC-005).

Implements the ``soa_config.SecretStore`` protocol on AWS Secrets
Manager — the production backend. Follows the S3 adapter's discipline:
this module is the only place the vendor SDK appears for secrets, and
``soa_storage/__init__`` never imports it, so importing the package
stays SDK-free — callers import ``soa_storage.secrets_aws`` directly.

Semantics mapped onto the manager:

- ``put`` creates a NEW secret per reference (rotation issues fresh
  names, references stay immutable) — an existing name is a hard error;
- ``revoke`` schedules deletion with the manager's minimum 7-day
  recovery window rather than force-deleting: a fat-fingered rotation
  stays recoverable by operators, while the platform already treats the
  reference as dead (the database row is revoked);
- a secret already scheduled for deletion resolves as NOT FOUND — the
  recovery window is an operator affordance, never a live read path.

``build_secret_store`` turns validated settings into the configured
backend so the API and worker construct secrets access identically.
"""

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from soa_config import (
    FileSecretStore,
    MemorySecretStore,
    SecretNotFoundError,
    SecretReference,
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailableError,
)

__all__ = ["AwsSecretsManagerStore", "build_secret_store"]

#: The manager's minimum recovery window; deletion is scheduled, not
#: immediate, so an operator can still recover from a bad rotation.
_RECOVERY_WINDOW_DAYS = 7


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


class AwsSecretsManagerStore:
    """SecretStore over AWS Secrets Manager."""

    def __init__(self, *, region: str, session: Any | None = None) -> None:
        if session is None:
            from aiobotocore.session import get_session

            session = get_session()
        self._session = session
        self._region = region

    @property
    def provider(self) -> str:
        return "aws-secrets-manager"

    def _client(self) -> Any:
        return self._session.create_client("secretsmanager", region_name=self._region)

    async def put(self, name: str, value: str) -> SecretReference:
        reference = SecretReference(provider=self.provider, name=name)
        async with self._client() as client:
            try:
                await client.create_secret(Name=name, SecretString=value)
            except ClientError as error:
                if _error_code(error) == "ResourceExistsException":
                    raise SecretStoreError(
                        f"secret {name!r} already exists — rotation uses a new name"
                    ) from None
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error
            except BotoCoreError as error:
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error
        return reference

    async def resolve(self, reference: SecretReference) -> str:
        if reference.provider != self.provider:
            raise SecretStoreError(
                f"reference {reference} belongs to provider {reference.provider!r}; "
                f"this store is {self.provider!r} — refusing to resolve across providers"
            )
        async with self._client() as client:
            try:
                response = await client.get_secret_value(SecretId=reference.name)
            except ClientError as error:
                if _error_code(error) in ("ResourceNotFoundException", "InvalidRequestException"):
                    # InvalidRequest covers "scheduled for deletion" — a
                    # revoked secret must not resolve during its recovery
                    # window.
                    raise SecretNotFoundError(f"no live secret behind {reference}") from None
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error
            except BotoCoreError as error:
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error
        value = response.get("SecretString")
        if value is None:
            raise SecretStoreError(f"secret behind {reference} is binary, not a string")
        return str(value)

    async def revoke(self, reference: SecretReference) -> None:
        if reference.provider != self.provider:
            raise SecretStoreError(
                f"reference {reference} belongs to provider {reference.provider!r}; "
                f"this store is {self.provider!r}"
            )
        async with self._client() as client:
            try:
                await client.delete_secret(
                    SecretId=reference.name, RecoveryWindowInDays=_RECOVERY_WINDOW_DAYS
                )
            except ClientError as error:
                if _error_code(error) in ("ResourceNotFoundException", "InvalidRequestException"):
                    return  # idempotent: already gone or already scheduled
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error
            except BotoCoreError as error:
                raise SecretStoreUnavailableError(
                    "AWS Secrets Manager is temporarily unavailable"
                ) from error


def build_secret_store(
    *,
    backend: str,
    directory: str | None = None,
    aws_region: str | None = None,
) -> SecretStore:
    """Construct the configured backend from validated settings values
    (``BaseServiceSettings`` already guaranteed the required fields)."""
    if backend == "memory":
        return MemorySecretStore()
    if backend == "file":
        if not directory:
            raise ValueError("the file secrets backend requires a directory")
        return FileSecretStore(directory)
    if backend == "aws-secrets-manager":
        if not aws_region:
            raise ValueError("the aws-secrets-manager backend requires a region")
        return AwsSecretsManagerStore(region=aws_region)
    raise ValueError(f"unknown secrets backend {backend!r}")
