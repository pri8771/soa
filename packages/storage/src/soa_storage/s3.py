"""S3-compatible ObjectStore adapter (STO-002).

Works against MinIO locally and any S3-compatible endpoint in production
(STO-006 adds the production configuration surface). This module is the
ONLY place vendor SDK types appear — nothing here leaks into the
interface, and ``soa_storage/__init__`` never imports it, so importing
the package stays SDK-free.

Checksums ride as object metadata (``x-amz-meta-sha256``) written at put
time and re-verified on get; S3's own ETag is not trusted as a content
hash (it isn't one for multipart uploads).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import ClientError

from soa_storage.store import (
    ChecksumMismatchError,
    ObjectMetadata,
    ObjectNotFoundError,
    SignedUrl,
    sha256_hex,
)

_SHA256_META = "sha256"


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "us-east-1"
    # MinIO requires path-style addressing; AWS accepts both.
    force_path_style: bool = True


def _is_missing(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound")


class S3ObjectStore:
    """ObjectStore over any S3-compatible endpoint."""

    def __init__(self, settings: S3Settings) -> None:
        self._settings = settings
        self._session = get_session()

    def _client(self) -> Any:
        return self._session.create_client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            aws_access_key_id=self._settings.access_key,
            aws_secret_access_key=self._settings.secret_key,
            region_name=self._settings.region,
            config=Config(
                s3={"addressing_style": "path" if self._settings.force_path_style else "auto"},
                signature_version="s3v4",
            ),
        )

    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist (local bootstrap)."""
        async with self._client() as client:
            try:
                await client.head_bucket(Bucket=self._settings.bucket)
            except ClientError as error:
                if not _is_missing(error):
                    raise
                await client.create_bucket(Bucket=self._settings.bucket)

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
        async with self._client() as client:
            await client.put_object(
                Bucket=self._settings.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={_SHA256_META: digest},
            )
        return ObjectMetadata(
            key=key,
            size=len(data),
            sha256=digest,
            content_type=content_type,
            created_at=datetime.now(tz=UTC),
        )

    async def get(self, key: str) -> bytes:
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._settings.bucket, Key=key)
            except ClientError as error:
                if _is_missing(error):
                    raise ObjectNotFoundError(f"object {key!r} does not exist") from None
                raise
            async with response["Body"] as stream:
                data: bytes = await stream.read()
            declared = response.get("Metadata", {}).get(_SHA256_META)
        if declared is not None and sha256_hex(data) != declared:
            raise ChecksumMismatchError(f"stored object {key!r} failed checksum re-verification")
        return data

    async def head(self, key: str) -> ObjectMetadata:
        async with self._client() as client:
            try:
                response = await client.head_object(Bucket=self._settings.bucket, Key=key)
            except ClientError as error:
                if _is_missing(error):
                    raise ObjectNotFoundError(f"object {key!r} does not exist") from None
                raise
        return ObjectMetadata(
            key=key,
            size=int(response["ContentLength"]),
            sha256=str(response.get("Metadata", {}).get(_SHA256_META, "")),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            created_at=response["LastModified"],
        )

    async def delete(self, key: str) -> None:
        # S3 deletes are silent for missing keys; the contract demands a
        # loud failure, so existence is checked first.
        await self.head(key)
        async with self._client() as client:
            await client.delete_object(Bucket=self._settings.bucket, Key=key)

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        source = await self.head(source_key)
        async with self._client() as client:
            await client.copy_object(
                Bucket=self._settings.bucket,
                Key=destination_key,
                CopySource={"Bucket": self._settings.bucket, "Key": source_key},
                MetadataDirective="COPY",
            )
        return ObjectMetadata(
            key=destination_key,
            size=source.size,
            sha256=source.sha256,
            content_type=source.content_type,
            created_at=datetime.now(tz=UTC),
        )

    async def list_by_manifest(self, keys: Sequence[str]) -> dict[str, ObjectMetadata | None]:
        report: dict[str, ObjectMetadata | None] = {}
        for key in keys:
            try:
                report[key] = await self.head(key)
            except ObjectNotFoundError:
                report[key] = None
        return report

    async def signed_upload_url(
        self, key: str, *, expires_in_seconds: int, content_type: str
    ) -> SignedUrl:
        async with self._client() as client:
            url = await client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._settings.bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=expires_in_seconds,
            )
        return SignedUrl(
            url=str(url),
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds),
            method="PUT",
        )

    async def signed_download_url(self, key: str, *, expires_in_seconds: int) -> SignedUrl:
        await self.head(key)  # the contract: no URLs for missing objects
        async with self._client() as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._settings.bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )
        return SignedUrl(
            url=str(url),
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds),
            method="GET",
        )

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        async with self._client() as client:
            try:
                await client.abort_multipart_upload(
                    Bucket=self._settings.bucket, Key=key, UploadId=upload_id
                )
            except ClientError as error:
                code = str(error.response.get("Error", {}).get("Code", ""))
                if code == "NoSuchUpload" or _is_missing(error):
                    return  # aborting an unknown upload must be retry-safe
                raise
