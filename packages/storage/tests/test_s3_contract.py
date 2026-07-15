"""S3 adapter contract run against a real MinIO (STO-002).

Marked ``minio``: runs when SOA_TEST_MINIO_URL points at a live MinIO
(CI starts one; locally `make services-up` provides it). Each test gets
its own bucket so runs are isolated and re-runnable.
"""

import os
import secrets

import pytest

from soa_storage import ObjectNotFoundError, ObjectStore
from soa_storage.contract import ObjectStoreContract
from soa_storage.s3 import S3ObjectStore, S3Settings

pytestmark = pytest.mark.minio

MINIO_URL = os.environ.get("SOA_TEST_MINIO_URL")


def make_settings() -> S3Settings:
    if not MINIO_URL:
        pytest.skip("SOA_TEST_MINIO_URL is not set; MinIO contract tests need a live MinIO")
    return S3Settings(
        endpoint_url=MINIO_URL,
        access_key=os.environ.get("SOA_TEST_MINIO_ACCESS_KEY", "soa_dev"),
        secret_key=os.environ.get("SOA_TEST_MINIO_SECRET_KEY", "soa_dev_password"),
        bucket=f"contract-{secrets.token_hex(6)}",
        # STO-006: point SOA_TEST_MINIO_URL at any S3-compatible
        # integration endpoint and optionally exercise encryption.
        sse=os.environ.get("SOA_TEST_MINIO_SSE") or None,
    )


class TestS3ObjectStoreContract(ObjectStoreContract):
    async def make_store(self) -> ObjectStore:
        store = S3ObjectStore(make_settings())
        await store.ensure_bucket()
        return store


async def test_signed_urls_actually_grant_and_deny_access() -> None:
    """End-to-end proof against MinIO: a presigned GET returns the bytes,
    and tampering with the signature is rejected by the server."""
    import httpx

    store = S3ObjectStore(make_settings())
    await store.ensure_bucket()
    await store.put("org-1/doc-1/original.pdf", b"pdf bytes", content_type="application/pdf")
    signed = await store.signed_download_url("org-1/doc-1/original.pdf", expires_in_seconds=60)

    async with httpx.AsyncClient() as client:
        granted = await client.get(signed.url)
        assert granted.status_code == 200
        assert granted.content == b"pdf bytes"

        tampered = await client.get(signed.url + "0")
        assert tampered.status_code in (400, 403), "tampered signature must be refused"


async def test_presigned_upload_refuses_a_body_larger_than_the_declaration() -> None:
    """The size limit is enforced by S3 before an oversized object lands."""
    import httpx

    store = S3ObjectStore(make_settings())
    await store.ensure_bucket()
    signed = await store.signed_upload_url(
        "org-1/doc-2/original.pdf",
        expires_in_seconds=60,
        content_type="application/pdf",
        size_bytes=4,
        sha256=None,
    )

    async with httpx.AsyncClient() as client:
        refused = await client.put(
            signed.url,
            content=b"12345",
            headers={"Content-Type": "application/pdf"},
        )
    assert refused.status_code in (400, 403)
    with pytest.raises(ObjectNotFoundError):
        # No object may be left behind after storage rejects the signed size.
        await store.head("org-1/doc-2/original.pdf")
