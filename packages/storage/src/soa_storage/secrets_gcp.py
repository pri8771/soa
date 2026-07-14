"""GCP Secret Manager adapter (SEC-005, OPEN-001 GCP deployment).

Implements the ``soa_config.SecretStore`` protocol on Google Cloud Secret
Manager — the production backend for the GCP/Firebase deployment
(``docs/DECISIONS.md`` OPEN-001). Follows the AWS adapter's discipline:
this module is the only place the GCP SDK appears for secrets, and
``soa_storage/__init__`` never imports it, so importing the package stays
SDK-free — callers import ``soa_storage.secrets_gcp`` directly.

Semantics mapped onto Secret Manager:

- a SecretStore ``name`` is path-like (``orgs/<id>/integrations/...``) and
  may contain ``/`` and ``.``, which a Secret Manager secret id may NOT.
  The adapter maps each name to a stable, collision-resistant id
  (``soa-<sha256(name)>``) computed identically on put/resolve/revoke, so
  the reference is the only handle callers ever need. (The readable name
  is not stored on the GCP resource — labels can't hold ``/`` or upper
  case — so operators identify secrets by reference, as elsewhere.)
- ``put`` creates a NEW secret container then adds its first version;
  rotation issues a fresh reference, so an existing name is a hard error
  (matching the interface's immutable-reference rule).
- ``resolve`` reads the ``latest`` version; a missing or destroyed secret
  is NOT FOUND.
- ``revoke`` deletes the secret and all versions, idempotently. (Unlike
  AWS's scheduled deletion with a recovery window, Secret Manager
  deletion is immediate; the platform already treats a revoked reference
  as dead, and object/secret recovery for GCP is a documented operational
  concern, not a live read path.)
"""

import hashlib
from typing import Any

from soa_config import (
    FileSecretStore,
    MemorySecretStore,
    SecretNotFoundError,
    SecretReference,
    SecretStore,
    SecretStoreError,
)

__all__ = ["GcpSecretManagerStore", "build_secret_store"]

_PROVIDER = "gcp-secret-manager"


def _secret_id(name: str) -> str:
    """A Secret-Manager-valid id (``[A-Za-z0-9_-]``, <=255) derived
    deterministically from the reference name. Hashed because the name may
    contain ``/`` and ``.``; the same name always yields the same id, so
    resolve/revoke find what put created."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"soa-{digest}"


class GcpSecretManagerStore:
    """SecretStore over Google Cloud Secret Manager. ``client`` is
    injectable for tests; when omitted an async client is created lazily.
    """

    def __init__(self, *, project: str, client: Any | None = None) -> None:
        if not project:
            raise ValueError("the GCP secret store requires a project id")
        self._project = project
        self._client = client

    @property
    def provider(self) -> str:
        return _PROVIDER

    @property
    def _parent(self) -> str:
        return f"projects/{self._project}"

    def _secret_path(self, name: str) -> str:
        return f"{self._parent}/secrets/{_secret_id(name)}"

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager_v1

            self._client = secretmanager_v1.SecretManagerServiceAsyncClient()
        return self._client

    def _require_same_provider(self, reference: SecretReference) -> None:
        if reference.provider != self.provider:
            raise SecretStoreError(
                f"reference {reference} belongs to provider {reference.provider!r}; "
                f"this store is {self.provider!r} — refusing to resolve across providers"
            )

    async def put(self, name: str, value: str) -> SecretReference:
        from google.api_core import exceptions as gcp_exceptions

        reference = SecretReference(provider=self.provider, name=name)
        client = self._get_client()
        try:
            secret = await client.create_secret(
                request={
                    "parent": self._parent,
                    "secret_id": _secret_id(name),
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        except gcp_exceptions.AlreadyExists:
            raise SecretStoreError(
                f"secret {name!r} already exists — rotation uses a new name"
            ) from None
        await client.add_secret_version(
            request={"parent": secret.name, "payload": {"data": value.encode("utf-8")}}
        )
        return reference

    async def resolve(self, reference: SecretReference) -> str:
        from google.api_core import exceptions as gcp_exceptions

        self._require_same_provider(reference)
        client = self._get_client()
        version = f"{self._secret_path(reference.name)}/versions/latest"
        try:
            response = await client.access_secret_version(request={"name": version})
        except gcp_exceptions.NotFound:
            raise SecretNotFoundError(f"no live secret behind {reference}") from None
        except gcp_exceptions.FailedPrecondition:
            # A disabled/destroyed latest version — treat as not found; a
            # revoked secret must never resolve.
            raise SecretNotFoundError(f"no live secret behind {reference}") from None
        return str(response.payload.data.decode("utf-8"))

    async def revoke(self, reference: SecretReference) -> None:
        from google.api_core import exceptions as gcp_exceptions

        self._require_same_provider(reference)
        client = self._get_client()
        try:
            await client.delete_secret(request={"name": self._secret_path(reference.name)})
        except gcp_exceptions.NotFound:
            return  # idempotent: already gone


def build_secret_store(
    *,
    backend: str,
    directory: str | None = None,
    aws_region: str | None = None,
    gcp_project: str | None = None,
) -> SecretStore:
    """Construct the configured backend from validated settings values.
    Superset of ``secrets_aws.build_secret_store`` adding the GCP backend;
    prefer this factory when the GCP backend may be selected."""
    if backend == "memory":
        return MemorySecretStore()
    if backend == "file":
        if not directory:
            raise ValueError("the file secrets backend requires a directory")
        return FileSecretStore(directory)
    if backend == "aws-secrets-manager":
        from soa_storage.secrets_aws import AwsSecretsManagerStore

        if not aws_region:
            raise ValueError("the aws-secrets-manager backend requires a region")
        return AwsSecretsManagerStore(region=aws_region)
    if backend == "gcp-secret-manager":
        if not gcp_project:
            raise ValueError("the gcp-secret-manager backend requires a project id")
        return GcpSecretManagerStore(project=gcp_project)
    raise ValueError(f"unknown secrets backend {backend!r}")
