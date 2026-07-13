"""In-memory reference adapter (STO-001).

The simplest correct implementation of the ObjectStore contract. It
exists so the contract suite has a reference to validate against and so
unit tests elsewhere can use real storage semantics without network
dependencies. Signed URLs are HMAC-signed opaque strings the adapter can
verify itself — the same shape a real presigned URL check has.
"""

import hmac
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from soa_storage.store import (
    ChecksumMismatchError,
    ObjectMetadata,
    ObjectNotFoundError,
    SignedUrl,
    SignedUrlExpiredError,
    sha256_hex,
)


class MemoryObjectStore:
    """Reference ObjectStore. Not for production — state lives in-process."""

    def __init__(self, *, base_url: str = "memory://store") -> None:
        self._objects: dict[str, tuple[bytes, ObjectMetadata]] = {}
        self._secret = secrets.token_bytes(32)
        self._base_url = base_url

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
        metadata = ObjectMetadata(
            key=key,
            size=len(data),
            sha256=digest,
            content_type=content_type,
            created_at=datetime.now(tz=UTC),
        )
        self._objects[key] = (bytes(data), metadata)
        return metadata

    async def get(self, key: str) -> bytes:
        data, metadata = self._require(key)
        if sha256_hex(data) != metadata.sha256:
            raise ChecksumMismatchError(f"stored object {key!r} failed checksum re-verification")
        return data

    async def head(self, key: str) -> ObjectMetadata:
        return self._require(key)[1]

    async def delete(self, key: str) -> None:
        self._require(key)
        del self._objects[key]

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        data, metadata = self._require(source_key)
        return await self.put(
            destination_key, data, content_type=metadata.content_type, sha256=metadata.sha256
        )

    async def list_by_manifest(self, keys: Sequence[str]) -> dict[str, ObjectMetadata | None]:
        report: dict[str, ObjectMetadata | None] = {}
        for key in keys:
            entry = self._objects.get(key)
            report[key] = entry[1] if entry is not None else None
        return report

    async def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self._objects if key.startswith(prefix))

    async def signed_upload_url(
        self, key: str, *, expires_in_seconds: int, content_type: str
    ) -> SignedUrl:
        return self._sign(key, method="PUT", expires_in_seconds=expires_in_seconds)

    async def signed_download_url(self, key: str, *, expires_in_seconds: int) -> SignedUrl:
        self._require(key)
        return self._sign(key, method="GET", expires_in_seconds=expires_in_seconds)

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        # The memory adapter has no multipart state; aborting is a no-op,
        # which is exactly the idempotence the contract demands.
        return None

    # -- signing ------------------------------------------------------------

    def verify_signed_url(self, url: str, *, now: datetime | None = None) -> str:
        """Validate a URL this adapter issued; returns the key. Raises
        SignedUrlExpiredError past expiry, ObjectStoreError-family
        (ChecksumMismatchError is not used here) via ValueError on tamper."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        key = parsed.path.lstrip("/")
        expires_raw = params.get("expires", ["0"])[0]
        signature = params.get("signature", [""])[0]
        method = params.get("method", [""])[0]
        expected = self._signature(key, method, expires_raw)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signed URL failed verification")
        current = now or datetime.now(tz=UTC)
        if current.timestamp() > float(expires_raw):
            raise SignedUrlExpiredError(f"signed URL for {key!r} expired")
        return key

    def _sign(self, key: str, *, method: str, expires_in_seconds: int) -> SignedUrl:
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds)
        expires_raw = str(expires_at.timestamp())
        signature = self._signature(key, method, expires_raw)
        url = f"{self._base_url}/{key}?method={method}&expires={expires_raw}&signature={signature}"
        return SignedUrl(url=url, expires_at=expires_at, method=method)

    def _signature(self, key: str, method: str, expires_raw: str) -> str:
        material = f"{method}\n{key}\n{expires_raw}".encode()
        return hmac.new(self._secret, material, "sha256").hexdigest()

    def _require(self, key: str) -> tuple[bytes, ObjectMetadata]:
        entry = self._objects.get(key)
        if entry is None:
            raise ObjectNotFoundError(f"object {key!r} does not exist")
        return entry
