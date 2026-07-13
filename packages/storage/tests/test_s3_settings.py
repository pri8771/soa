"""Production configuration surface of the S3 adapter (STO-006)."""

import pytest

from soa_storage.s3 import S3Settings, encryption_args


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
