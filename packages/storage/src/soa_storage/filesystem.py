"""Filesystem-backed ObjectStore for local development.

A zero-dependency ObjectStore for running the whole stack on one machine
without MinIO/S3 or Docker. Object bytes live under ``<root>/blobs/<key>``
and metadata sidecars under ``<root>/meta/<key>.json`` — two parallel
trees so ``list_keys`` never trips over metadata files.

Signed URLs use the same HMAC scheme as the in-memory reference adapter,
but point at the API's local-blob endpoint (``signed_upload_url`` /
``signed_download_url`` return ``{base_url}/<key>?...``). The API serves
those PUT/GET requests by verifying the signature and reading/writing this
store, so a browser can upload exactly as it would to a presigned S3 URL —
while the worker reads the same bytes off disk via :meth:`get`.

Not for production: there is no durability, replication, or access control
beyond the short-lived signature. Production uses S3 (STO-006) or GCS.
"""

import hashlib
import hmac
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from soa_storage.store import (
    ChecksumMismatchError,
    ObjectMetadata,
    ObjectNotFoundError,
    SignedUrl,
    SignedUrlExpiredError,
    sha256_hex,
)

# A fixed dev secret keeps signatures stable across a process's lifetime.
# Only the API verifies these URLs, and they are short-lived, so a static
# local secret is acceptable for development-only storage.
_DEV_SIGNING_SECRET = b"soa-local-filesystem-store-dev-signing-key"


def _safe_relative(key: str) -> Path:
    """Turn an opaque store key into a path under the root, refusing any
    traversal. Keys are forward-slash separated by contract."""
    parts = [segment for segment in key.split("/") if segment not in ("", ".")]
    if any(segment == ".." for segment in parts):
        raise ValueError(f"unsafe object key {key!r}")
    if not parts:
        raise ValueError("empty object key")
    return Path(*parts)


class FilesystemObjectStore:
    """ObjectStore backed by the local filesystem (development only)."""

    def __init__(
        self, *, root: str | Path, base_url: str = "http://127.0.0.1:8000/_local-blobs"
    ) -> None:
        self._root = Path(root)
        self._blobs = self._root / "blobs"
        self._meta = self._root / "meta"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)
        self._secret = _DEV_SIGNING_SECRET
        self._base_url = base_url.rstrip("/")

    # -- paths --------------------------------------------------------------

    def _blob_path(self, key: str) -> Path:
        return self._blobs / _safe_relative(key)

    def _meta_path(self, key: str) -> Path:
        rel = _safe_relative(key)
        return self._meta / rel.parent / f"{rel.name}.json"

    # -- core contract ------------------------------------------------------

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
        blob_path = self._blob_path(key)
        meta_path = self._meta_path(key)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename for atomic-ish visibility.
        tmp = blob_path.with_suffix(blob_path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, blob_path)
        meta_path.write_text(
            json.dumps(
                {
                    "sha256": digest,
                    "content_type": content_type,
                    "created_at": metadata.created_at.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return metadata

    def _read_meta(self, key: str) -> ObjectMetadata:
        blob_path = self._blob_path(key)
        meta_path = self._meta_path(key)
        if not blob_path.exists() or not meta_path.exists():
            raise ObjectNotFoundError(f"object {key!r} does not exist")
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return ObjectMetadata(
            key=key,
            size=blob_path.stat().st_size,
            sha256=raw["sha256"],
            content_type=raw["content_type"],
            created_at=datetime.fromisoformat(raw["created_at"]),
        )

    async def get(self, key: str) -> bytes:
        metadata = self._read_meta(key)
        data = self._blob_path(key).read_bytes()
        if sha256_hex(data) != metadata.sha256:
            raise ChecksumMismatchError(f"stored object {key!r} failed checksum re-verification")
        return data

    async def head(self, key: str) -> ObjectMetadata:
        return self._read_meta(key)

    async def delete(self, key: str) -> None:
        blob_path = self._blob_path(key)
        if not blob_path.exists():
            raise ObjectNotFoundError(f"object {key!r} does not exist")
        blob_path.unlink()
        meta_path = self._meta_path(key)
        if meta_path.exists():
            meta_path.unlink()

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        data = await self.get(source_key)
        metadata = self._read_meta(source_key)
        return await self.put(
            destination_key, data, content_type=metadata.content_type, sha256=metadata.sha256
        )

    async def list_by_manifest(self, keys: Sequence[str]) -> dict[str, ObjectMetadata | None]:
        report: dict[str, ObjectMetadata | None] = {}
        for key in keys:
            try:
                report[key] = self._read_meta(key)
            except ObjectNotFoundError:
                report[key] = None
        return report

    async def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        for path in self._blobs.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                key = path.relative_to(self._blobs).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    # -- signed URLs --------------------------------------------------------

    async def signed_upload_url(
        self, key: str, *, expires_in_seconds: int, content_type: str
    ) -> SignedUrl:
        return self._sign(key, method="PUT", expires_in_seconds=expires_in_seconds)

    async def signed_download_url(self, key: str, *, expires_in_seconds: int) -> SignedUrl:
        self._read_meta(key)  # raises ObjectNotFoundError if absent
        return self._sign(key, method="GET", expires_in_seconds=expires_in_seconds)

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        return None

    def verify_signed_url(self, url: str, *, now: datetime | None = None) -> tuple[str, str]:
        """Validate a URL this adapter issued; returns ``(key, method)``.
        Raises SignedUrlExpiredError past expiry, ValueError on tamper."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        path = parsed.path
        marker = "/_local-blobs/"
        key = path.split(marker, 1)[1] if marker in path else path.lstrip("/")
        expires_raw = params.get("expires", ["0"])[0]
        signature = params.get("signature", [""])[0]
        method = params.get("method", [""])[0]
        expected = self._signature(key, method, expires_raw)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signed URL failed verification")
        current = now or datetime.now(tz=UTC)
        if current.timestamp() > float(expires_raw):
            raise SignedUrlExpiredError(f"signed URL for {key!r} expired")
        return key, method

    def _sign(self, key: str, *, method: str, expires_in_seconds: int) -> SignedUrl:
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds)
        expires_raw = str(expires_at.timestamp())
        signature = self._signature(key, method, expires_raw)
        url = f"{self._base_url}/{key}?method={method}&expires={expires_raw}&signature={signature}"
        return SignedUrl(url=url, expires_at=expires_at, method=method)

    def _signature(self, key: str, method: str, expires_raw: str) -> str:
        material = f"{method}\n{key}\n{expires_raw}".encode()
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()
