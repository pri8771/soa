"""Google Cloud Storage ObjectStore adapter (STO-002 for GCP; OPEN-001).

Implements the STO-001 ``ObjectStore`` contract on Google Cloud Storage —
the object store for the GCP/Firebase deployment (``docs/DECISIONS.md``
OPEN-001), the peer of the S3 adapter. Same discipline: this module is
the only place the GCS SDK appears, and ``soa_storage/__init__`` never
imports it, so importing the package stays SDK-free — callers import
``soa_storage.gcs`` directly.

The GCS client is SYNCHRONOUS (there is no official async client), so
every blocking call is dispatched to a worker thread with
``asyncio.to_thread`` to keep the adapter's async contract without
blocking the event loop.

Contract fidelity mirrors the S3 adapter:

- **checksums are first-class** — the SHA-256 is written as object custom
  metadata at ``put`` and re-verified on ``get``; a caller-supplied
  checksum that disagrees with the bytes refuses the write.
- **signed URLs, not open buckets** — uploads/downloads use short-lived
  V4 signed URLs (signing needs service-account credentials; on Cloud Run
  that means a key or IAM SignBlob — an operational concern, not this
  module's).
- **loud deletes / missing** — deleting or reading a missing key raises
  ``ObjectNotFoundError`` rather than passing silently.

Encryption at rest is always on in GCS; an optional customer-managed key
(``kms_key_name``) is applied per object when configured.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from soa_storage.store import (
    ChecksumMismatchError,
    ObjectMetadata,
    ObjectNotFoundError,
    SignedUrl,
    sha256_hex,
)

_SHA256_META = "sha256"


@dataclass(frozen=True)
class GcsSettings:
    """Adapter configuration. ``kms_key_name`` (a full
    ``projects/.../cryptoKeys/...`` resource) applies a customer-managed
    encryption key per object; omitted, Google's default at-rest
    encryption applies."""

    bucket: str
    project: str
    kms_key_name: str | None = None

    def __post_init__(self) -> None:
        if not self.bucket.strip():
            raise ValueError("a GCS bucket name is required")
        if not self.project.strip():
            raise ValueError("a GCP project is required")


def _now() -> datetime:
    return datetime.now(tz=UTC)


class GcsObjectStore:
    """ObjectStore over Google Cloud Storage. ``client`` is injectable for
    tests; when omitted a ``google.cloud.storage.Client`` is created
    lazily."""

    def __init__(self, settings: GcsSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # type: ignore[import-untyped, attr-defined]

            self._client = storage.Client(project=self._settings.project)
        return self._client

    def _bucket(self) -> Any:
        return self._get_client().bucket(self._settings.bucket)

    def _blob(self, key: str) -> Any:
        if self._settings.kms_key_name:
            return self._bucket().blob(key, kms_key_name=self._settings.kms_key_name)
        return self._bucket().blob(key)

    def _signed_url_credentials(self) -> dict[str, str]:
        """Return keyless IAM-signing arguments when running on Cloud Run.

        Local service-account credentials implement ``sign_bytes`` and the
        storage library uses them directly. Metadata-server credentials do
        not carry a private key, so refresh them to discover the runtime
        identity and ask IAM Credentials ``signBlob`` to sign with the short-
        lived access token. The runtime service account receives permission
        to sign only as itself in Terraform; no downloadable key exists.

        Injected test clients intentionally have no credentials and keep the
        adapter's original fake-friendly path.
        """
        client = self._get_client()
        credentials = getattr(client, "_credentials", None)
        if credentials is None or hasattr(credentials, "sign_bytes"):
            return {}

        email = getattr(credentials, "service_account_email", None)
        token = getattr(credentials, "token", None)
        if not token or not email or email == "default":
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
            email = getattr(credentials, "service_account_email", None)
            token = getattr(credentials, "token", None)
        if not token or not email or email == "default":
            raise RuntimeError(
                "GCS signed URLs require signable credentials or a refreshed "
                "Cloud Run service-account identity"
            )
        return {"service_account_email": str(email), "access_token": str(token)}

    # -- helpers that run the blocking SDK off the event loop --------------

    def _put_sync(self, key: str, data: bytes, content_type: str, digest: str) -> None:
        blob = self._blob(key)
        blob.metadata = {_SHA256_META: digest}
        blob.upload_from_string(data, content_type=content_type)

    def _get_blob_sync(self, key: str) -> Any:
        # get_blob returns None for a missing key and populates metadata.
        return self._bucket().get_blob(key)

    def _download_sync(self, blob: Any) -> bytes:
        return bytes(blob.download_as_bytes())

    # -- ObjectStore contract ---------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        sha256: str | None = None,
    ) -> ObjectMetadata:
        digest = sha256_hex(data)
        if sha256 is not None and sha256 != digest:
            raise ChecksumMismatchError(
                f"declared sha256 {sha256[:12]}… does not match the uploaded bytes"
            )
        await asyncio.to_thread(self._put_sync, key, data, content_type, digest)
        return ObjectMetadata(
            key=key,
            size=len(data),
            sha256=digest,
            content_type=content_type,
            created_at=_now(),
        )

    async def get(self, key: str) -> bytes:
        blob = await asyncio.to_thread(self._get_blob_sync, key)
        if blob is None:
            raise ObjectNotFoundError(f"object {key!r} does not exist")
        data = await asyncio.to_thread(self._download_sync, blob)
        declared = (blob.metadata or {}).get(_SHA256_META)
        if declared is not None and sha256_hex(data) != declared:
            raise ChecksumMismatchError(f"stored object {key!r} failed checksum re-verification")
        return data

    async def head(self, key: str) -> ObjectMetadata:
        blob = await asyncio.to_thread(self._get_blob_sync, key)
        if blob is None:
            raise ObjectNotFoundError(f"object {key!r} does not exist")
        return _metadata_from_blob(key, blob)

    async def delete(self, key: str) -> None:
        # A loud failure for a missing key (the contract) — head first,
        # then delete the known-present blob.
        await self.head(key)
        await asyncio.to_thread(self._bucket().blob(key).delete)

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        source = await self.head(source_key)

        def _copy() -> None:
            bucket = self._bucket()
            source_blob = bucket.blob(source_key)
            bucket.copy_blob(source_blob, bucket, destination_key)

        await asyncio.to_thread(_copy)
        return ObjectMetadata(
            key=destination_key,
            size=source.size,
            sha256=source.sha256,
            content_type=source.content_type,
            created_at=_now(),
        )

    async def list_by_manifest(self, keys: Sequence[str]) -> dict[str, ObjectMetadata | None]:
        report: dict[str, ObjectMetadata | None] = {}
        for key in keys:
            try:
                report[key] = await self.head(key)
            except ObjectNotFoundError:
                report[key] = None
        return report

    async def list_keys(self, prefix: str = "") -> list[str]:
        def _list() -> list[str]:
            client = self._get_client()
            return [blob.name for blob in client.list_blobs(self._settings.bucket, prefix=prefix)]

        keys = await asyncio.to_thread(_list)
        return sorted(keys)

    async def signed_upload_url(
        self, key: str, *, expires_in_seconds: int, content_type: str
    ) -> SignedUrl:
        def _sign() -> str:
            return str(
                self._blob(key).generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=expires_in_seconds),
                    method="PUT",
                    content_type=content_type,
                    **self._signed_url_credentials(),
                )
            )

        url = await asyncio.to_thread(_sign)
        return SignedUrl(
            url=url,
            expires_at=_now() + timedelta(seconds=expires_in_seconds),
            method="PUT",
        )

    async def signed_download_url(self, key: str, *, expires_in_seconds: int) -> SignedUrl:
        await self.head(key)  # the contract: no URLs for missing objects

        def _sign() -> str:
            return str(
                self._blob(key).generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=expires_in_seconds),
                    method="GET",
                    **self._signed_url_credentials(),
                )
            )

        url = await asyncio.to_thread(_sign)
        return SignedUrl(
            url=url,
            expires_at=_now() + timedelta(seconds=expires_in_seconds),
            method="GET",
        )

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        # GCS has no S3-style multipart upload id to abort: resumable
        # upload sessions are addressed by an opaque session URI and expire
        # on their own (about a week). There is nothing id-addressable to
        # cancel here, so this is a safe no-op — matching the contract's
        # "unknown uploads abort as a no-op".
        return None


def _metadata_from_blob(key: str, blob: Any) -> ObjectMetadata:
    return ObjectMetadata(
        key=key,
        size=int(blob.size or 0),
        sha256=str((blob.metadata or {}).get(_SHA256_META, "")),
        content_type=str(blob.content_type or "application/octet-stream"),
        created_at=blob.time_created or _now(),
    )


__all__ = ["GcsObjectStore", "GcsSettings"]
