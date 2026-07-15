"""Vendor-neutral object storage contract (STO-001).

Domain and application code depends on this module ONLY — never on a
vendor SDK. Adapters (memory here; MinIO/S3 in STO-002/006) implement
``ObjectStore`` and must pass the shared contract suite in
``soa_storage.contract``.

Design decisions the contract bakes in:

- **Checksums are first-class.** Every stored object records its SHA-256;
  ``put`` verifies a caller-supplied checksum and refuses mismatched
  writes, so corruption is caught at the door, not at read time.
- **Signed URLs, not open buckets.** Upload/download happen through
  short-lived signed URLs; the interface treats them as opaque strings
  plus an expiry instant.
- **Manifest listing.** ``list_by_manifest`` reports per-key presence and
  metadata for backup/reconciliation (STO-005) without ever streaming
  object content.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class ObjectStoreError(Exception):
    """Base class for storage failures."""


class ObjectNotFoundError(ObjectStoreError):
    """The key does not exist."""


class ChecksumMismatchError(ObjectStoreError):
    """The provided or stored checksum disagrees with the object bytes."""


class SignedUrlExpiredError(ObjectStoreError):
    """A signed URL was presented after its expiry."""


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: datetime
    method: str  # "GET" | "PUT"
    # Headers covered by the signature and therefore required on the client
    # request (for example the declared SHA-256 metadata on a direct upload).
    required_headers: dict[str, str] = field(default_factory=dict)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class ObjectStore(Protocol):
    """The storage contract. All methods are async; keys are opaque,
    forward-slash-separated strings owned by the caller."""

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        sha256: str | None = None,
    ) -> ObjectMetadata:
        """Store ``data`` under ``key``. When ``sha256`` is supplied it is
        verified against the bytes and a mismatch refuses the write."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the object bytes; ObjectNotFoundError if absent. The
        stored checksum is re-verified before bytes are returned."""
        ...

    async def head(self, key: str) -> ObjectMetadata:
        """Return metadata without fetching content."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the object. Deleting a missing key raises
        ObjectNotFoundError — silent deletes hide reconciliation bugs."""
        ...

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        """Server-side copy preserving content type and checksum."""
        ...

    async def list_by_manifest(self, keys: Sequence[str]) -> dict[str, ObjectMetadata | None]:
        """Presence report for every requested key (None = missing).
        Never returns object content."""
        ...

    async def list_keys(self, prefix: str = "") -> list[str]:
        """Every stored key under ``prefix`` (reconciliation: finding
        objects with no owning record). Keys only, never content."""
        ...

    async def signed_upload_url(
        self,
        key: str,
        *,
        expires_in_seconds: int,
        content_type: str,
        size_bytes: int,
        sha256: str | None = None,
    ) -> SignedUrl:
        """Short-lived URL a client can PUT bytes to.

        ``size_bytes`` is bound at the storage authorization boundary so a
        client cannot declare a small file and use the capability to store an
        unbounded object. When ``sha256`` is supplied, production adapters
        also bind it into signed object metadata.
        """
        ...

    async def signed_download_url(self, key: str, *, expires_in_seconds: int) -> SignedUrl:
        """Short-lived URL a client can GET the object from. The object
        must exist."""
        ...

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        """Abort an in-progress multipart upload, freeing partial data.
        Unknown uploads abort as a no-op (retries must be safe)."""
        ...
