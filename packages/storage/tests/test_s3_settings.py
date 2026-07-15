"""Production configuration surface of the S3 adapter (STO-006)."""

from urllib.parse import parse_qs, urlparse

import pytest

from soa_storage.s3 import S3ObjectStore, S3Settings, encryption_args


def make(**overrides: object) -> S3Settings:
    base: dict[str, object] = {
        "endpoint_url": "https://s3.example.com",
        "access_key": "ak",
        "secret_key": "sk",
        "bucket": "b",
    }
    base.update(overrides)
    return S3Settings(**base)  # type: ignore[arg-type]


def test_encryption_disabled_by_default() -> None:
    assert encryption_args(make()) == {}


def test_sse_s3_mode() -> None:
    assert encryption_args(make(sse="AES256")) == {"ServerSideEncryption": "AES256"}


def test_sse_kms_with_customer_key() -> None:
    args = encryption_args(make(sse="aws:kms", sse_kms_key_id="key-123"))
    assert args == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "key-123"}


def test_invalid_modes_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unsupported sse mode"):
        make(sse="rot13")
    with pytest.raises(ValueError, match="requires sse='aws:kms'"):
        make(sse="AES256", sse_kms_key_id="key-123")


def test_path_style_is_configurable() -> None:
    assert make().force_path_style is True  # MinIO default
    assert make(force_path_style=False).force_path_style is False  # AWS virtual-hosted


async def test_presigned_put_binds_the_exact_declared_content_length() -> None:
    store = S3ObjectStore(make())
    signed = await store.signed_upload_url(
        "org/doc/original.pdf",
        expires_in_seconds=60,
        content_type="application/pdf",
        size_bytes=42,
        sha256="a" * 64,
    )
    signed_headers = parse_qs(urlparse(signed.url).query)["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signed_headers
