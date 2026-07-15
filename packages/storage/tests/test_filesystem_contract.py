"""Filesystem adapter contract and signed-claim coverage."""

import tempfile
from pathlib import Path

import pytest

from soa_storage import ObjectStore
from soa_storage.contract import ObjectStoreContract
from soa_storage.filesystem import FilesystemObjectStore


class TestFilesystemObjectStoreContract(ObjectStoreContract):
    async def make_store(self) -> ObjectStore:
        return FilesystemObjectStore(root=Path(tempfile.mkdtemp(prefix="soa-fs-contract-")))


async def test_upload_signature_binds_key_method_and_content_type(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    signed = await store.signed_upload_url(
        "org/doc/original.pdf",
        expires_in_seconds=60,
        content_type="application/pdf",
        size_bytes=42,
    )
    assert store.verify_signed_url(signed.url) == (
        "org/doc/original.pdf",
        "PUT",
        "application/pdf",
        42,
        None,
    )
    with pytest.raises(ValueError, match="failed verification"):
        store.verify_signed_url(signed.url.replace("application%2Fpdf", "image%2Fpng"))


async def test_upload_signature_binds_declared_checksum(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    digest = "a" * 64
    signed = await store.signed_upload_url(
        "org/doc/original.pdf",
        expires_in_seconds=60,
        content_type="application/pdf",
        size_bytes=42,
        sha256=digest,
    )
    assert signed.required_headers == {"X-SOA-Content-SHA256": digest}
    assert store.verify_signed_url(signed.url)[3:] == (42, digest)
    with pytest.raises(ValueError, match="failed verification"):
        store.verify_signed_url(signed.url.replace(digest, "b" * 64))
